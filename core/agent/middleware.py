"""Guard and budget middleware for the LangChain agent (plan sections 1.9, 1.11, 1.12; brief A5).

This module translates. Every decision comes from the kernel:

* ``GuardMiddleware`` asks ``core.kernel.guard.decide`` before a tool body runs, and
  scans the touched files with ``core.kernel.policy.scan_path`` afterwards.
* ``BudgetMiddleware`` asks ``core.kernel.budget.should_stop`` before each model call.

Tool contract the guard relies on: the tool message's ``artifact`` is the list of
workspace-relative paths the call touched (``ToolResult.touched``). A write or edit
whose result carries no such list is refused with a RuntimeError, because the guard
cannot scan what it cannot see.

``wrap_tool_call`` for an ``ask`` verdict calls ``interrupt()`` before the handler.
A tool whose ``ToolSpec.needs_approval`` is True is gated the same way, even when the
guard allows the call. A tool body must never interrupt itself (``core.tools.spec``):
on proceed the middleware runs again from the top, so the only side effect before the
interrupt is the pure decision. The proceed value is ``"allow"`` (or
``{"answer": "allow", ...}``); anything else refuses, so the guard fails closed.

``approval_granted()`` is True only while the handler runs after the gate has passed.
The LangChain tool body passes it to ``Toolbox.call(approved=...)``, so a
``needs_approval`` tool cannot run without this middleware's gate.

Standard library, LangChain, LangGraph, and ``core.kernel`` / ``core.tools`` only.
"""
from __future__ import annotations

import contextvars
import dataclasses
import time
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import ToolMessage
from langgraph.types import interrupt

from core.kernel import budget as kernel_budget
from core.kernel import guard
from core.kernel import policy
from core.kernel.types import (Action, ActionKind, Budget, Decision, HumanAnswer,
                               Severity, Spend, Verdict)
from core.tools.registry import ToolRefused, Toolbox

_WRITE_KINDS = (ActionKind.WRITE, ActionKind.EDIT)

_APPROVED: contextvars.ContextVar[bool] = contextvars.ContextVar("guard_approved",
                                                                 default=False)


def approval_granted() -> bool:
    """True only inside the handler call of a tool call that passed the gate."""
    return _APPROVED.get()


def _denial(name: str, tool_call_id: str, decision: Decision) -> ToolMessage:
    return ToolMessage(content=guard.denial_message(decision), tool_call_id=tool_call_id,
                       name=name, status="error")


def _refused(rule: str, reason: str) -> Decision:
    return Decision(verdict=Verdict.DENY, rule=rule, reason=reason,
                    remedy=None, findings=())


def _answered_allow(answer: Any) -> bool:
    if isinstance(answer, dict):
        answer = answer.get("answer")
    return answer == HumanAnswer.ALLOW.value


def _advice_lines(findings) -> list[str]:
    return [f"Advice: [{f.rule}] line {f.line}: {f.message}" for f in findings]


