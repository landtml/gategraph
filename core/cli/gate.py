#!/usr/bin/env python3
"""gate: the command layer over the unit graph (plan sections 1.8, 1.12, 1.15; brief G8).

Parse, call the runner, print. No gate logic lives here; the graph decides, the runner locks and
invokes, and the projection writes files. What this layer adds:

* Every answer is checked against the pending request before it is proceeded (section 1.12): the
  kind's answers, the answers the request offers, the reject-needs-note rule. A value that reaches
  the graph is replayed on every later proceed, so a refused answer never gets there.
* Approval channels (section 1.12). ``tty``: the answer must come from a terminal on standard
  input. ``token``: the answer must carry the one-time token printed when the request was raised;
  the request holds only its sha256, and the token is never written anywhere.
* Projections (manifest.yaml and evidence.jsonl, section 1.8, copies 2 and 3) are written from
  ``get_state`` after every run returns, interrupts included.
* Every mutating verb arms the session first (B32). Every refusal prints the command to run next
  and writes one row to the failure ledger.

Verbs: start, status, attempt, pending, proceed, pause, abandon, escalate, verify, archive.

Standard library, PyYAML, LangGraph and core only. The product name is not written here; see
core/brand.py.
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import hmac
import os
import secrets
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
from langgraph.types import Command  # noqa: E402

from core import brand  # noqa: E402
from core.graph import runner  # noqa: E402
from core.graph.store import make_checkpointer  # noqa: E402
from core.graph.unit import build_unit_graph, initial_state  # noqa: E402
from core.hosts import session  # noqa: E402
from core.kernel import config, ledger, locks, paths, projection, surface, verify  # noqa: E402
from core.kernel.evidence import ChainError  # noqa: E402
from core.kernel.surface import SurfaceMissing  # noqa: E402
from core.kernel.types import HUMAN_ANSWERS, Budget, HumanKind  # noqa: E402

PROG = "gate"
TERMINAL = ("complete", "escalated", "abandoned")
MUTATING = frozenset({"start", "attempt", "proceed", "pause", "abandon", "escalate", "archive"})
TOKEN_LINE = "approval token (shown once, not stored): "


def new_token() -> str:
    """A one-time approval token: 36 lowercase hex digits. Hex never starts with '-', so
    `--token <tok>` always parses (argparse reads a leading '-' as an option; a urlsafe
    token did so about once in 64 tries)."""
    return secrets.token_hex(18)


# Bounds for one unit. The adapter has no budget key yet, so every command uses these.
DEFAULT_BUDGET = Budget(max_iterations=200, max_model_calls=100)
DEFAULT_REPAIR_ATTEMPTS = 3


class Refused(Exception):
    """A command that cannot do what was asked. ``next_step`` is the command to run instead."""

    def __init__(self, message: str, next_step: str, **fields):
        super().__init__(message)
        self.message = message
        self.next_step = next_step
        self.fields = fields


@dataclass
class Ctx:
    root: Path
    cfg: config.Config
    unit: str | None
    session_id: str | None
    channel: str = "tty"
    token: str | None = None
    token_digest: str | None = None
    take_active: bool = False

    @property
    def unit_dir(self) -> Path:
        return paths.unit_path(self.root, self.cfg.unit_dir, self.unit)

    @property
    def state_dir(self) -> Path:
        return paths.state_dir(self.root)

    @property
    def lock_dir(self) -> Path:
        return self.state_dir / "locks"

    @property
    def thread(self) -> str:
        return f"{self.cfg.project}/{self.unit}"


# --------------------------------------------------------------------------
# Graph, snapshots, projections
# --------------------------------------------------------------------------

def _channel(cfg: config.Config) -> str:
    """The one channel of the adapter's approval gates (the graph takes a single channel)."""
    channels = {gate.approval.channel for gate in cfg.gates if gate.approval is not None}
    if len(channels) > 1:
        raise Refused(f"approval gates name different channels ({', '.join(sorted(channels))}); "
                      "the graph takes one channel per adapter",
                      f"edit {config.ADAPTER_REL} so every approval gate names one channel, "
                      f"then run: {PROG} status --unit <slug>")
    return channels.pop() if channels else "tty"


