"""The variant-search graph (plan section 1.14; brief S2; bugs B19, B20, B21, B22, B43, B50).

Shape of the graph::

    brief -> (Send per slot) variant -> record -> pace -> brief
                                                  \\-> escalate (fail streak) -> brief | end
    variant subgraph:
        propose -> dedup -> smoke -> oracle -> (Send per self-consistency sample) check_sample
                                                                                  -> decide
        smoke bug, repairs left   -> propose
        malformed proposal        -> propose (re-proposals left)
        decide REJECT, revisions  -> propose
        every terminal path       -> finish, which emits one result to the parent

Decisions:

* **Three counters (B22).** ``repairs`` (smoke bugs), ``reproposals`` (malformed proposals) and
  ``revisions`` (critique-revise bounces) each have their own bound. A variant uses each one
  independently, so two smoke repairs never starve a revision. The bound is inclusive: with
  ``max_repairs=1``, one repair is allowed.
* **Iterations (B19).** Every variant that reaches a result counts, whether it fails at dedup,
  at smoke or at the oracle. Counting happens at ``record``, after the fan-in. Variant ids are
  ``c<iterations + slot>``, so they are unique across iterations.
* **Wall clock (B19).** The clock starts at ``spend.first_started_at``, written once and kept in
  the checkpoint. A restart does not reset it. ``spend.elapsed_s`` is recomputed from it in
  ``brief`` and ``pace``.
* **Escalation (B20).** A fail streak at the threshold raises an ``interrupt``. The answer
  ``retry`` resets the streak and is recorded in ``decisions``. Any other answer stops the run.
* **Rate limits (B43).** Model calls carry a ``RetryPolicy`` whose predicate is
  ``kernel.faults.is_retryable``. The library owns the backoff. A rate-limited call is retried
  and never counts against the variant. Other failures propagate: the run stops, and proceed
  continues from the checkpoint.
* **Fan-out spend (B21).** Each branch returns its own spend. The sums happen in ``record``, after
  the fan-in. A failing branch leaves the completed branches' pending writes in the checkpoint, so
  they are not run again on proceed.
* **Soft passes.** The decision reads ``oracle.soft_passes()``, as in kernel/decision.py. The
  graph never names them.

Models: a variant is proposed and judged through ``cfg["model"]``, which only needs
``complete(prompt, schema) -> ModelResult``. The tests use FakeModel. The CLI's ``--mock`` mode
uses a scripted stand-in. No real model port is wired in this module.

Dependencies reach the nodes by closure, not through state. Sends carry only JSON-plain data,
and the checkpointer never sees an oracle or a model.

Standard library, LangGraph, and core.kernel only. No product name appears here.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import time
import traceback
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send, interrupt

from core.graph.state import SearchState, collect
from core.kernel import brief as kbrief
from core.kernel import decision as kdecision
from core.kernel import dedup as kdedup
from core.kernel.budget import refuse_unbounded, should_stop
from core.kernel.faults import is_retryable
from core.kernel.floors import apply_metric_floors
from core.kernel.oracle import VariantVerdict, OracleGateResult
from core.kernel.registry import Registry
from core.kernel.types import Spend

REGISTRY_NAME = "registry.jsonl"
STATUS_NAME = "STATUS.md"
PROPOSAL_SCHEMA = {"name": "proposal",
                   "required": ["cell", "hypothesis", "mechanism", "kill_criteria", "source"]}
JUDGEMENT_SCHEMA = {"name": "judgement",
                    "required": ["soft_passes", "gaming_clean", "mechanism_sentence",
                                 "structurally_promising", "exhausted"]}
_HALTED = ("abandoned", "complete")
_NUMERIC_SPEND = ("usd", "tokens", "model_calls", "tool_calls")


# ---------------------------------------------------------------------------
# Reducers and the variant state
# ---------------------------------------------------------------------------

def add_spend(old: Any, new: Any) -> dict:
    """Sum the numeric spend keys of a branch into the variant's running spend."""
    out = dict(old or {})
    for key, value in dict(new or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = out.get(key, 0) + value
    return out


class VariantState(TypedDict, total=False):
    """One variant's working state, inside the variant subgraph."""

    cid: str
    brief: str
    feedback: str
    repairs: int                    # smoke repairs used
    reproposals: int                # malformed proposals re-asked
    revisions: int                  # critique-revise bounces used
    problem: str                    # why the last proposal was malformed ("" when fine)
    proposal: dict | None           # the current proposal card (ProposalCard shape)
    passed: dict | None             # the last proposal that passed smoke; the only one recorded
    source_path: str | None
    smoke_ok: bool
    dedup: dict | None
    gate: dict | None               # OracleGateResult.to_dict()
    judgements: Annotated[list, collect]
    spend: Annotated[dict, add_spend]
    judgement: dict | None
    verdict: str | None
    reason: str
    voids_cell: bool
    target_pass: str | None
    revise: bool


class VariantOut(TypedDict, total=False):
    """What the subgraph hands the parent: one result, appended to the iteration's list."""

    variants: Annotated[list, collect]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def thread_id(project: str) -> str:
    return f"{project}/search"


def initial_state(project: str) -> dict:
    """The state a new search starts from. The runner passes it as the first input."""
    return {"project": project, "status": "in_progress", "stop_reason": None,
            "brief_digest": "", "variants": [], "proposals": [], "repairs": 0,
            "reproposals": 0, "revisions": 0, "fail_streak": 0,
            "spend": Spend().to_dict(), "decisions": []}


def aggregate_judgements(judgements: "list[dict]") -> "tuple[dict, float]":
    """Majority-aggregate N checker judgements into ``(aggregated, disagreement)``.

    Ported from the legacy core/loop/search.py. Booleans, top-level and per soft pass, take the
    majority vote. The mechanism sentence takes the most common string (ties go to the earliest).
    ``disagreement`` is the mean minority fraction over every voted field: 0.0 is unanimous, and
    the value tends to 0.5 as the split becomes maximal.
    """
    if not judgements:
        raise ValueError("aggregate_judgements needs at least one judgement")
    n = len(judgements)
    agg = dict(judgements[0])
    fractions: list[float] = []

    soft_keys: set = set()
    for judgement in judgements:
        soft_keys |= set((judgement.get("soft_passes") or {}).keys())
    agg_soft: dict = {}
    for key in sorted(soft_keys):
        yes = sum(1 for j in judgements if bool((j.get("soft_passes") or {}).get(key, False)))
        agg_soft[key] = yes > n / 2
        fractions.append(min(yes, n - yes) / n)
    agg["soft_passes"] = agg_soft

    for key in ("gaming_clean", "structurally_promising", "exhausted"):
        yes = sum(1 for j in judgements if bool(j.get(key, False)))
        agg[key] = yes > n / 2
        fractions.append(min(yes, n - yes) / n)

    sentences = [str(j.get("mechanism_sentence", "")) for j in judgements]
    counts: dict[str, int] = {}
    for sentence in sentences:
        counts[sentence] = counts.get(sentence, 0) + 1
    best = max(counts, key=lambda s: (counts[s], -sentences.index(s)))
    agg["mechanism_sentence"] = best
    fractions.append(1 - counts[best] / n)

    disagreement = sum(fractions) / len(fractions) if fractions else 0.0
    return agg, disagreement


def _now_iso(seconds: float) -> str:
    return _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc).isoformat()


