"""External tools through the Model Context Protocol .

One entry point: ``load_mcp_tools(cfg, on_advisory=None) -> list[ToolSpec]``.

Adapter: ``langchain.mcp.MCPAdapter``, which works with the pinned versions
(``langchain-mcp-adapters`` was not tested). LangChain and FastMCP are imported inside the functions, never at
module top, so importing this module never fails and never starts a server.

Configuration (the ``mcp`` section; a plain mapping)::

    servers:
      <server name>:
        command: <executable>
        args: [<argument>, ...]
        env: {<name>: <value>}      # optional, added to the child environment
        timeout_s: 30               # optional, per session (start, list, call)
        tools:
          <tool name>:
            effects: [read | write | exec | net, ...]   # required to be trusted
            idempotent: false                            # optional
            needs_approval: false                        # optional

A tool with no ``effects`` in this configuration is treated as
``write + exec + net`` and needs approval. A server that
cannot start, or cannot list its tools, produces one advisory and no tools.
Nothing here raises for a bad server; problems go to ``on_advisory``.

Each tool call opens its own session to the server (start, list, call, stop).
That is slower than a long-lived connection, but a session never outlives the
call and never crosses an event loop.

The FastMCP update check and startup banner are switched off through
``FASTMCP_CHECK_FOR_UPDATES`` and ``FASTMCP_SHOW_SERVER_BANNER``. They are set
in this process before FastMCP is imported, and passed to each child server.

Standard library plus ``core.tools.spec`` and ``core.kernel.types``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import os
import warnings
from typing import Any, Callable, Mapping

from core.kernel.types import Action, ActionKind, canonical_json
from core.tools import spec as S

PREFIX = "mcp"
DEFAULT_TIMEOUT_S = 30.0
UNDECLARED_EFFECTS = frozenset({"write", "exec", "net"})

# Set before any server or client starts. The child gets the same values.
QUIET_ENV = {
    "FASTMCP_CHECK_FOR_UPDATES": "off",
    "FASTMCP_SHOW_SERVER_BANNER": "false",
    "FASTMCP_LOG_LEVEL": "WARNING",
}


class McpToolError(RuntimeError):
    """The MCP server ran the tool and reported an error."""


@dataclasses.dataclass(frozen=True)
class McpAdvisory:
    """A doctor-style advisory: never required, never a failed check."""
    id: str
    detail: str
    required: bool = False
    ok: bool = False


def _quiet_fastmcp() -> None:
    """Switch off the update check and the banner in this process."""
    os.environ.update(QUIET_ENV)


def _run(coro):
    """Run a coroutine to completion from synchronous code.

    If an event loop is already running in this thread, the coroutine runs on
    a fresh loop in a worker thread, so the caller never blocks its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _make_transport(server: Mapping[str, Any]):
    """The stdio transport for one server. Tests replace this to observe starts."""
    _quiet_fastmcp()
    from fastmcp.client.transports import StdioTransport
    env = {**{str(k): str(v) for k, v in (server.get("env") or {}).items()}, **QUIET_ENV}
    return StdioTransport(
        command=str(server["command"]),
        args=[str(a) for a in server.get("args") or []],
        env=env,
        keep_alive=False,
    )


def _adapter_class():
    _quiet_fastmcp()
    from langchain_core._api import LangChainBetaWarning
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LangChainBetaWarning)
        from langchain.mcp import MCPAdapter
    return MCPAdapter


async def _list_tools(server: Mapping[str, Any]) -> list[Any]:
    MCPAdapter = _adapter_class()
    async with MCPAdapter(_make_transport(server)) as adapter:
        return list(await adapter.list_tools())


async def _call_tool(server: Mapping[str, Any], tool_name: str, args: dict) -> str:
    MCPAdapter = _adapter_class()
    async with MCPAdapter(_make_transport(server)) as adapter:
        tools = {t.name: t for t in await adapter.list_tools()}
        if tool_name not in tools:
            raise McpToolError(f"server no longer offers tool {tool_name!r}")
        message = await tools[tool_name].ainvoke(
            {"type": "tool_call", "id": "mcp-call", "name": tool_name, "args": args})
    text = "\n".join(
        str(block.get("text", f"[{block.get('type', 'unknown')} block]"))
        for block in (message.content or []) if isinstance(block, dict))
    if getattr(message, "status", "success") == "error":
        raise McpToolError(text or f"tool {tool_name!r} reported an error")
    return text