def _recursion_limit(cfg: config.Config) -> int:
    """Plan 1.15: bounded by the gate count and the repair budgets, not the library default."""
    steps = sum((gate.repair.max_attempts if gate.repair else DEFAULT_REPAIR_ATTEMPTS) + 1
                for gate in cfg.gates)
    return 60 + 30 * max(1, steps)


def _graph(ctx: Ctx):
    """The compiled unit graph for this process. The checkpointer is SQLite under the state dir."""
    return build_unit_graph({
        "config": ctx.cfg,
        "unit_dir": ctx.unit_dir,
        "state_dir": ctx.state_dir,
        "config_digest": config.config_digest(ctx.cfg),
        "budget": DEFAULT_BUDGET,
        "checkpointer": make_checkpointer({"root": ctx.root}),
        "channel": ctx.channel,
        "token_digest": ctx.token_digest,
        "model_ports": {},
    })


def _snapshot(graph, thread: str):
    return graph.get_state({"configurable": {"thread_id": thread}})


def _kind_of(item) -> str | None:
    return item.get("kind") if isinstance(item, dict) else None


def _paused_here(ctx: Ctx, snapshot) -> bool:
    """True when ``gate pause`` wrote ``paused`` at this checkpoint. The record holds only until a
    run moves the checkpoint: attempt and proceed project ``in_progress`` again."""
    try:
        data = projection.read_manifest(ctx.unit_dir).data
    except projection.ManifestError:
        return False
    checkpoint = ((snapshot.config or {}).get("configurable") or {}).get("checkpoint_id")
    return (bool(checkpoint) and data.get("status") == "paused"
            and data.get("checkpoint_id") == checkpoint)


def _status_of(ctx: Ctx, snapshot) -> str:
    """The status a projection shows (plan 1.12, 1.16; ruling on the stop hook).

    A unit waiting at a work request is ``in_progress``: the external actor does the work and runs
    ``gate attempt``, and the stop hook holds the session until it does. A unit waiting at an
    approval, an escalation or an ask is ``paused``: a person is needed, and the stop hook lets the
    session end. A unit that ``gate pause`` left stopped at its work request stays ``paused``.
    """
    waiting = [item.value for task in snapshot.tasks for item in task.interrupts]
    if waiting and any(_kind_of(item) != HumanKind.WORK.value for item in waiting):
        return "paused"
    if waiting and _paused_here(ctx, snapshot):
        return "paused"
    return str((snapshot.values or {}).get("status") or "in_progress")


def _sync_active(ctx: Ctx, status: str) -> None:
    """The ``.active`` pointer names the unit being worked on; a terminal unit releases it."""
    pointer = session.active_path(ctx.root, ctx.cfg)
    current = session.read_active(ctx.root, ctx.cfg)
    if status in TERMINAL:
        if current and current.get("slug") == ctx.unit:
            pointer.unlink(missing_ok=True)
        return
    if ctx.take_active:
        paths.atomic_write(pointer, yaml.safe_dump(
            {"slug": ctx.unit, "session_id": ctx.session_id}, sort_keys=True))


def _project(ctx: Ctx, snapshot, status: str | None = None) -> None:
    """Write copies 2 and 3 (evidence.jsonl, manifest.yaml) from one get_state result (1.8).
    ``status`` is given only by ``gate pause``; every other run projects what the graph says."""
    values = dict(snapshot.values or {})
    if not values:
        return
    checkpoint_id = ((snapshot.config or {}).get("configurable") or {}).get("checkpoint_id")
    status = status or _status_of(ctx, snapshot)
    projection.write_projections(
        ctx.unit_dir, ctx.cfg, values.get("evidence") or [],
        thread_id=ctx.thread, status=status,
        surfaces=functools.partial(surface.load_surface, ctx.state_dir),
        config_digest=values.get("config_digest") or config.config_digest(ctx.cfg),
        checkpoint_id=checkpoint_id)
    _sync_active(ctx, status)


