"""The agent loop as the native work actor (plan sections 1.10, 1.11; brief A7).

``NativeActor.run(goal, findings, context)`` is one work step for one gate:

1. the toolbox holds the packs' tool grants (``core.kernel.packs.load_packs``) and the
   harness tools, each run through ``core.tools.registry.Toolbox`` in the unit directory.
   A granted tool that cannot be built (no exec allow-list, no search backend, no pack
   implementation) is not held; the step names it and the reason, in the prompt and in
   the summary;
2. the agent is ``langchain.agents.create_agent`` with the middleware list of section 1.11:
   ``ToolErrorMiddleware`` (refusals to the model), ``GuardMiddleware`` (decide, then scan
   what was touched), ``BudgetMiddleware`` (cost, iteration and wall-clock backstops),
   ``ModelRetryMiddleware`` (infrastructure faults only), and the model-call and tool-call
   ceilings from the budget;
3. the loop ends when the model calls ``request_gate`` (a ``return_direct`` tool), or at a
   ceiling, or when the model replies without a tool call.
4. a tool that needs a human (``needs_approval``, or an ``ask`` verdict) pauses the agent. The
   agent runs as a subgraph of the caller's graph (the unit's work node), so the pause is the
   unit's own interrupt: ``run`` re-raises it as a GraphInterrupt, and a proceed continues the
   same tool call.

The result is a ``WorkResult`` (amendment 5): ``finished`` and ``gate`` come from the
``request_gate`` tool message, and ``summary`` is a fixed phrase. Message text never leaves
this module, so it never reaches UnitState.

``register_native(actor)`` registers the actor under ``native``, the name the work node looks
up. The actor is built per unit (it needs the unit directory and the model), so a process
that runs several units registers one actor per unit under distinct names.

Only this module and core/agent/middleware.py import ``langchain.agents``. No decision is made
here: every allow, deny, ask and budget stop comes from the kernel through the middleware.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from langchain.agents import create_agent
from langchain.agents.middleware import (ModelCallLimitMiddleware, ModelRetryMiddleware,
                                         ToolCallLimitMiddleware, ToolErrorMiddleware)
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphInterrupt, GraphRecursionError

from core.agent.middleware import BudgetMiddleware, GuardMiddleware, approval_granted
from core.graph.contracts import WorkResult
from core.graph.nodes.work import register_work_actor
from core.kernel import packs as kpacks
from core.kernel.budget import refuse_unbounded
from core.kernel.faults import is_retryable
from core.kernel.types import Budget, Spend
from core.tools.registry import ToolRefused, Toolbox, build_specs
from core.tools.spec import ToolContext, ToolSpec

NATIVE_NAME = "native"
REQUEST_GATE = "request_gate"
# Harness tools touch no workspace file, so every native agent holds them.
HARNESS_TOOLS = frozenset({"gate_status", REQUEST_GATE, "note_decision", "commit_fact",
                           "escalate"})

_SYSTEM = (
    "You are the work step of one unit of a gated workflow. Make the named gate pass by "
    "changing files in the unit directory with the tools you hold. Policy outranks the task: "
    "a refused action stays refused, so repair the content instead. When the gate is ready, "
    "call request_gate with the gate id. Do not reply without calling request_gate."
)


class MemoryJournal:
    """An effects journal for one run: a non-idempotent tool's result, keyed by call id."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def seen(self, key: str):
        return self._store.get(key)

    def record(self, key: str, result) -> None:
        self._store[key] = result


def _refusal_text(exc: BaseException) -> str | None:
    """Show a toolbox refusal to the model. Any other exception propagates (infrastructure)."""
    if isinstance(exc, ToolRefused):
        return f"refused: {exc}"
    return None


def _with_gaps(summary: str, gaps: list[tuple[str, str]]) -> str:
    """The summary, with the unavailable granted tools named when there are any."""
    if not gaps:
        return summary
    return summary + "; unavailable: " + ", ".join(f"{name} ({reason})" for name, reason in gaps)


def _task(goal: str, findings: list, gate: str | None, gaps: list) -> str:
    lines = [f"Gate: {gate}", f"Goal: {goal}"]
    if gaps:
        lines.append("Granted but unavailable (do not plan on them): "
                     + "; ".join(f"{name} ({reason})" for name, reason in gaps))
    if findings:
        lines.append("Findings from the last attempt, to fix:")
        for finding in findings:
            if isinstance(finding, Mapping):
                lines.append(f"- [{finding.get('rule')}] {finding.get('message')}")
    return "\n".join(lines)


