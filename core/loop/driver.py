"""The driver, a deterministic Python state machine that OWNS the loop (the control inversion).

Instead of an LLM voluntarily obeying a loop prompt (and drifting: skipped methodology, repeated
levers under renamed cells, unrecorded proposals), Python walks the pipeline and invokes a worker
only at the judgment points (PROPOSE, DEDUP-judge, CHECK). Every quantitative decision is
deterministic code; every transition is persisted and receipted.

    BRIEF → PROPOSE → DEDUP → SMOKE(+repair) → ORACLE → CHECK → DECIDE → RECORD → PACE

**Resumability (the ``kill -9`` guarantee).** State is saved *after* each handler completes,
recording the phase to run next. So the phase on disk is always "the handler about to run"; a
crash mid-CHECK leaves ``phase == check`` and the proceeded driver simply re-runs CHECK. Every
handler is idempotent (re-proposing overwrites the variant; re-running the oracle recomputes;
RECORD dedupes the registry append), so re-entry is safe.

**Failure routing.** Variant faults (bad contract, smoke bug) strike → repair or a clean
REJECT that records **no** ProposalCard (so a bug is never a false dead-end). Infra faults
(auth, rate-limit, oracle exception) escalate/back-off and abort the iteration **without**
blaming the variant. The ProposalCard is recorded only *after* SMOKE passes, that deferral
is what keeps buggy code out of the dead-end memory.

The phase list is FIXED (plan §8.3, a new phase is a version bump, not a plugin API). DEDUP
degrades to registry-key dedup + optional judge; BRIEF to a deterministic digest. On top of
that fixed core sit three optional layers: config metric floors at ORACLE (§8.1),
``self_consistency`` at CHECK and ``reflexion`` at DECIDE (§14, both off by default), and the
§12 escalation driver-backstop (fail streak / disagreement > τ → stop and escalate).
"""
from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass, field

from core.loop import manifest as loop_manifest
from core.loop.backstops import is_drifting, should_stop
from core.loop.brief import dead_ends_digest
from core.loop.contracts import checker_kwargs, verdict_from_contract
from core.loop.decision import decide
from core.loop.dedup import dedup_check
from core.loop.errors import EscalationRequested, Route, SmokeError, classify, write_escalation
from core.loop.events import EventLog, render_status
from core.loop.gates import apply_metric_floors
from core.loop.gateway import VARIANT_FILE, WorkerGateway
from core.loop.pidfile import PidFile
from core.loop.registry import Registry
from core.loop.schemas import GateResult, ProposalCard, Verdict, VerdictCard
from core.loop.search import aggregate_judgements
from core.loop.smoke import smoke_or_raise
from core.loop.statefile import IterationState, Phase, StateStore
from core.loop.usage import _iter_usage_records, over_threshold, tokens_in_window

__all__ = ["Driver", "Transition", "compute_status", "VARIANT_FILE"]


@dataclass
class Transition:
    """A handler's result: the next phase + the receipt fields for this step."""

    next_phase: Phase
    detail: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    verdict: "str | None" = None
    outcome: "str | None" = None
    artifacts: "list[str]" = field(default_factory=list)


def compute_status(reg: Registry, log: EventLog, cfg: dict, *,
                   elapsed_minutes: float = 0.0) -> dict:
    """Reduce the registry + receipts to "where are we", auditable, survives a context reset."""
    graduated = reg.graduated_ids()
    n_proposals = sum(1 for e in reg.events() if e.get("kind") == "proposal")
    target = (cfg.get("stop") or {}).get("graduate_target", 9999)
    bk = cfg.get("backstops", {}) or {}
    cost = log.total_cost_usd()
    stop = should_stop(
        {"graduated": len(graduated), "iterations": n_proposals, "cost_usd": cost,
         "elapsed_minutes": elapsed_minutes},
        graduate_target=target,
        max_iterations=bk.get("max_iterations"),
        cost_budget_usd=bk.get("cost_budget_usd"),
        wall_clock_minutes=bk.get("wall_clock_minutes"),
    )
    return {
        "n_graduated": len(graduated),
        "graduated": graduated,
        "target": target,
        "probation": reg.probation_ids(),
        "dead_end_cells": sorted(reg.dead_end_cells()),
        "iterations": n_proposals,
        "cost_usd": cost,
        "stop": stop.stop,
        "stop_reason": stop.reason,
    }