def _run(ctx: Ctx, graph, payload) -> None:
    """Invoke or proceed under the thread lock. The projection is written before the lock is
    released, whether the run returned, paused at an interrupt, or raised (runner.run calls
    ``on_return`` in every case)."""
    failed: dict = {}

    def on_return(snapshot):
        try:
            _project(ctx, snapshot)
        except Exception as exc:  # the run's own outcome must not be hidden by this
            failed["projection"] = exc

    try:
        runner.run(graph, ctx.thread, payload, on_return,
                   recursion_limit=_recursion_limit(ctx.cfg), lock_dir=ctx.lock_dir,
                   owner=f"{PROG} {ctx.unit}")
    except locks.LockHeld as exc:
        raise Refused(f"unit {ctx.unit} is being changed by another gate command ({exc}).",
                      f"wait for it to finish, then run: {PROG} status --unit {ctx.unit}") from None
    if "projection" in failed:
        raise Refused(f"the run finished but the projections could not be written: "
                      f"{failed['projection']}", f"{PROG} verify --unit {ctx.unit}")


@contextlib.contextmanager
def _locked(ctx: Ctx):
    """The thread lock, for work that writes projections without running the graph."""
    try:
        with locks.file_lock(runner.lock_path(ctx.lock_dir, ctx.thread),
                             owner=f"{PROG} {ctx.unit}"):
            yield
    except locks.LockHeld as exc:
        raise Refused(f"unit {ctx.unit} is being changed by another gate command ({exc}).",
                      f"wait for it to finish, then run: {PROG} status --unit {ctx.unit}") from None


# --------------------------------------------------------------------------
# Requests: what is open, what answers it, how to say so
# --------------------------------------------------------------------------

def _next_for(ctx: Ctx, item: dict) -> str:
    kind, gate_id = item.get("kind"), item.get("gate")
    if kind == "work":
        return f"{PROG} attempt {gate_id} --unit {ctx.unit}"
    if kind == "approval":
        token = " --token <token>" if item.get("channel") == "token" else ""
        return (f"{PROG} proceed --unit {ctx.unit} --decision approve|reject --by <name> "
                f"[--note <text>]{token}")
    if kind == "escalation":
        return (f"{PROG} proceed --unit {ctx.unit} --decision retry|abandon --by <name> "
                f"[--note <text>]")
    return f"{PROG} pending --unit {ctx.unit}"


def _report(ctx: Ctx, graph, message: str) -> int:
    """Print the outcome, the open requests, and the command to run next. Shows the approval
    token only in the process that raised its request (only that process knows it)."""
    items = runner.pending(graph, ctx.thread)
    print(message)
    for item in items:
        print(f"  {item.get('id')}  {item.get('kind')}  gate={item.get('gate')}  "
              f"channel={item.get('channel')}  {item.get('summary')}")
        if (ctx.token and item.get("channel") == "token"
                and item.get("token_digest") == ctx.token_digest):
            print(f"  {TOKEN_LINE}{ctx.token}")
        print(f"  next: {_next_for(ctx, item)}")
    if not items:
        print(f"  next: {PROG} status --unit {ctx.unit}")
    return 0


def _print_findings(findings) -> None:
    """One block per finding: rule, severity, path:line, message, and the remedy (if any)."""
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        path, line = finding.get("path"), finding.get("line")
        where = (f"{path}:{line}" if path and line is not None
                 else (path or "(no location)"))
        print(f"  finding {finding.get('rule')} ({finding.get('severity')}) {where}: "
              f"{finding.get('message')}")
        print(f"    remedy: {finding.get('remedy') or 'none (the check gave none)'}")


def _attempt_outcome(ctx: Ctx, graph, gate_id: str) -> int:
    """What one ``gate attempt`` ended with, and its exit code.

    Passed: exit 0. Checks passed and an approval waits: exit 0 (a person answers it next). Failed
    with a work request re-issued: the findings, the next attempt, exit 1. Escalated: why, the
    proceed command, exit 1.
    """
    values = _snapshot(graph, ctx.thread).values or {}
    if gate_id in (values.get("effective") or []):
        return _report(ctx, graph, f"gate {gate_id} passed for {ctx.unit}.")
    items = runner.pending(graph, ctx.thread)
    first = items[0] if items else None
    kind = first.get("kind") if first else None
    if kind == HumanKind.APPROVAL.value:
        return _report(ctx, graph, f"gate {gate_id}: the checks passed for {ctx.unit}; it waits "
                                   "for an approval.")
    payload = (first or {}).get("payload") or {}
    if kind == HumanKind.ESCALATION.value:
        print(f"gate {gate_id} did not pass for {ctx.unit}: the unit escalated. "
              f"{first.get('summary')}.")
        print(f"  why: {payload.get('reason') or 'no reason recorded'}")
        _print_findings(payload.get("findings"))
        print(f"  next: {_next_for(ctx, first)}")
        return 1
    findings = payload.get("findings") if first else values.get("findings")
    print(f"gate {gate_id} did not pass for {ctx.unit}: {len(findings or [])} finding(s).")
    _print_findings(findings)
    print(f"  next: {_next_for(ctx, first) if first else f'{PROG} status --unit {ctx.unit}'}")
    return 1


