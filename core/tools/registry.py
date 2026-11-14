"""Toolbox: the registry the native actor calls tools through (plan section 1.10, brief A1).

``Toolbox(specs, context)`` holds the tool specs and the ``ToolContext`` they run
in. ``call`` does, in order:

1. looks the tool up (an unknown name is refused);
2. checks the required arguments from ``args_schema``;
3. calls ``ToolSpec.action_for(args)``, which refuses a tool whose action needs
   an effect it does not declare (bug B36);
4. refuses a tool with ``needs_approval`` unless the caller passes
   ``approved=True``; the toolbox never asks a human itself;
5. for a non-idempotent tool, returns the result already recorded in the effects
   journal under ``sha256(thread_id, tool_call_id)``, if there is one;
6. runs the tool body, and records the result for a non-idempotent tool.

No tool body calls ``interrupt()``. Standard library and ``core.tools.spec`` only.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

from core.tools.spec import ToolContext, ToolResult, ToolSpec, idempotency_key


class ToolRefused(Exception):
    """A call that was declined. The tool body did not run, or did nothing to disk."""


class Toolbox:
    def __init__(self, specs: Iterable[ToolSpec], context: ToolContext):
        table: dict[str, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec):
                raise TypeError(f"toolbox entries must be ToolSpec, got {type(spec).__name__}")
            if spec.name in table:
                raise ValueError(f"duplicate tool name {spec.name!r}")
            table[spec.name] = spec
        self._specs = table
        self._context = context

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolRefused(f"no tool named {name!r}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def call(self, name: str, args: dict, *, thread_id: str, tool_call_id: str,
             approved: bool = False) -> ToolResult:
        spec = self.get(name)
        if not isinstance(args, dict):
            raise ToolRefused(f"{name}: arguments must be an object")
        missing = [key for key in spec.args_schema.get("required", ()) if key not in args]
        if missing:
            raise ToolRefused(f"{name}: missing argument(s) {', '.join(missing)}")
        try:
            spec.action_for(args)
        except (TypeError, ValueError, KeyError) as exc:
            raise ToolRefused(f"{name}: {exc}") from exc
        if spec.needs_approval and not approved:
            raise ToolRefused(f"{name} needs a human approval before it runs")
        key = idempotency_key(thread_id, tool_call_id)
        if not spec.idempotent:
            recorded = self._context.journal.seen(key)
            if recorded is not None:
                return recorded
        result = spec.run(args, self._context)
        if not isinstance(result, ToolResult):
            raise TypeError(f"{name} must return a ToolResult, got {type(result).__name__}")
        if not spec.idempotent:
            self._context.journal.record(key, result)
        return result


def default_specs() -> tuple[ToolSpec, ...]:
    """The six file tools and the five harness tools.

    Imported here, not at module top, because the tool modules import
    ``ToolRefused`` from this module.
    """
    from core.tools import fs, harness_tools
    return (*fs.specs(), *harness_tools.specs())


class _Unavailable(Exception):
    """A granted tool that cannot be built from the configuration given. The text is the reason."""


def _build_one(name: str, *, exec_policy, net_config, doc_config, pack_tools) -> ToolSpec:
    from core.tools import data, doc, fs, harness_tools, net
    from core.tools import exec as exec_tools

    for spec in (*fs.specs(), *harness_tools.specs()):
        if spec.name == name:
            return spec
    if name == "run_command":
        if exec_policy is None:
            raise _Unavailable("no exec allow-list configured")
        return exec_tools.make_run_command_tool(exec_policy)
    if name == "fetch_url":
        hosts = (net_config or {}).get("allowed_hosts")
        if not hosts:
            raise _Unavailable("no fetch allow-list configured")
        return net.make_fetch_url(allowed_hosts=hosts)
    if name == "web_search":
        backend = (net_config or {}).get("search_backend")
        if backend is None:
            raise _Unavailable("no search backend configured")
        return net.make_web_search(backend)
    if name == "query_table":
        return data.make_query_table()
    if name == "extract_text":
        extract = (doc_config or {}).get("extract_command")
        if extract is None:
            return doc.make_extract_text()
        return doc.make_extract_text(command=extract)
    if name == "render_document":
        command = (doc_config or {}).get("render_command")
        if not command:
            raise _Unavailable("no render command configured")
        return doc.make_render_document(command=command)
    if name in pack_tools:
        return pack_tools[name]()
    raise _Unavailable("no implementation is registered for this tool")


def build_specs(grants: Iterable[str], *, exec_policy=None,
                net_config: Optional[Mapping] = None, doc_config: Optional[Mapping] = None,
                pack_tools: Optional[Mapping] = None,
                ) -> tuple[list[ToolSpec], list[tuple[str, str]]]:
    """The specs for exactly the granted names, and the grants that could not be built.

    Returns ``(specs, gaps)``. ``specs`` holds one ToolSpec per buildable grant, in name
    order. ``gaps`` holds ``(name, reason)`` for each grant that is not available, so the
    caller can say so; nothing is dropped silently.

    ``exec_policy`` is a ``core.tools.exec.ExecPolicy`` (run_command needs one).
    ``net_config`` is a mapping with ``allowed_hosts`` (fetch_url) and ``search_backend``
    (web_search). ``doc_config`` is a mapping with ``render_command`` (render_document,
    required) and ``extract_command`` (extract_text, optional). ``pack_tools`` maps a name
    to a zero-argument ToolSpec factory, e.g. ``{"apply_plan": apply_spec}``.
    """
    pack_tools = dict(pack_tools or {})
    specs: list[ToolSpec] = []
    gaps: list[tuple[str, str]] = []
    for name in sorted(set(grants)):
        try:
            spec = _build_one(name, exec_policy=exec_policy, net_config=net_config,
                              doc_config=doc_config, pack_tools=pack_tools)
        except (_Unavailable, ValueError, TypeError) as exc:
            gaps.append((name, str(exc)))
            continue
        if not isinstance(spec, ToolSpec) or spec.name != name:
            raise ValueError(f"factory for {name!r} did not return a ToolSpec named {name!r}")
        specs.append(spec)
    return specs, gaps