def _parse_iso(text: str) -> float:
    return _dt.datetime.fromisoformat(text).timestamp()


def _spend_delta(result: Any) -> dict:
    spend = result.spend
    return {"usd": float(spend.usd), "tokens": int(spend.tokens),
            "model_calls": int(spend.model_calls), "tool_calls": int(spend.tool_calls)}


def _proposal_problem(data: Mapping, space: list) -> str:
    """Empty when the proposal has the shape the graph needs; otherwise the reason."""
    for key in PROPOSAL_SCHEMA["required"]:
        if key not in data:
            return f"missing field {key!r}"
    cell = str(data["cell"]).strip()
    if not cell:
        return "empty cell"
    if space and cell not in space:
        return f"cell {cell!r} is not in the proposal space"
    if not str(data["source"]).strip():
        return "empty source"
    return ""


def _proposal_prompt(cid: str, brief: str, feedback: str, space: list) -> str:
    lines = [f"Variant id: {cid}", "Propose one variant lever that is not a dead end.",
             brief]
    if space:
        lines.append("Allowed cells: " + ", ".join(space))
    if feedback:
        lines.append("Feedback on your last attempt:\n" + feedback)
    return "\n".join(lines)


def _judge_prompt(cid: str, proposal: Mapping | None, gate: Mapping | None) -> str:
    return "\n".join([f"Variant id: {cid}", "Judge the variant against its gate result.",
                      "Proposal: " + json.dumps(proposal or {}, sort_keys=True),
                      "Gate result: " + json.dumps(gate or {}, sort_keys=True)])