def _open_unit(ctx: Ctx):
    """The graph and the snapshot of a unit that has a thread. Never creates one (B39)."""
    graph = _graph(ctx)
    snapshot = _snapshot(graph, ctx.thread)
    if not snapshot.values:
        raise Refused(f"no unit named {ctx.unit} has been started, so there is nothing to answer",
                      f"{PROG} start --unit {ctx.unit}")
    return graph, snapshot


def _authorise(ctx: Ctx, item: dict, token: str | None, decision: str) -> str:
    """The channel check of section 1.12. Returns the channel the answer is recorded with."""
    kind, channel = item.get("kind"), item.get("channel")
    if kind not in ("approval", "escalation"):
        return "command"  # work is answered by the external actor (channel tty is not enforced)
    if channel == "tty":
        if not sys.stdin.isatty():
            raise Refused(
                f"this {kind} request's channel is tty: the answer must be given from a terminal, "
                "and standard input here is not one. Run the same command in a terminal.",
                f"{PROG} proceed --unit {ctx.unit} --decision {decision} --by <name>"
                + (" --note <text>" if decision != "attempt" else ""))
        return "tty"
    if channel == "token":
        if not token:
            raise Refused(f"this {kind} request's channel is token: give the one-time token "
                          "printed when the request was raised, with --token.",
                          _next_for(ctx, item))
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not item.get("token_digest") or not hmac.compare_digest(digest, item["token_digest"]):
            raise Refused(f"the token does not match this {kind} request. Use the token printed "
                          "when the request was raised, by the command that raised it.",
                          _next_for(ctx, item))
        return "token"
    raise Refused(f"this request names an unknown channel {channel!r}; the adapter is wrong.",
                  f"{PROG} status --unit {ctx.unit}")


def _answer(ctx: Ctx, graph, *, decision: str, by: str | None, note: str | None,
            token: str | None = None, attempt_gate: str | None = None) -> int:
    """Validate one answer against the open request, then proceed the graph with it.

    Everything that would make the graph refuse is checked here, before the proceed. See the
    module docstring and section 1.12. ``attempt_gate`` names the gate of a ``gate attempt``: its
    outcome decides the exit code (see _attempt_outcome).
    """
    items = runner.pending(graph, ctx.thread)
    if not items:
        status = _status_of(ctx, _snapshot(graph, ctx.thread))
        raise Refused(f"unit {ctx.unit} has no open request (status {status}), so there is "
                      "nothing to answer", f"{PROG} status --unit {ctx.unit}")
    if len(items) > 1:
        raise Refused(f"unit {ctx.unit} has {len(items)} open requests; this command answers one "
                      "at a time", f"{PROG} pending --unit {ctx.unit}")
    item = items[0]
    kind = item.get("kind")
    try:
        human_kind = HumanKind(kind)
    except ValueError:
        raise Refused(f"unknown request kind {kind!r}", f"{PROG} pending --unit {ctx.unit}") from None
    kind_answers = [answer.value for answer in HUMAN_ANSWERS[human_kind]]
    offered = (item.get("payload") or {}).get("answers")
    allowed = [a for a in (offered or kind_answers) if a in kind_answers]
    example = f"{PROG} proceed --unit {ctx.unit} --decision {decision} --by <name>"
    if decision not in allowed:
        raise Refused(f"answer {decision!r} is not allowed for the open {kind} request; "
                      f"expected one of: {', '.join(allowed)}", _next_for(ctx, item))
    if not by or not str(by).strip():
        raise Refused("an answer needs --by NAME (who is answering)", f"{example} [--note <text>]")
    if decision == "reject" and not (note or "").strip():
        raise Refused("reject needs a note: say why with --note",
                      f"{example} --note <why it is rejected>")
    channel = _authorise(ctx, item, token, decision)
    fields = {"answer": decision, "by": str(by).strip(), "channel": channel,
              "note": note if note else None}
    _run(ctx, graph, Command(resume=fields))
    if attempt_gate is not None:
        return _attempt_outcome(ctx, graph, attempt_gate)
    return _report(ctx, graph, f"answered the open {kind} request {item.get('id')} with {decision}.")