class GuardMiddleware(AgentMiddleware):
    """Decides every tool call, and scans what each allowed call touched.

    ``toolbox`` supplies the ToolSpec for a name (``get``), and ``cfg`` is the
    configuration object ``guard.decide`` reads. The workspace root is ``cfg.root``.
    """

    def __init__(self, toolbox: Toolbox, cfg: Any):
        super().__init__()
        self._toolbox = toolbox
        self._cfg = cfg
        self._root = Path(cfg.root).resolve()
        try:
            self._policies = tuple(policy.compile_policies(cfg))
        except Exception:
            # decide() already denies every action when the policies do not compile
            # (config-unloadable), so nothing reaches the post-write scan with them.
            self._policies = ()

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        name, tool_call_id = call["name"], call["id"]
        args = call.get("args") or {}
        try:
            spec = self._toolbox.get(name)
        except ToolRefused as exc:
            return _denial(name, tool_call_id, _refused("unknown-tool", str(exc)))
        try:
            action = spec.action_for(args)
        except (TypeError, ValueError, KeyError) as exc:
            return _denial(name, tool_call_id, _refused("tool-contract", str(exc)))

        decision = guard.decide(action, self._cfg)
        if decision.verdict is Verdict.DENY:
            return _denial(name, tool_call_id, decision)
        # An ask verdict and a needs_approval tool take the same path: a human answers
        # before the body runs, whatever the guard said.
        if decision.verdict is Verdict.ASK or spec.needs_approval:
            reason = decision.reason or f"{name} needs a human approval before it runs"
            answer = interrupt({"kind": "ask", "tool": name, "path": action.path,
                                "tool_call_id": tool_call_id,
                                "reason": reason, "remedy": decision.remedy})
            if not _answered_allow(answer):
                refusal = dataclasses.replace(decision, verdict=Verdict.DENY,
                                              rule="ask-path",
                                              reason="a human refused this action")
                return _denial(name, tool_call_id, refusal)

        token = _APPROVED.set(True)
        try:
            response = handler(request)
        finally:
            _APPROVED.reset(token)
        return self._after(action, response, name, decision)

    def _after(self, action: Action, response, name: str, decision: Decision):
        touched = None
        if isinstance(response, ToolMessage):
            artifact = response.artifact
            if isinstance(artifact, (list, tuple)) and all(isinstance(p, str) for p in artifact):
                touched = tuple(artifact)
        if touched is None:
            if action.kind in _WRITE_KINDS:
                raise RuntimeError(
                    f"{name}: a write returned no touched list, so the post-write scan "
                    f"cannot run; the tool must return touched paths as its artifact")
            return response

        lines = _advice_lines(decision.findings)
        for rel in touched:
            path = (self._root / rel).resolve()
            found = policy.scan_path(path, self._root, list(self._policies)).findings
            for finding in found:
                if finding.severity is Severity.FATAL:
                    lines.append(f"Post-write scan: [{finding.rule}] {finding.path} "
                                 f"line {finding.line}: {finding.message}")
        if not lines or not isinstance(response, ToolMessage):
            return response
        content = response.content if isinstance(response.content, str) else str(response.content)
        return response.model_copy(update={"content": "\n".join([content, *lines])})


class BudgetMiddleware(AgentMiddleware):
    """Ends the run when a backstop is reached (``kernel.budget.should_stop``).

    One instance covers one run. ``cost`` is a zero-argument callable returning the
    USD spent so far (the caller owns pricing); without it cost is 0. Elapsed time is
    ``elapsed_before_s`` plus ``clock()`` since construction, so a proceeded run can carry
    its earlier time (bug B19). The refusal of an unbounded budget is the kernel's.
    """

    def __init__(self, bounds: Budget, *, cost: Callable[[], float] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 elapsed_before_s: float = 0.0):
        super().__init__()
        kernel_budget.refuse_unbounded(bounds)
        self._bounds = bounds
        self._cost = cost
        self._clock = clock
        self._started = clock()
        self._elapsed_before = float(elapsed_before_s)
        self._model_calls = 0
        self._tool_calls = 0
        self._iterations = 0
        self.stop_reason: str | None = None

    def _spend(self) -> Spend:
        return Spend(
            usd=float(self._cost()) if self._cost is not None else 0.0,
            model_calls=self._model_calls,
            tool_calls=self._tool_calls,
            iterations=self._iterations,
            elapsed_s=self._elapsed_before + (self._clock() - self._started),
        )

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        self._iterations += 1
        stop = kernel_budget.should_stop(self._spend(), self._bounds)
        if stop.stop:
            self.stop_reason = stop.reason
            return {"jump_to": "end"}
        return None

    def wrap_model_call(self, request, handler):
        self._model_calls += 1
        return handler(request)

    def wrap_tool_call(self, request, handler):
        self._tool_calls += 1
        return handler(request)