def _lc_tool(spec: ToolSpec, toolbox: Toolbox) -> StructuredTool:
    """The LangChain tool for one spec. Its body is the toolbox call, and it returns
    (content, touched) so GuardMiddleware can scan exactly the paths the call changed."""

    def body(runtime: ToolRuntime, **kwargs):
        thread = runtime.config.get("configurable", {}).get("thread_id", "")
        try:
            result = toolbox.call(spec.name, kwargs, thread_id=thread,
                                  tool_call_id=runtime.tool_call_id,
                                  approved=approval_granted())
        except ToolRefused as exc:
            return f"refused: {exc}", []
        return result.content, list(result.touched)

    return StructuredTool.from_function(
        func=body, name=spec.name, description=spec.description,
        args_schema=spec.args_schema, response_format="content_and_artifact",
        return_direct=spec.name == REQUEST_GATE)


def _gate_request(messages: list) -> str | None:
    """The gate named by the first successful request_gate tool message, or None."""
    for message in messages:
        if not (isinstance(message, ToolMessage) and message.name == REQUEST_GATE):
            continue
        if message.status == "error":
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if (isinstance(payload, dict) and payload.get("work_step") == "finished"
                and isinstance(payload.get("gate"), str)):
            return payload["gate"]
    return None


class NativeActor:
    """The native work actor. ``run`` satisfies ``core.graph.contracts.WorkActor``.

    ``config`` is a core.kernel.config.Config (its root, packs, deny, ask, protected_paths
    and blind). ``model`` is a model port with ``chat_model()`` (FakeModel in tests).
    ``unit_dir`` is the unit directory, inside ``config.root``. ``state_dir`` holds the
    runner's files; the scratch directory is ``state_dir/scratch``. ``budget`` must be
    bounded. ``cost`` is an optional zero-argument callable returning the USD spent so
    far; ``clock`` is the monotonic clock for elapsed time.

    The tool configuration is passed to ``core.tools.registry.build_specs``:
    ``exec_policy`` (run_command), ``net_config`` (fetch_url, web_search), ``doc_config``
    (extract_text, render_document) and ``pack_tools`` (name to ToolSpec factory, e.g.
    ``{"apply_plan": apply_spec}`` for the ops pack). A tool with no configuration is a gap.
    """

    def __init__(self, config: Any, model: Any, *, unit_dir, state_dir, budget: Budget,
                 cost: Callable[[], float] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 exec_policy: Any = None, net_config: Mapping | None = None,
                 doc_config: Mapping | None = None, pack_tools: Mapping | None = None):
        refuse_unbounded(budget)
        self._config = config
        self._model = model
        self._exec_policy = exec_policy
        self._net_config = net_config
        self._doc_config = doc_config
        self._pack_tools = dict(pack_tools or {})
        self._root = Path(config.root).resolve()
        self._unit_dir = Path(unit_dir).resolve()
        self._scratch = Path(state_dir).resolve() / "scratch"
        self._budget = budget
        self._cost = cost
        self._clock = clock

    def grants(self) -> frozenset[str]:
        """Tool names this actor may hold: the harness tools and the packs' tools.
        A pack may name any built-in tool or any name in ``pack_tools``."""
        known = set(kpacks.BUILTIN_TOOLS) | set(self._pack_tools)
        names = set(HARNESS_TOOLS)
        for pack in kpacks.load_packs(self._config, known_tools=known):
            names.update(pack.tools)
        return frozenset(names)

    def _toolbox(self) -> tuple[Toolbox, list[tuple[str, str]]]:
        """The toolbox for one step, and the granted tools it could not build (name, reason)."""
        specs, gaps = build_specs(self.grants(), exec_policy=self._exec_policy,
                                  net_config=self._net_config, doc_config=self._doc_config,
                                  pack_tools=self._pack_tools)
        unit_rel = self._unit_dir.relative_to(self._root).as_posix()
        context = ToolContext(root=self._root, unit_dir=self._unit_dir,
                              scratch_dir=self._scratch, writable=(f"{unit_rel}/**",),
                              blind=tuple(self._config.blind), budget=self._budget,
                              journal=MemoryJournal())
        return Toolbox(specs, context), gaps

    def _middleware(self, toolbox: Toolbox, budget_mw: BudgetMiddleware) -> list:
        middleware: list = [
            ToolErrorMiddleware(on_error=_refusal_text),
            GuardMiddleware(toolbox, self._config),
            budget_mw,
            ModelRetryMiddleware(max_retries=2, retry_on=is_retryable, on_failure="error"),
        ]
        if self._budget.max_model_calls is not None:
            middleware.append(ModelCallLimitMiddleware(run_limit=self._budget.max_model_calls,
                                                       exit_behavior="end"))
        if self._budget.max_tool_calls is not None:
            middleware.append(ToolCallLimitMiddleware(run_limit=self._budget.max_tool_calls,
                                                      exit_behavior="end"))
        return middleware

    def _recursion_limit(self) -> int:
        """A step ceiling for LangGraph's recursion limit: four graph steps per call of the
        largest bounded count (a model step and a tool step, with slack), plus a constant.
        Hitting it is an unfinished result, not a crash."""
        counts = [n for n in (self._budget.max_model_calls, self._budget.max_tool_calls,
                              self._budget.max_iterations) if n is not None]
        return 2 * (max(counts) if counts else 50) * 2 + 10

    def _unfinished_summary(self, messages: list, spend: Spend, stop_reason: str | None) -> str:
        budget = self._budget
        if budget.max_model_calls is not None and spend.model_calls >= budget.max_model_calls:
            return "agent stopped: model-call ceiling reached"
        if budget.max_tool_calls is not None and spend.tool_calls >= budget.max_tool_calls:
            return "agent stopped: tool-call ceiling reached"
        if stop_reason:
            return f"agent stopped: {stop_reason}"
        if not any(isinstance(m, AIMessage) for m in messages):
            return "agent stopped before any model reply"
        return "agent ended without calling request_gate"

    def run(self, goal: str, findings: list[dict], context: Mapping[str, Any]) -> WorkResult:
        gate = context.get("gate") if isinstance(context, Mapping) else None
        toolbox, gaps = self._toolbox()
        tools = [_lc_tool(toolbox.get(name), toolbox) for name in toolbox.names()]
        budget_mw = BudgetMiddleware(self._budget, cost=self._cost, clock=self._clock)
        agent = create_agent(self._model.chat_model(), tools, system_prompt=_SYSTEM,
                             middleware=self._middleware(toolbox, budget_mw))
        task = _task(goal, list(findings), gate, gaps)
        request = {"messages": [{"role": "user", "content": task}]}
        # No thread_id: the agent runs as a subgraph of the caller's graph (the work node).
        # It inherits the caller's thread and checkpointer, so a checkpoint of its own steps
        # lives with the unit, and a proceed continues the same tool call (Q4).
        config = {"recursion_limit": self._recursion_limit()}
        try:
            out = agent.invoke(request, config)
        except GraphRecursionError:
            spend = budget_mw._spend()
            return WorkResult(summary=_with_gaps("agent stopped: step ceiling reached", gaps),
                              spend=spend, finished=False, gate=None)
        if out.get("__interrupt__"):
            # GuardMiddleware asked a human (an ask verdict or needs_approval). The pause is
            # re-raised, never reported as a result: the caller's graph pauses on it, and a
            # proceed runs this step again from the top with the same agent checkpoint.
            raise GraphInterrupt(tuple(out["__interrupt__"]))
        messages = list(out.get("messages", []))
        spend = budget_mw._spend()
        requested = _gate_request(messages)
        if requested is not None:
            return WorkResult(summary=_with_gaps(f"request_gate: gate {requested}", gaps),
                              spend=spend, finished=True, gate=requested)
        summary = self._unfinished_summary(messages, spend, budget_mw.stop_reason)
        return WorkResult(summary=_with_gaps(summary, gaps), spend=spend,
                          finished=False, gate=None)


def register_native(actor: NativeActor, name: str = NATIVE_NAME) -> None:
    """Register the actor under ``name`` (default ``native``) for the work node to find."""
    register_work_actor(name, actor)


__all__ = ["HARNESS_TOOLS", "MemoryJournal", "NATIVE_NAME", "NativeActor", "register_native"]