def _waiting(items: list[dict]) -> str:
    if not items:
        return "not waiting at an interrupt"
    first = items[0]
    return f"waiting at {first.get('kind')} for gate {first.get('gate')}"


# --------------------------------------------------------------------------
# Verbs
# --------------------------------------------------------------------------

def cmd_start(ctx: Ctx, args) -> int:
    graph = _graph(ctx)
    snapshot = _snapshot(graph, ctx.thread)
    ctx.take_active = True
    if snapshot.values:
        status = _status_of(ctx, snapshot)
        if status in TERMINAL:
            raise Refused(f"unit {ctx.unit} is {status} (terminal); a terminal unit is not restarted",
                          f"{PROG} start --unit <new-slug>")
        with _locked(ctx):
            _project(ctx, _snapshot(graph, ctx.thread))
        return _report(ctx, graph, f"{ctx.unit} already exists ({status}); its evidence is kept.")
    ctx.unit_dir.mkdir(parents=True, exist_ok=True)
    payload = initial_state(project=ctx.cfg.project, unit=ctx.unit,
                            config_digest=config.config_digest(ctx.cfg))
    _run(ctx, graph, payload)
    return _report(ctx, graph, f"started {ctx.unit}.")


def cmd_status(ctx: Ctx, args) -> int:
    if ctx.unit is None:
        pointer = session.read_active(ctx.root, ctx.cfg)
        if pointer is None:
            print(f"no active unit. Start one with: {PROG} start --unit <slug>")
            return 0
        ctx.unit = pointer["slug"]
    graph = _graph(ctx)  # read only: no lock is taken
    snapshot = _snapshot(graph, ctx.thread)
    if not snapshot.values:
        try:
            proj = projection.read_manifest(ctx.unit_dir)
        except projection.ManifestError:
            raise Refused(f"no unit named {ctx.unit}", f"{PROG} start --unit {ctx.unit}") from None
        print(f"unit {ctx.unit}: status {proj.data.get('status')} (no thread: the checkpoints "
              "were archived or never written)")
        print(f"  next: {PROG} verify --unit {ctx.unit}")
        return 0
    status = _status_of(ctx, snapshot)
    items = runner.pending(graph, ctx.thread)
    passed = snapshot.values.get("effective") or []
    print(f"unit {ctx.unit}: {status}")
    print(f"  gates passed: {', '.join(passed) if passed else '(none)'}")
    if snapshot.values.get("stale"):
        print(f"  stale: {', '.join(snapshot.values['stale'])}")
    print(f"  {_waiting(items)}")
    print(f"  next: {_next_for(ctx, items[0]) if items else f'{PROG} status --unit {ctx.unit}'}")
    return 0


def cmd_pending(ctx: Ctx, args) -> int:
    graph, snapshot = _open_unit(ctx)
    items = runner.pending(graph, ctx.thread)
    if not items:
        print(f"no open requests for {ctx.unit} (status {_status_of(ctx, snapshot)}).")
        print(f"  next: {PROG} status --unit {ctx.unit}")
        return 0
    for item in items:
        print(f"{item.get('id')}  {item.get('kind')}  gate={item.get('gate')}  "
              f"channel={item.get('channel')}  {item.get('summary')}")
        print(f"  next: {_next_for(ctx, item)}")
    return 0


def _fresh_gates(ctx: Ctx, snapshot) -> tuple[list, list] | None:
    """(effective, stale) as the files on disk are now: the same computation as the graph's
    reconcile node (core.kernel.surface, plan 1.7). None when the chain or a stored surface is
    broken; the graph's own reconcile reports that as an integrity finding."""
    chain = list((snapshot.values or {}).get("evidence") or [])
    stored_surface = functools.partial(surface.load_surface, ctx.state_dir)
    try:
        return (surface.effective_gates(chain, ctx.cfg, ctx.unit_dir, stored_surface),
                surface.stale_gates(chain, ctx.cfg, ctx.unit_dir, stored_surface))
    except (ChainError, SurfaceMissing):
        return None