def _load_variant(path: Path, cid: str):
    """Smoke: import the variant file. Any exception is a smoke bug."""
    spec = importlib.util.spec_from_file_location(f"search_smoke_{cid}", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status_text(values: Mapping, registry: Registry, target: Any) -> str:
    """Render STATUS.md (legacy parity): the scoreboard, the run state and the last verdicts."""
    spend = dict(values.get("spend") or {})
    graduated = registry.graduated_ids()
    probation = registry.probation_ids()
    dead = sorted(registry.dead_end_cells())
    status = values.get("status") or "in_progress"
    reason = values.get("stop_reason")
    stop = f"{status}: {reason}" if reason else str(status)
    lines = ["# search: run status", "",
             f"**Graduated: {len(graduated)}/{'?' if target is None else target}**  ·  "
             f"iterations: {spend.get('iterations', 0)}  ·  "
             f"cost: ${float(spend.get('usd', 0.0)):.2f}",
             f"**Stop:** {stop}", "",
             f"- Graduated: {', '.join(graduated) or 'none'}",
             f"- Probation (re-queue): {', '.join(probation) or 'none'}",
             f"- Dead-end cells: {', '.join(dead) or 'none'}", "",
             "## Current iteration",
             f"- status **{status}** · fail streak {int(values.get('fail_streak', 0))} · "
             f"repairs {int(values.get('repairs', 0))} · "
             f"reproposals {int(values.get('reproposals', 0))} · "
             f"revisions {int(values.get('revisions', 0))}", "",
             "## Recent transitions"]
    verdicts = [e for e in registry.events() if e.get("kind") == "verdict"][-8:]
    if not verdicts:
        lines.append("- none")
    for event in verdicts:
        why = f": {event['why']}" if event.get("why") else ""
        lines.append(f"- {event['variant_id']} [{event['verdict']}]{why}")
    return "\n".join(lines) + "\n"


def _verdict_card(result: Mapping) -> dict:
    judgement = result.get("judgement") or {}
    return {"kind": "verdict", "variant_id": result["cid"], "verdict": result["verdict"],
            "why": result.get("reason", ""), "voids_cell": bool(result.get("voids_cell", True)),
            "target_pass": result.get("target_pass"),
            "soft_passes": dict(judgement.get("soft_passes") or {}),
            "gaming_clean": bool(judgement.get("gaming_clean", False)),
            "mechanism_sentence": str(judgement.get("mechanism_sentence", ""))}


class _Record:
    """Adapter so a plain card dict can be given to Registry.record (which calls to_dict)."""

    def __init__(self, payload: dict) -> None:
        self._payload = dict(payload)

    def to_dict(self) -> dict:
        return dict(self._payload)


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def build_search_graph(cfg: Mapping[str, Any]):
    """Compile the variant-search graph. ``cfg`` keys are listed in the module docstring.

    Required: ``project``, ``state_dir``, ``oracle``, ``model``, ``budget`` (bounded). Optional:
    ``checkpointer`` (default in-memory), ``clock`` (epoch seconds), ``slots`` (1),
    ``self_consistency`` (1), ``max_repairs`` (2), ``max_reproposals`` (1), ``max_revisions``
    (0, off), ``fail_streak_to_escalate`` (off), ``graduate_target`` (off), ``floors``,
    ``retry_initial_interval`` (0.5 s).
    """
    budget = cfg["budget"]
    refuse_unbounded(budget)
    project = str(cfg["project"])
    state_dir = Path(cfg["state_dir"])
    oracle = cfg["oracle"]
    model = cfg["model"]
    clock = cfg.get("clock") or time.time
    slots = int(cfg.get("slots", 1))
    if slots < 1:
        raise ValueError("slots must be at least 1")
    max_repairs = int(cfg.get("max_repairs", 2))
    max_reproposals = int(cfg.get("max_reproposals", 1))
    max_revisions = int(cfg.get("max_revisions", 0))
    samples = max(1, int(cfg.get("self_consistency", 1)))
    fail_streak_at = cfg.get("fail_streak_to_escalate")
    graduate_target = cfg.get("graduate_target")
    floors = dict(cfg.get("floors") or {})
    registry = Registry(state_dir / REGISTRY_NAME)
    space = list(oracle.proposal_space() or [])
    retry = RetryPolicy(retry_on=is_retryable, max_attempts=3,
                        initial_interval=float(cfg.get("retry_initial_interval", 0.5)))

    def cand_dir(cid: str) -> Path:
        return state_dir / "variants" / cid

    def write_status(values: Mapping) -> None:
        """STATUS.md next to registry.jsonl: rewritten at every halt check (brief, pace, escalate)."""
        state_dir.mkdir(parents=True, exist_ok=True)
        text = _status_text(values, registry, graduate_target)
        (state_dir / STATUS_NAME).write_text(text, encoding="utf-8")

    # -- elapsed time and halting, shared by brief and pace ------------------------------
    def halt(spend: Any, now: float):
        """Return (spend, status, reason). status is None while the run may continue."""
        spend = dict(spend or {})
        if spend.get("first_started_at") is None:
            spend["first_started_at"] = _now_iso(now)
        spend["elapsed_s"] = max(0.0, now - _parse_iso(spend["first_started_at"]))
        # The target is checked first: reaching it on the last allowed iteration is a success.
        if graduate_target is not None:
            graduated = len(registry.graduated_ids())
            if graduated >= graduate_target:
                return spend, "complete", (f"target: {graduated} graduated reaches "
                                           f"{graduate_target}")
        decision = should_stop(spend, budget)
        if decision.stop:
            return spend, "abandoned", decision.reason
        return spend, None, None

    # -- the variant subgraph ----------------------------------------------------------
    def propose(state: VariantState) -> dict:
        cid = state["cid"]
        prompt = _proposal_prompt(cid, state.get("brief", ""), state.get("feedback", ""), space)
        result = model.complete(prompt, PROPOSAL_SCHEMA)
        data = result.data if isinstance(result.data, Mapping) else {}
        spend = _spend_delta(result)
        problem = _proposal_problem(data, space)
        if problem:
            used = int(state.get("reproposals", 0)) + 1
            update = {"problem": problem, "reproposals": used, "proposal": None,
                      "smoke_ok": False, "verdict": None, "revise": False, "spend": spend,
                      "feedback": f"Your proposal was rejected: {problem}. Propose again."}
            if used > max_reproposals:
                update.update(verdict="REJECT", voids_cell=False,
                              reason=f"malformed proposal after {max_reproposals} "
                                     f"re-proposal(s): {problem}")
            return update
        directory = cand_dir(cid)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "variant.py"
        path.write_text(str(data["source"]), encoding="utf-8")
        card = {"kind": "proposal", "id": cid, "cell": str(data["cell"]).strip(),
                "hypothesis": str(data["hypothesis"]), "mechanism": str(data["mechanism"]),
                "kill_criteria": str(data["kill_criteria"])}
        return {"problem": "", "feedback": "", "proposal": card, "source_path": str(path),
                "smoke_ok": False, "verdict": None, "revise": False, "spend": spend}

    def dedup(state: VariantState) -> dict:
        card = state["proposal"]
        view = SimpleNamespace(cell=card["cell"], mechanism=card["mechanism"],
                               hypothesis=card["hypothesis"])
        result = kdedup.dedup_check(view, registry)
        update: dict = {"dedup": result.to_dict()}
        if not result.distinct:
            update.update(verdict="REJECT", voids_cell=True, reason=f"dedup: {result.reason}")
        return update

    def smoke(state: VariantState) -> dict:
        path = Path(state["source_path"])
        try:
            _load_variant(path, state["cid"])
        except Exception:  # noqa: BLE001, any failure of the variant file is a smoke bug
            used = int(state.get("repairs", 0)) + 1
            update = {"smoke_ok": False, "repairs": used, "proposal": None,
                      "feedback": "Smoke failed:\n" + traceback.format_exc()}
            if used > max_repairs:
                update.update(verdict="REJECT", voids_cell=False,
                              reason=f"smoke failed after {max_repairs} repair(s)")
            return update
        return {"smoke_ok": True, "passed": state.get("proposal"), "feedback": ""}

    def run_oracle(state: VariantState) -> dict:
        gate = oracle.run_gates(state["source_path"], {"project": project})
        if floors:
            gate = apply_metric_floors(gate, floors)
        update: dict = {"gate": gate.to_dict(), "judgements": None, "judgement": None}
        if not gate.tripwire_ok:
            tripped = ", ".join(sorted(k for k, v in gate.tripwires.items() if v))
            update.update(verdict="REJECT", voids_cell=False, reason=f"tripwire: {tripped}")
        return update

    def check_sample(state: VariantState) -> dict:
        prompt = _judge_prompt(state["cid"], state.get("proposal"), state.get("gate"))
        result = model.complete(prompt, JUDGEMENT_SCHEMA)
        data = dict(result.data) if isinstance(result.data, Mapping) else {}
        return {"judgements": [data], "spend": _spend_delta(result)}

    def decide(state: VariantState) -> dict:
        judgements = list(state.get("judgements") or [])
        agg, disagreement = aggregate_judgements(judgements)
        gate = OracleGateResult.from_dict(state["gate"])
        gaming_clean = bool(agg.get("gaming_clean", False))
        outcome = kdecision.decide(
            gate, oracle=oracle, soft_passes=dict(agg.get("soft_passes") or {}),
            gaming_clean=gaming_clean,
            mechanism_sentence=str(agg.get("mechanism_sentence", "")),
            structurally_promising=bool(agg.get("structurally_promising", False)),
            exhausted=bool(agg.get("exhausted", False)))
        judgement = dict(agg, self_consistency={"n": len(judgements),
                                                "disagreement": round(disagreement, 4)})
        update: dict = {"judgement": judgement, "verdict": outcome.verdict.value,
                        "reason": outcome.reason, "target_pass": outcome.target_pass,
                        "voids_cell": True, "revise": False}
        used = int(state.get("revisions", 0)) + 1
        revisable = (outcome.verdict is VariantVerdict.REJECT and gate.tripwire_ok
                     and gaming_clean)
        if revisable and used <= max_revisions:
            failing = [k for k, v in gate.passes.items() if not v]
            failing += [k for k, v in (agg.get("soft_passes") or {}).items() if not v]
            update.update(verdict=None, revise=True, revisions=used, reason="",
                          feedback=(f"REVISE: the verdict would be REJECT ({outcome.reason}). "
                                    f"Failing passes: {', '.join(failing) or 'none'}. "
                                    f"Revise the variant to address these."))
        return update

    def finish(state: VariantState) -> dict:
        result = {"cid": state["cid"], "verdict": state.get("verdict") or "REJECT",
                  "reason": state.get("reason", ""),
                  "voids_cell": bool(state.get("voids_cell", True)),
                  "target_pass": state.get("target_pass"),
                  "proposal": state.get("passed"), "gate": state.get("gate"),
                  "judgement": state.get("judgement"),
                  "repairs": int(state.get("repairs", 0)),
                  "reproposals": int(state.get("reproposals", 0)),
                  "revisions": int(state.get("revisions", 0)),
                  "spend": dict(state.get("spend") or {})}
        return {"variants": [result]}

    def after_propose(state: VariantState) -> str:
        if state.get("verdict"):
            return "finish"
        return "propose" if state.get("problem") else "dedup"

    def after_dedup(state: VariantState) -> str:
        return "finish" if state.get("verdict") else "smoke"

    def after_smoke(state: VariantState) -> str:
        if state.get("verdict"):
            return "finish"
        return "oracle" if state.get("smoke_ok") else "propose"

    def after_oracle(state: VariantState):
        if state.get("verdict"):
            return "finish"
        return [Send("check_sample", {"cid": state["cid"], "proposal": state.get("proposal"),
                                      "gate": state.get("gate")})
                for _ in range(samples)]

    def after_decide(state: VariantState) -> str:
        return "propose" if state.get("revise") else "finish"

    sub = StateGraph(VariantState, output_schema=VariantOut)
    sub.add_node("propose", propose, retry_policy=retry)
    sub.add_node("dedup", dedup)
    sub.add_node("smoke", smoke)
    sub.add_node("oracle", run_oracle)
    sub.add_node("check_sample", check_sample, retry_policy=retry)
    sub.add_node("decide", decide)
    sub.add_node("finish", finish)
    sub.add_edge(START, "propose")
    sub.add_conditional_edges("propose", after_propose, ["finish", "propose", "dedup"])
    sub.add_conditional_edges("dedup", after_dedup, ["finish", "smoke"])
    sub.add_conditional_edges("smoke", after_smoke, ["finish", "propose", "oracle"])
    sub.add_conditional_edges("oracle", after_oracle, ["finish", "check_sample"])
    sub.add_edge("check_sample", "decide")
    sub.add_conditional_edges("decide", after_decide, ["finish", "propose"])
    sub.add_edge("finish", END)
    variant = sub.compile()

    # -- the parent graph ----------------------------------------------------------------
    def brief(state: SearchState) -> dict:
        spend, status, reason = halt(state.get("spend"), float(clock()))
        update: dict = {"spend": spend, "variants": None}
        if status:
            update.update(status=status, stop_reason=reason)
        else:
            update.update(status="in_progress", stop_reason=None,
                          brief_digest=kbrief.dead_ends_digest(registry))
        write_status({**state, **update})
        return update

    def dispatch(state: SearchState):
        if state.get("status") in _HALTED:
            return END
        done = int((state.get("spend") or {}).get("iterations", 0))
        count = slots
        if budget.max_iterations is not None:
            count = min(count, budget.max_iterations - done)
        if count <= 0:
            return END
        return [Send("variant", {"cid": f"c{done + slot:03d}",
                                   "brief": state.get("brief_digest", "")})
                for slot in range(count)]

    def record(state: SearchState) -> dict:
        results = sorted(state.get("variants") or [], key=lambda r: r["cid"])
        recorded = {e["id"] for e in registry.events() if e.get("kind") == "proposal"}
        latest: dict[str, str] = {}
        for event in registry.events():
            if event.get("kind") == "verdict":
                latest[event["variant_id"]] = event["verdict"]
        proposals = []
        for result in results:
            card = result.get("proposal")
            if card:
                if card["id"] not in recorded:
                    registry.record(_Record(card))
                    recorded.add(card["id"])
                proposals.append(card)
        for result in results:
            if latest.get(result["cid"]) != result["verdict"]:
                registry.record(_Record(_verdict_card(result)))
        streak = int(state.get("fail_streak", 0))
        spend = dict(state.get("spend") or {})
        for result in results:
            streak = streak + 1 if result["verdict"] == "REJECT" else 0
            spend = add_spend(spend, result.get("spend"))
        spend["iterations"] = int(spend.get("iterations", 0)) + len(results)
        return {"proposals": proposals, "fail_streak": streak, "spend": spend,
                "repairs": int(state.get("repairs", 0))
                + sum(r.get("repairs", 0) for r in results),
                "reproposals": int(state.get("reproposals", 0))
                + sum(r.get("reproposals", 0) for r in results),
                "revisions": int(state.get("revisions", 0))
                + sum(r.get("revisions", 0) for r in results)}

    def pace(state: SearchState) -> dict:
        spend, status, reason = halt(state.get("spend"), float(clock()))
        update: dict = {"spend": spend}
        if status:
            update.update(status=status, stop_reason=reason)
        write_status({**state, **update})
        return update

    def after_pace(state: SearchState) -> str:
        if state.get("status") in _HALTED:
            return END
        if fail_streak_at and int(state.get("fail_streak", 0)) >= int(fail_streak_at):
            return "escalate"
        return "brief"

    def escalate(state: SearchState) -> dict:
        streak = int(state.get("fail_streak", 0))
        answer = str(interrupt({"kind": "escalation", "fail_streak": streak,
                                "threshold": fail_streak_at,
                                "options": ["retry", "stop"],
                                "summary": f"{streak} consecutive REJECT verdicts"}))
        record_of_answer = {"kind": "escalation", "fail_streak": streak, "answer": answer}
        if answer == "retry":
            update = {"fail_streak": 0, "decisions": [record_of_answer]}
        else:
            update = {"status": "escalated", "decisions": [record_of_answer],
                      "stop_reason": f"escalation: fail streak {streak}, stopped by a human"}
        write_status({**state, **update})
        return update

    def after_escalate(state: SearchState) -> str:
        return END if state.get("status") in _HALTED + ("escalated",) else "brief"

    graph = StateGraph(SearchState)
    graph.add_node("brief", brief)
    graph.add_node("variant", variant)
    graph.add_node("record", record)
    graph.add_node("pace", pace)
    graph.add_node("escalate", escalate)
    graph.add_edge(START, "brief")
    graph.add_conditional_edges("brief", dispatch, ["variant", END])
    graph.add_edge("variant", "record")
    graph.add_edge("record", "pace")
    graph.add_conditional_edges("pace", after_pace, ["brief", "escalate", END])
    graph.add_conditional_edges("escalate", after_escalate, ["brief", END])
    return graph.compile(checkpointer=cfg.get("checkpointer") or InMemorySaver())