def _timeout_of(server: Mapping[str, Any]) -> float:
    value = server.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("timeout_s must be a positive number")
    return float(value)


def _kind_for(effects: frozenset[str]) -> ActionKind:
    """The most restrictive action kind the declared effects cover."""
    for effect, kind in (("exec", ActionKind.EXEC), ("net", ActionKind.NET),
                         ("write", ActionKind.WRITE), ("read", ActionKind.READ)):
        if effect in effects:
            return kind
    raise ValueError("no effect to judge")


def _make_spec(server_name: str, server: Mapping[str, Any], tool: Any) -> S.ToolSpec:
    tool_name = str(tool.name)
    declared = (server.get("tools") or {}).get(tool_name) or {}
    raw_effects = declared.get("effects")
    if not raw_effects:  # missing or empty: fail closed, as undeclared
        effects = UNDECLARED_EFFECTS
        needs_approval = True
        idempotent = False
    else:
        if isinstance(raw_effects, str):
            raise ValueError("effects must be a list, not a string")
        effects = frozenset(str(e) for e in raw_effects)
        needs_approval = bool(declared.get("needs_approval", False))
        idempotent = bool(declared.get("idempotent", False))
    if not effects or effects - S.ALLOWED_EFFECTS:
        raise ValueError(f"unknown or empty effects {sorted(effects)!r}")
    name = f"{PREFIX}__{server_name}__{tool_name}"
    kind = _kind_for(effects)

    def to_action(args: dict) -> Action:
        body = canonical_json(args)
        if kind is ActionKind.EXEC:
            return Action(kind=kind, tool=name, command=f"{server_name}.{tool_name} {body}")
        if kind is ActionKind.NET:
            return Action(kind=kind, tool=name, url=f"mcp://{server_name}/{tool_name}")
        if kind is ActionKind.WRITE:
            return Action(kind=kind, tool=name, content=body)
        return Action(kind=kind, tool=name)

    def run(args: dict, ctx: S.ToolContext) -> S.ToolResult:
        text = _run(asyncio.wait_for(_call_tool(server, tool_name, dict(args)),
                                     timeout=_timeout_of(server)))
        return S.ToolResult(content=text)

    schema = tool.args_schema if isinstance(tool.args_schema, dict) else {"type": "object"}
    return S.ToolSpec(
        name=name,
        description=str(tool.description or ""),
        args_schema=schema,
        effects=effects,
        to_action=to_action,
        run=run,
        idempotent=idempotent,
        needs_approval=needs_approval,
    )


def load_mcp_tools(cfg: Mapping[str, Any] | None,
                   on_advisory: Callable[[McpAdvisory], None] | None = None,
                   ) -> list[S.ToolSpec]:
    """Start each configured server, list its tools, and wrap them as ToolSpecs.

    Never raises for a bad server or a bad tool entry. Each problem becomes one
    ``McpAdvisory`` passed to ``on_advisory`` (ignored when None), and that
    server or tool contributes no ToolSpec.
    """
    report = on_advisory or (lambda advisory: None)
    servers = (cfg or {}).get("servers") or {}
    specs: list[S.ToolSpec] = []
    for server_name, server in servers.items():
        server_name = str(server_name)
        try:
            if not isinstance(server, Mapping) or not server.get("command"):
                raise ValueError("server entry needs a command")
            timeout = _timeout_of(server)
            tools = _run(asyncio.wait_for(_list_tools(server), timeout=timeout))
        except Exception as exc:  # any startup failure is an advisory
            report(McpAdvisory(
                id=f"mcp:{server_name}",
                detail=f"MCP server {server_name!r} did not start or list tools: "
                       f"{type(exc).__name__}: {str(exc)[:200]}"))
            continue
        for tool in tools:
            try:
                specs.append(_make_spec(server_name, server, tool))
            except (ValueError, TypeError) as exc:
                report(McpAdvisory(
                    id=f"mcp:{server_name}:{tool.name}",
                    detail=f"MCP tool {tool.name!r} from {server_name!r} refused: {exc}"))
    return specs