def _reconcile_pass(ctx: Ctx, graph) -> None:
    """Run the graph's reconcile node against the files on disk (plan 1.7).

    The unit waits at a work request, and the work node takes only ``attempt`` as an answer to
    move on. Any other proceed returns nothing, so the graph reconciles and asks for work again at
    the first gate that is not effective now. This pass writes no evidence."""
    _run(ctx, graph, Command(resume={"answer": "reconcile"}))


def cmd_attempt(ctx: Ctx, args) -> int:
    gate_ids = [gate.id for gate in ctx.cfg.gates]
    if args.gate not in gate_ids:
        raise Refused(f"gate {args.gate!r} is not in the adapter (gates: {', '.join(gate_ids)})",
                      f"{PROG} status --unit {ctx.unit}")
    graph, snapshot = _open_unit(ctx)
    status = _status_of(ctx, snapshot)
    if status in TERMINAL:
        raise Refused(f"unit {ctx.unit} is {status}; it takes no more attempts",
                      f"{PROG} status --unit {ctx.unit}")
    # The stored snapshot is only as new as the last run. A file edit makes gates stale without a
    # run, so the decision reads effective and stale from the files now (plan 1.7).
    fresh = _fresh_gates(ctx, snapshot)
    stored = (list((snapshot.values or {}).get("effective") or []),
              list((snapshot.values or {}).get("stale") or []))
    effective = fresh[0] if fresh is not None else stored[0]
    items = runner.pending(graph, ctx.thread)
    work = [item for item in items if item.get("kind") == "work"]
    # The graph chose the request the unit waits at on its last run. When an edit has changed the
    # picture since, the graph reconciles before this attempt is judged: if the attempt is for a
    # gate the unit does not wait on, or for a gate that is effective now. An attempt for the gate
    # the unit waits on runs its checks on the files as they are.
    rerouted = fresh is None or fresh != stored
    if work and rerouted and (work[0].get("gate") != args.gate or args.gate in effective):
        ctx.take_active = True
        _reconcile_pass(ctx, graph)
        snapshot = _snapshot(graph, ctx.thread)
        items = runner.pending(graph, ctx.thread)
        work = [item for item in items if item.get("kind") == "work"]
        effective = list((snapshot.values or {}).get("effective") or [])
    if args.gate in effective:
        raise Refused(f"gate {args.gate} has already passed for {ctx.unit}",
                      f"{PROG} status --unit {ctx.unit}")
    if not work:
        raise Refused(f"unit {ctx.unit} is not waiting for work ({_waiting(items)})",
                      _next_for(ctx, items[0]) if items else f"{PROG} status --unit {ctx.unit}")
    if work[0].get("gate") != args.gate:
        raise Refused(f"unit {ctx.unit} is waiting for gate {work[0].get('gate')}, not {args.gate}",
                      f"{PROG} attempt {work[0].get('gate')} --unit {ctx.unit}")
    ctx.take_active = True
    return _answer(ctx, graph, decision="attempt", by=args.by or "external", note=None,
                   attempt_gate=args.gate)


def cmd_proceed(ctx: Ctx, args) -> int:
    graph, _ = _open_unit(ctx)
    ctx.take_active = True
    return _answer(ctx, graph, decision=args.decision, by=args.by, note=args.note,
                   token=args.token)


def cmd_pause(ctx: Ctx, args) -> int:
    graph, snapshot = _open_unit(ctx)
    status = _status_of(ctx, snapshot)
    if status in TERMINAL:
        raise Refused(f"unit {ctx.unit} is {status}; a terminal unit cannot be paused (B02)",
                      f"{PROG} status --unit {ctx.unit}")
    if not runner.pending(graph, ctx.thread):
        raise Refused(f"unit {ctx.unit} is not waiting at an interrupt (status {status}), so "
                      "there is nothing to pause", f"{PROG} status --unit {ctx.unit}")
    with _locked(ctx):
        current = _snapshot(graph, ctx.thread)
        _project(ctx, current, status="paused")
    items = runner.pending(graph, ctx.thread)
    print(f"paused {ctx.unit}: it waits at its {items[0].get('kind')} request. The stop hook does "
          "not block on a paused unit.")
    print(f"  next: {_next_for(ctx, items[0])}")
    return 0