class Driver:
    def __init__(
        self,
        *,
        oracle,
        gateway: WorkerGateway,
        registry: Registry,
        store: StateStore,
        log: EventLog,
        cfg: dict,
        state_root: str,
        repo_root=None,
        variant_prefix: str = "c",
        brief_fn=None,
        sleeper=time.sleep,
        clock=time.time,
    ) -> None:
        self.oracle = oracle
        self.gateway = gateway
        self.registry = registry
        self.store = store
        self.log = log
        self.cfg = cfg
        self.state_root = state_root
        self.repo_root = repo_root         # enables the protected-file mid-run recheck
        self.prefix = variant_prefix
        self._sleep = sleeper              # injectable so backoff is instant under test
        self._now = clock
        self._run_started = clock()
        self._stop_requested = False       # set by SIGINT (finish the phase, persist, exit 0)
        self._banner = ""                  # STATUS.md banner (PAUSED / backing off / halted)
        self.faulted = False               # a fault-shaped stop (escalation / protected-file halt)
                                           #, the CLI's exit code reads this, not the banner text
        self._infra_streak = 0             # consecutive escalating infra aborts (bounded retries)
        self._run_manifest = None          # captured at run() start for the mid-run recheck
        self.pidfile = PidFile(os.path.join(state_root, "loop.pid"))
        # Deterministic dead-ends brief by default; a brief failure never blocks an iteration.
        self.brief_fn = brief_fn or (lambda: dead_ends_digest(self.registry))
        self._handlers = {
            Phase.BRIEF: self._brief,
            Phase.PROPOSE: self._propose,
            Phase.DEDUP: self._dedup,
            Phase.SMOKE: self._smoke,
            Phase.ORACLE: self._oracle,
            Phase.CHECK: self._check,
            Phase.DECIDE: self._decide,
            Phase.RECORD: self._record,
            Phase.PACE: self._pace,
        }

    # ── path helpers ──────────────────────────────────────────────────────────
    def _cand_dir(self, cid: str) -> str:
        return os.path.join(self.state_root, "variants", cid)

    def _cand_file(self, cid: str, name: str) -> str:
        return os.path.join(self._cand_dir(cid), name)

    def _read_json(self, cid: str, name: str) -> dict:
        with open(self._cand_file(cid, name), encoding="utf-8") as fh:
            return json.load(fh)

    def _write_json(self, cid: str, name: str, obj: dict) -> str:
        path = self._cand_file(cid, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
        return path

    def _elapsed_minutes(self) -> float:
        return (self._now() - self._run_started) / 60.0

    # ── the loop ──────────────────────────────────────────────────────────────
    def run_iteration(self, state: IterationState) -> IterationState:
        """Walk ``state`` from its current phase to DONE (or abort on an infra fault)."""
        while state.phase_enum is not Phase.DONE:
            if self._stop_requested:
                # SIGINT: the just-finished phase is already persisted (save-after-completion),
                # so we return between phases, proceed picks up exactly here.
                return state
            phase = state.phase_enum
            try:
                trans = self._handlers[phase](state)
            except Exception as exc:  # noqa: BLE001, every fault is routed, never crashes the loop
                trans = self._route_failure(state, phase, exc)
                if trans is None:  # infra abort: leave state as-is to retry, stop this iteration
                    return state
            prev = phase.value
            state.advance(trans.next_phase)
            state.detail = trans.detail
            self.store.save(state)
            self.log.emit(
                state.iteration, prev, trans.next_phase.value,
                detail=trans.detail, variant=state.variant_id,
                cost_usd=trans.cost_usd, tokens=trans.tokens,
                verdict=trans.verdict, outcome=trans.outcome, artifact_paths=trans.artifacts,
            )
        return state

    def run(self, max_iterations: "int | None" = None) -> int:
        """Run iterations to completion, proceeding any in-flight one first. Returns #completed.

        Wrapped by the operational guards: a single-instance pidfile lock, a graceful SIGINT
        that finishes the current phase then exits 0, a ``state/PAUSE`` yield, ``RateLimited``
        backoff, the usage-window guard, and the §12 fail-streak escalation, all checked
        between iterations so state is always at a clean boundary when we stop."""
        completed = 0
        carry_target: "str | None" = None  # a PROBATION target, carried one hop to the next maker
        self._run_started = self._now()
        if self.repo_root is not None:
            self._run_manifest = loop_manifest.compute_manifest(self.repo_root)
        self.pidfile.acquire()  # refuse a second instance on this state dir
        with self._sigint_guard():
            try:
                while True:
                    if self._stop_requested:
                        if not self._banner:  # keep a more-specific reason (e.g. protected-file halt)
                            self._set_banner("SIGINT: stopped at a clean phase boundary; re-run to proceed")
                        return completed
                    if self._paused():
                        self._set_banner("PAUSED (state/PAUSE present), yielding; remove it and re-run")
                        return completed
                    if not self._wait_out_backoff() or not self._usage_ok():
                        return completed
                    state = self.store.load_latest()
                    if state is None or state.phase_enum is Phase.DONE:
                        if self._fail_streak_escalation(state):
                            return completed
                        stop = self._backstop()
                        if stop.stop:
                            self._set_banner(f"HALTED: {stop.reason}")
                            return completed
                        nxt = 0 if state is None else state.iteration + 1
                        state = IterationState(iteration=nxt, probation_target=carry_target)
                        self.store.save(state)
                    self.run_iteration(state)
                    if state.phase_enum is Phase.DONE:
                        # _decide sets probation_target to the failing pass on PROBATION (else
                        # None), so this carries a PROBATION target to the next iteration's maker.
                        carry_target = state.probation_target
                        completed += 1  # only a DONE iteration counts, aborts are not progress
                        self._infra_streak = 0
                        if max_iterations is not None and completed >= max_iterations:
                            return completed
                    elif self._infra_streak >= self._infra_retry_limit():
                        # A persistent infra fault (buggy oracle, expired auth) must stop with a
                        # typed escalation, not spin at full speed re-entering the same phase.
                        # The receipt already exists (coalesced per distinct fault signature).
                        self._set_banner(
                            f"ESCALATED: {self._infra_streak} consecutive infra faults on "
                            f"iteration {state.iteration}, stopping; see "
                            f"{os.path.join(self.state_root, 'escalations')} and re-run after fixing")
                        self.faulted = True
                        return completed
            finally:
                self.pidfile.release()

    # ── operational guards ─────────────────────────────────────────────────────
    def request_stop(self) -> None:
        """Ask the loop to finish the current phase, persist, and exit 0 (SIGINT target / testable)."""
        self._stop_requested = True

    def _sigint_guard(self):
        driver = self

        class _Guard:
            def __enter__(self):
                self._prev = None
                try:
                    self._prev = signal.signal(signal.SIGINT, lambda *_: driver.request_stop())
                except ValueError:
                    self._prev = None  # not on the main thread (e.g. under a test runner), skip
                return self

            def __exit__(self, *exc):
                if self._prev is not None:
                    try:
                        signal.signal(signal.SIGINT, self._prev)
                    except ValueError:
                        pass

        return _Guard()

    def _paused(self) -> bool:
        return os.path.exists(os.path.join(self.state_root, "PAUSE"))

    def _wait_out_backoff(self) -> bool:
        """If the persisted state carries a future ``backoff_until`` (set on ``RateLimited``), sleep the
        remainder here, at an iteration boundary, then proceed. Returns False only if stopped meanwhile."""
        state = self.store.load_latest()
        if state is None or not state.backoff_until:
            return True
        remaining = state.backoff_until - self._now()
        if remaining > 0:
            self._set_banner(f"backing off {remaining:.0f}s after a rate-limit / infra fault")
            self._sleep(remaining)
        state.backoff_until = None  # clear so we don't re-sleep; a fresh fault re-arms it
        self.store.save(state)
        return not self._stop_requested

    def _usage_ok(self) -> bool:
        """Best-effort 5h-window guard. Ships DISABLED; a no-op unless the user calibrated it."""
        guard = (self.cfg.get("backstops", {}) or {}).get("rate_limit_guard", {}) or {}
        if not guard.get("enabled"):
            return True
        ceiling = guard.get("tokens_5h_ceiling")
        try:
            glob_pat = os.path.expanduser("~/.claude/projects/*/*.jsonl")
            used = tokens_in_window(list(_iter_usage_records(glob_pat)), self._now())
        except Exception:  # noqa: BLE001, a guard that can't read transcripts must not block the loop
            return True
        if over_threshold(used, ceiling, guard.get("halt_at_percent", 97)):
            self._set_banner(f"HALTED: ~{used:,} tokens in 5h over the calibrated ceiling, yielding")
            return False
        return True

    def _set_banner(self, text: str) -> None:
        self._banner = text

    def _infra_retry_limit(self) -> int:
        """How many consecutive escalating infra aborts to retry before stopping (default 3)."""
        return int((self.cfg.get("escalation") or {}).get("infra_retry_limit") or 3)

    def _backstop(self):
        graduated = self.registry.graduated_ids()
        n_prop = sum(1 for e in self.registry.events() if e.get("kind") == "proposal")
        bk = self.cfg.get("backstops", {}) or {}
        return should_stop(
            {"graduated": len(graduated), "iterations": n_prop,
             "cost_usd": self.log.total_cost_usd(),
             "elapsed_minutes": self._elapsed_minutes()},
            graduate_target=(self.cfg.get("stop") or {}).get("graduate_target", 9999),
            max_iterations=bk.get("max_iterations"),
            cost_budget_usd=bk.get("cost_budget_usd"),
            wall_clock_minutes=bk.get("wall_clock_minutes"),
        )

    def _fail_streak_escalation(self, state: "IterationState | None") -> bool:
        """§12 driver backstop: N consecutive REJECT verdicts → stop and escalate, don't loop."""
        streak_at = (self.cfg.get("escalation") or {}).get("fail_streak_to_escalate")
        if not streak_at:
            return False
        trailing = 0
        for e in reversed([e for e in self.registry.events() if e.get("kind") == "verdict"]):
            if e.get("verdict") == Verdict.REJECT.value:
                trailing += 1
            else:
                break
        if trailing < int(streak_at):
            return False
        exc = EscalationRequested(
            f"fail streak: {trailing} consecutive REJECT verdicts (threshold {streak_at}), "
            f"stopping to escalate instead of looping")
        iteration = state.iteration if state is not None else 0
        path = write_escalation(self.state_root, iteration=iteration, phase="run", exc=exc)
        self._set_banner(f"ESCALATED: {exc}")
        self.faulted = True
        self.log.emit(iteration, "run", "run", detail=str(exc), outcome="infra",
                      artifact_paths=[str(path)])
        return True

    # ── phase handlers ────────────────────────────────────────────────────────
    def _brief(self, state: IterationState) -> Transition:
        # The dead-ends digest (deterministic over the registry). It never blocks the iteration.
        brief = self.brief_fn()
        state.artifacts["brief_len"] = len(brief)
        return Transition(Phase.PROPOSE, detail=f"brief ready ({len(brief)} chars)")

    def _propose(self, state: IterationState) -> Transition:
        cid = state.variant_id or f"{self.prefix}{state.iteration:03d}"
        state.variant_id = cid
        cand_dir = self._cand_dir(cid)
        repair = ""
        repair_path = self._cand_file(cid, "_repair.txt")
        if os.path.exists(repair_path):
            with open(repair_path, encoding="utf-8") as fh:
                repair = fh.read()
        # On a PROBATION re-iteration, hand the maker the near-miss's ACTUAL code (not just the
        # failing pass name) so it CONTINUES that variant rather than proposing a fresh lever.
        probation_code = None
        if state.probation_target:
            prior = self.registry.latest_probation_id()
            prior_cand = self._cand_file(prior, VARIANT_FILE) if prior else None
            if prior_cand and os.path.exists(prior_cand):
                with open(prior_cand, encoding="utf-8") as fh:
                    probation_code = fh.read()
        try:
            space = list(self.oracle.proposal_space() or [])
        except Exception:  # noqa: BLE001, a broken optional hook must not block PROPOSE
            space = []
        res = self.gateway.propose(
            variant_id=cid, variant_dir=cand_dir, brief=self.brief_fn(),
            probation_target=state.probation_target, probation_code=probation_code,
            repair_feedback=repair, proposal_space=space,
        )
        self._write_json(cid, "proposal.json", res.card.to_dict())
        return Transition(
            Phase.DEDUP, detail=f"proposed {cid} cell={res.card.cell}",
            cost_usd=res.cost_usd, tokens=res.tokens, artifacts=[res.experiment_path],
        )

    def _dedup(self, state: IterationState) -> Transition:
        cid = state.variant_id
        card = ProposalCard.from_dict(self._read_json(cid, "proposal.json"))
        self._last_judge = None
        res = dedup_check(card, self.registry, judge_fn=self._judge_fn)
        self._write_json(cid, "dedup.json", res.to_dict())
        cost = getattr(self._last_judge, "cost_usd", 0.0) or 0.0
        tokens = getattr(self._last_judge, "tokens", 0) or 0
        if not res.distinct:
            # A judge-adjudicated high-confidence DUPLICATE gets ONE re-proposal (the maker may
            # pivot to a distinct lever) before we give up; an exact registry match is *certain*,
            # so it short-circuits straight to REJECT. Either way it is a zero-oracle-compute
            # outcome, and no ProposalCard is recorded (deferred to SMOKE-pass), so a duplicate
            # never adds a *new* dead-end cell, it re-confirms an existing one.
            drift = self.cfg.get("backstops", {}).get("drift", {}) or {}
            max_attempts = drift.get("max_attempts_per_variant", 3)
            if res.source == "judge" and state.attempts < max_attempts:
                state.attempts += 1
                with open(self._cand_file(cid, "_repair.txt"), "w", encoding="utf-8") as fh:
                    fh.write(
                        f"Your previous proposal (cell '{card.cell}') was rejected at DEDUP as a "
                        f"DUPLICATE of an already-settled dead-end ({res.matched_cell}): {res.reason}\n"
                        f"Propose a GENUINELY DISTINCT lever: a different mechanism/axis, not a rewording."
                    )
                return Transition(Phase.PROPOSE, cost_usd=cost, tokens=tokens, outcome="variant",
                                  detail=f"dedup DUPLICATE → re-propose attempt {state.attempts}")
            vcard = VerdictCard(
                variant_id=cid, soft_passes={"mechanism_lead": False, "robustness": False},
                gaming_clean=True, mechanism_sentence="", verdict=Verdict.REJECT,
                target_pass=None, why=f"dedup DUPLICATE ({res.source}): {res.reason}",
            )
            self._write_json(cid, "verdict.json", vcard.to_dict())
            return Transition(Phase.RECORD, detail=f"dedup DUPLICATE → REJECT ({res.source})",
                              verdict="REJECT", outcome="variant", cost_usd=cost, tokens=tokens)
        flag = " [flagged]" if res.flagged else ""
        return Transition(Phase.SMOKE, detail=f"dedup DISTINCT{flag} ({res.source})",
                          cost_usd=cost, tokens=tokens)

    def _judge_fn(self, card, neighbours) -> dict:
        """Adapter from the gateway's blinded dedup worker to the ``dedup_check`` callable shape.
        Stashes the raw result so ``_dedup`` can bill its cost/tokens onto the receipt."""
        r = self.gateway.judge(card=card, neighbours=neighbours)
        self._last_judge = r
        return {"decision": r.decision, "confidence": r.confidence, "reason": r.reason}

    def _smoke(self, state: IterationState) -> Transition:
        cid = state.variant_id
        variant = self._cand_file(cid, VARIANT_FILE)
        smoke_or_raise(variant, self.oracle)  # SmokeError → repair loop (in _route_failure)
        # SMOKE passed: NOW record the ProposalCard (deferral keeps buggy code out of dead-end memory).
        self._record_proposal_once(cid)
        # clear any repair breadcrumb
        repair_path = self._cand_file(cid, "_repair.txt")
        if os.path.exists(repair_path):
            os.remove(repair_path)
        return Transition(Phase.ORACLE, detail="smoke ok; proposal recorded")

    def _oracle(self, state: IterationState) -> Transition:
        cid = state.variant_id
        variant = self._cand_file(cid, VARIANT_FILE)
        gr = self.oracle.run_gates(variant, self.cfg)  # exception → infra escalation
        floors = (self.cfg.get("gates") or {}).get("metric_floors") or {}
        gr = apply_metric_floors(gr, floors)
        self._write_json(cid, "gate_result.json", gr.to_dict())
        if not gr.tripwire_ok:
            # Tripwire voids the variant: REJECT, skip CHECK/DECIDE, straight to RECORD.
            tripped = ", ".join(sorted(k for k, v in gr.tripwires.items() if v))
            card = VerdictCard(
                variant_id=cid, soft_passes={"mechanism_lead": False, "robustness": False},
                gaming_clean=True, mechanism_sentence="", verdict=Verdict.REJECT,
                target_pass=None, why=f"tripwire: {tripped}",
                voids_cell=False,  # a tripwire is a CODE bug, not a falsified mechanism → don't burn the cell
            )
            self._write_json(cid, "verdict.json", card.to_dict())
            return Transition(Phase.RECORD, detail="tripwire → REJECT", verdict="REJECT")
        return Transition(Phase.CHECK, detail=f"gates {gr.n_passed}/{len(gr.passes)}, tripwire ok")

    def _check(self, state: IterationState) -> Transition:
        cid = state.variant_id
        gr = self._read_json(cid, "gate_result.json")
        variant = self._cand_file(cid, VARIANT_FILE)
        sc = self.cfg.get("self_consistency") or {}
        if not sc.get("enabled"):
            res = self.gateway.check(variant_id=cid, experiment_path=variant, gate_result=gr)
            self._write_json(cid, "checker_judgement.json", res.judgement)
            return Transition(Phase.DECIDE, detail="checker judged",
                              cost_usd=res.cost_usd, tokens=res.tokens)
        # §14 self-consistency: sample N checkers, majority-aggregate; the vote spread is the
        # §12 black-box confidence signal, above the calibrated τ we escalate, not proceed.
        n = max(1, int(sc.get("n") or 8))
        results = [self.gateway.check(variant_id=cid, experiment_path=variant, gate_result=gr)
                   for _ in range(n)]
        judgement, disagreement = aggregate_judgements([r.judgement for r in results])
        judgement["self_consistency"] = {"n": n, "disagreement": round(disagreement, 4)}
        self._write_json(cid, "checker_judgement.json", judgement)
        esc = self.cfg.get("escalation") or {}
        # τ is a raw vote-spread cut-point, so the key says "disagreement";
        # the pre-rename `error_prob_threshold` stays readable for stamped
        # projects (F16, remove the alias after one release).
        tau = esc.get("disagreement_threshold")
        if tau is None:
            tau = esc.get("error_prob_threshold")
        if tau is not None and disagreement > float(tau):
            raise EscalationRequested(
                f"self-consistency disagreement {disagreement:.2f} > τ={tau} on {cid}, "
                f"escalating instead of confidently proceeding")
        return Transition(
            Phase.DECIDE,
            detail=f"checker judged (self-consistency n={n}, disagreement={disagreement:.2f})",
            cost_usd=sum(r.cost_usd for r in results), tokens=sum(r.tokens for r in results))

    def _decide(self, state: IterationState) -> Transition:
        cid = state.variant_id
        gr = GateResult.from_dict(self._read_json(cid, "gate_result.json"))
        judgement = self._read_json(cid, "checker_judgement.json")
        outcome = decide(gr, **checker_kwargs(judgement))
        # §14 reflexion (off by default): a non-voiding REJECT gets a bounded critique→revise
        # bounce through the repair loop before the verdict is recorded. Tripwire/gaming REJECTs
        # are voiding and never revised.
        rf = self.cfg.get("reflexion") or {}
        if (outcome.verdict is Verdict.REJECT and rf.get("enabled")
                and gr.tripwire_ok and bool(judgement.get("gaming_clean", True))
                and state.attempts < int(rf.get("max_iters") or 2)):
            state.attempts += 1
            failing = [k for k, v in gr.passes.items() if not v]
            failing += [k for k, v in (judgement.get("soft_passes") or {}).items() if not v]
            with open(self._cand_file(cid, "_repair.txt"), "w", encoding="utf-8") as fh:
                fh.write(
                    f"REFLEXION CRITIQUE (bounded revise, attempt {state.attempts}):\n"
                    f"The verdict would be REJECT: {outcome.reason}\n"
                    f"Failing passes: {', '.join(failing) or 'none'}\n"
                    f"Checker notes: {judgement.get('notes', '')}\n"
                    f"Revise the variant to address these specifically.")
            return Transition(Phase.PROPOSE, outcome="variant",
                              detail=f"reflexion revise attempt {state.attempts}")
        card = verdict_from_contract(judgement, cid, outcome.verdict, outcome.target_pass,
                                     outcome.reason)
        self._write_json(cid, "verdict.json", card.to_dict())
        state.probation_target = outcome.target_pass if outcome.verdict is Verdict.PROBATION else None
        return Transition(
            Phase.RECORD, detail=f"decided {outcome.verdict.value} ({outcome.reason})",
            verdict=outcome.verdict.value,
        )

    def _record(self, state: IterationState) -> Transition:
        cid = state.variant_id
        card = VerdictCard.from_dict(self._read_json(cid, "verdict.json"))
        self._record_verdict_once(cid, card)
        artifacts: "list[str]" = []
        if card.verdict is Verdict.GRADUATE:
            artifacts.append(self._sketch_graduation(cid))
        return Transition(Phase.PACE, detail=f"recorded {card.verdict.value}",
                          verdict=card.verdict.value, artifacts=artifacts)

    def _pace(self, state: IterationState) -> Transition:
        self._check_protected_files()
        self._rewrite_status(state)
        drift = self.cfg.get("backstops", {}).get("drift", {}) or {}
        if is_drifting(
            {"attempts": state.attempts, "minutes": (state.updated_ts - state.started_ts) / 60.0},
            max_attempts=drift.get("max_attempts_per_variant", 3),
            max_minutes=drift.get("max_minutes_per_variant", 20),
        ):
            return Transition(Phase.DONE, detail="pace: variant drift noted")
        return Transition(Phase.DONE, detail="iteration complete")

    # ── failure routing ───────────────────────────────────────────────────────
    def _route_failure(self, state: IterationState, phase: Phase, exc: Exception) -> "Transition | None":
        routing = classify(exc)
        cid = state.variant_id
        # A failed attempt was still billed (contract strikes, budget trips), carry its spend
        # onto the receipt so the budget backstop never undercounts real burn.
        spent_usd = float(getattr(exc, "cost_usd", 0.0) or 0.0)
        spent_tokens = int(getattr(exc, "tokens", 0) or 0)
        if routing.route is Route.VARIANT:
            # Smoke bug with attempts left → repair: write feedback, bounce back to PROPOSE.
            drift = self.cfg.get("backstops", {}).get("drift", {}) or {}
            max_attempts = drift.get("max_attempts_per_variant", 3)
            if isinstance(exc, SmokeError) and state.attempts < max_attempts:
                state.attempts += 1
                with open(self._cand_file(cid, "_repair.txt"), "w", encoding="utf-8") as fh:
                    fh.write(exc.traceback_text or str(exc))
                return Transition(Phase.PROPOSE, detail=f"smoke failed → repair attempt {state.attempts}",
                                  outcome="variant", cost_usd=spent_usd, tokens=spent_tokens)
            # Exhausted / non-repairable variant fault → clean REJECT. No ProposalCard was recorded
            # (we defer that to SMOKE-pass), so this never becomes a false dead-end cell.
            card = VerdictCard(
                variant_id=cid, soft_passes={"mechanism_lead": False, "robustness": False},
                gaming_clean=True, mechanism_sentence="", verdict=Verdict.REJECT,
                target_pass=None, why=routing.reason,
            )
            self._write_json(cid, "verdict.json", card.to_dict())
            return Transition(Phase.RECORD, detail=f"variant REJECT: {routing.reason}",
                              verdict="REJECT", outcome="variant",
                              cost_usd=spent_usd, tokens=spent_tokens)
        # INFRA: optionally escalate, optionally back off, abort the iteration (no strike, no
        # dead-end). A rate-limit is expected operation (escalate=False) → back off silently; only
        # genuine faults (auth/spawn/timeout/budget/oracle blow-ups/§12) write a receipt.
        artifacts: "list[str]" = []
        if routing.escalate:
            self._infra_streak += 1  # bounded retries: run() stops after the configured budget
            path = write_escalation(self.state_root, iteration=state.iteration, phase=phase.value,
                                    exc=exc, variant=cid)
            artifacts.append(str(path))
        if routing.backoff:
            state.backoff_count += 1  # each consecutive rate-limit lengthens the next sleep
            state.backoff_until = self._backoff_deadline(state)
        self.store.save(state)
        self.log.emit(state.iteration, phase.value, phase.value, detail=routing.reason,
                      variant=cid, outcome="infra", artifact_paths=artifacts,
                      cost_usd=spent_usd, tokens=spent_tokens)
        if isinstance(exc, EscalationRequested):
            # §12: escalation means STOP, a human decides; re-running would just re-escalate.
            self._set_banner(f"ESCALATED: {exc}")
            self.faulted = True
            self.request_stop()
        return None

    def _backoff_deadline(self, state: IterationState) -> float:
        # Exponential on CONSECUTIVE rate-limit backoffs (60s→…), capped at 30 min. Keyed on
        # backoff_count (not variant ``attempts``, which the drift backstop owns) so a multi-hour
        # session limit escalates its sleep instead of polling every 60s forever.
        base = 60.0 * (2 ** min(max(state.backoff_count - 1, 0), 5))
        return self._now() + min(base, 30 * 60.0)

    # ── idempotent registry writes ────────────────────────────────────────────
    def _record_proposal_once(self, cid: str) -> None:
        if cid in {e["id"] for e in self.registry.events() if e.get("kind") == "proposal"}:
            return
        self.registry.record(ProposalCard.from_dict(self._read_json(cid, "proposal.json")))

    def _record_verdict_once(self, cid: str, card: VerdictCard) -> None:
        latest = self.registry._latest_verdict_by_variant().get(cid)
        if latest is card.verdict:
            return  # idempotent: same verdict already the latest for this variant
        self.registry.record(card)

    def _sketch_graduation(self, cid: str) -> str:
        d = os.path.join(self.state_root, "graduated", cid)
        os.makedirs(d, exist_ok=True)
        note = os.path.join(d, "SKETCH.md")
        with open(note, "w", encoding="utf-8") as fh:
            fh.write(f"# SKETCH graduate: {cid}\n\nComposed by the loop; a human reviews & ships. "
                     f"See the variant dir for the artifact, gate_result, and verdict.\n")
        return note

    def _check_protected_files(self) -> None:
        """A methodology-critical file changed mid-run (adapter.yaml, oracle, agents, loop code)
        makes every subsequent result untraceable, request a graceful stop with a receipt-visible
        banner. Only active when ``repo_root`` was supplied (a baseline manifest was captured)."""
        if self._run_manifest is None or self.repo_root is None:
            return
        d = loop_manifest.diff(self._run_manifest, loop_manifest.compute_manifest(self.repo_root))
        touched = sorted(d.get("modified", []) + d.get("added", []) + d.get("removed", []))
        if touched:
            self._set_banner(f"HALTED: protected methodology file changed mid-run "
                             f"({', '.join(touched)}), stopping")
            self.faulted = True
            self.request_stop()

    def _rewrite_status(self, state: IterationState) -> None:
        try:
            engine_status = compute_status(self.registry, self.log, self.cfg,
                                           elapsed_minutes=self._elapsed_minutes())
        except Exception:  # noqa: BLE001, STATUS is a convenience, never fail the loop over it
            engine_status = {"n_graduated": len(self.registry.graduated_ids())}
        md = render_status(engine_status, state.to_dict(), self.log.tail(8), banner=self._banner)
        with open(os.path.join(self.state_root, "STATUS.md"), "w", encoding="utf-8") as fh:
            fh.write(md)