def cmd_abandon(ctx: Ctx, args) -> int:
    graph, snapshot = _open_unit(ctx)
    status = _status_of(ctx, snapshot)
    if status in TERMINAL:
        raise Refused(f"unit {ctx.unit} is already {status}; terminal statuses do not change",
                      f"{PROG} status --unit {ctx.unit}")
    items = runner.pending(graph, ctx.thread)
    if not items or items[0].get("kind") != "escalation":
        raise Refused(f"abandon is the answer to an escalation, and unit {ctx.unit} is "
                      f"{_waiting(items)}. To leave it stopped without abandoning, run: "
                      f"{PROG} pause --unit {ctx.unit}",
                      f"{PROG} pause --unit {ctx.unit}")
    return _answer(ctx, graph, decision="abandon", by=args.by, note=args.note, token=args.token)


def cmd_escalate(ctx: Ctx, args) -> int:
    graph, snapshot = _open_unit(ctx)
    status = _status_of(ctx, snapshot)
    if status in TERMINAL:
        raise Refused(f"unit {ctx.unit} is {status}; it cannot be escalated",
                      f"{PROG} status --unit {ctx.unit}")
    items = runner.pending(graph, ctx.thread)
    if items and items[0].get("kind") == "escalation":
        raise Refused(f"unit {ctx.unit} already waits on an escalation, which the graph recorded. "
                      "Decide it with proceed.", _next_for(ctx, items[0]))
    raise Refused(f"{PROG} escalate cannot stop a unit on request: the graph escalates by itself "
                  "when the attempts or the fail streak run out, or the budget is spent. Your note "
                  "is in the failure ledger. To leave the unit stopped, run: "
                  f"{PROG} pause --unit {ctx.unit}",
                  f"{PROG} pause --unit {ctx.unit}", note=args.note)


def _verify_reader(graph, ctx: Ctx):
    """The checkpoint reader kernel.verify expects: (thread, checkpoint) -> evidence and status.

    The runner's reader returns the latest checkpoint only; this one reads the named one. The
    status is the one the projection would write for that checkpoint (see _status_of)."""
    def read(thread_id, checkpoint_id):
        snapshot = graph.get_state({"configurable": {"thread_id": thread_id,
                                                     "checkpoint_id": checkpoint_id}})
        found = ((snapshot.config or {}).get("configurable") or {}).get("checkpoint_id")
        if found != checkpoint_id or not snapshot.values:
            return None
        return {"evidence": list(snapshot.values.get("evidence") or []),
                "status": _status_of(ctx, snapshot)}
    return read


def cmd_verify(ctx: Ctx, args) -> int:
    graph = _graph(ctx)
    report = verify.verify_unit(ctx.unit_dir, ctx.cfg, _verify_reader(graph, ctx))
    if report.legacy:
        print(f"{ctx.unit} has a legacy (schema 1) manifest with no evidence chain; it cannot be "
              "verified.")
        print(f"  next: {PROG} status --unit {ctx.unit}")
        return 1
    for step in report.steps:
        detail = f" ({step.detail})" if step.detail else ""
        print(f"step {step.step} {step.name}: {step.status}{detail}")
    if report.ok:
        print(f"verified {ctx.unit}: every checked step passes.")
        return 0
    print(f"verification of {ctx.unit} failed at step {report.failed_step}: {report.failed_detail}")
    print(f"  next: restore the file named above from version control, then run: "
          f"{PROG} verify --unit {ctx.unit}")
    return 1


def cmd_archive(ctx: Ctx, args) -> int:
    graph, snapshot = _open_unit(ctx)
    status = _status_of(ctx, snapshot)
    if status not in TERMINAL:
        items = runner.pending(graph, ctx.thread)
        raise Refused(f"unit {ctx.unit} is {status}; only a complete, escalated or abandoned unit "
                      "is archived", f"{PROG} pending --unit {ctx.unit}" if items
                      else f"{PROG} status --unit {ctx.unit}")
    with _locked(ctx):
        _project(ctx, _snapshot(graph, ctx.thread))  # export evidence.jsonl and manifest first
        graph.checkpointer.delete_thread(ctx.thread)
    print(f"archived {ctx.unit}: evidence.jsonl and manifest.yaml stay in {ctx.unit_dir}; the "
          f"checkpoints of thread {ctx.thread} are deleted.")
    print(f"  next: {PROG} status --unit {ctx.unit}")
    return 0


HANDLERS = {
    "start": cmd_start, "status": cmd_status, "attempt": cmd_attempt, "pending": cmd_pending,
    "proceed": cmd_proceed, "pause": cmd_pause, "abandon": cmd_abandon,
    "escalate": cmd_escalate, "verify": cmd_verify, "archive": cmd_archive,
}


# --------------------------------------------------------------------------
# Parsing, dispatch, refusals
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=None,
                        help="project root (default: found from the working directory)")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="VERB")

    def verb(name: str, help_text: str, *, session: bool = True, unit: bool = True):
        q = sub.add_parser(name, help=help_text)
        if unit:
            q.add_argument("--unit", required=True, help="unit slug")
        if session:
            q.add_argument("--session-id", dest="session_id", default=None,
                           help="the host session id; arms the stop rule for it")
        return q

    verb("start", "create a unit and run it to its first request")
    st = verb("status", "show a unit (read only; takes no lock)", session=False, unit=False)
    st.add_argument("--unit", default=None, help="unit slug (default: the active unit)")
    at = verb("attempt", "make one attempt at a gate the unit is waiting for")
    at.add_argument("gate", help="gate id")
    at.add_argument("--by", default=None, help="who attempts (default: external)")
    verb("pending", "list the open requests of a unit", session=False)
    rs = verb("proceed", "answer the open request of a unit")
    rs.add_argument("--decision", required=True, help="the answer, e.g. approve, reject, retry")
    rs.add_argument("--by", default=None, help="who answers")
    rs.add_argument("--note", default=None, help="a note (required for reject)")
    rs.add_argument("--token", default=None, help="the one-time token (channel token)")
    verb("pause", "leave the unit stopped at its request (the stop hook does not block)")
    ab = verb("abandon", "abandon a unit at its escalation")
    ab.add_argument("--by", default=None, help="who abandons")
    ab.add_argument("--note", default=None, help="why")
    ab.add_argument("--token", default=None, help="the one-time token (channel token)")
    es = verb("escalate", "record why a unit is stuck")
    es.add_argument("--note", required=True, help="the written reason (mandatory)")
    verb("verify", "prove a unit in five steps (read only)", session=False)
    verb("archive", "export and delete the checkpoints of a finished unit")
    return parser


def _refuse(ctx: Ctx, verb_name: str, exc: Refused) -> int:
    """One refusal: a ledger row, the reason, and the command to run next (stderr)."""
    ledger.record(ctx.root, "gate_refusal", verb=verb_name, unit=ctx.unit, reason=exc.message,
                  next=exc.next_step, **exc.fields)
    print(f"error: {exc.message}", file=sys.stderr)
    print(f"next: {exc.next_step}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = paths.find_root(args.root) if args.root else paths.find_root()
    except paths.RootNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"next: run {PROG} from the project directory, or pass --root <project>",
              file=sys.stderr)
        return 1
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"next: fix {config.ADAPTER_REL} as listed, then run: {PROG} status",
              file=sys.stderr)
        return 1
    unit = getattr(args, "unit", None)
    ctx = Ctx(root=root, cfg=cfg, unit=unit, session_id=getattr(args, "session_id", None))
    try:
        if unit is not None and not paths.valid_slug(unit):
            raise Refused(f"invalid unit slug {unit!r} (letters, digits, dot, underscore and "
                          "hyphen; no path separators)", f"{PROG} start --unit <slug>")
        ctx.channel = _channel(cfg)
        if ctx.channel == "token":
            ctx.token = new_token()
            ctx.token_digest = hashlib.sha256(ctx.token.encode("utf-8")).hexdigest()
        if args.cmd in MUTATING and ctx.session_id:
            session.arm(root, ctx.session_id)  # B32: every mutating verb arms the session
        return HANDLERS[args.cmd](ctx, args)
    except Refused as exc:
        return _refuse(ctx, args.cmd, exc)
    except Exception as exc:  # the graph or the store failed: say what to look at
        if os.environ.get(brand.ENV_PREFIX + "DEBUG"):
            traceback.print_exc()
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"next: {PROG} status --unit {ctx.unit or '<slug>'}, then "
              f"{PROG} verify --unit {ctx.unit or '<slug>'}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
