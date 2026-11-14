"""runner_v2.py, the deterministic corpus runner on the new engine (V1).

The same contract as the legacy runner (deleted at the legacy-0.4 freeze), run against
the new engine:

- ``detection`` cases go through ``core.kernel.policy`` (scan_text) with the policies that
  ``core.kernel.config.load`` reads from the pack;
- ``machinery`` cases run through the unit graph (core/graph/unit.py), one thread per trial,
  with the pack's gate chain. Every check command is replaced with a passing stub (``true``),
  except in the escalation scenario, where the first check gate fails (``false``) with
  ``max_attempts: 1``. The scenarios test the evidence chain; the commands are not the subject;
- ``live`` cases are skipped.

Importable and runnable on system python3 (stdlib + PyYAML only): LangGraph is imported lazily,
only when a machinery case needs it. Without LangGraph (no venv) the machinery cases are reported
as ``skipped (no venv)`` and counted in neither pass nor fail.

Output: the same bucket report as the legacy runner (pass-rate with a Wilson CI, ICC(1) across
seeds, the per-gate failure histogram). With ``root``, every failed case is appended to the
failure ledger (``harness/state/failures.jsonl``) through ``core.kernel.ledger.record``.

CLI: ``.venv/bin/python core/eval/runner_v2.py --pack eval/pack_v2 --corpus eval/corpus``.
Exit 1 on any failed trial.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.eval import corpus, stats  # noqa: E402
from core.kernel import config, ledger, policy  # noqa: E402
from core.kernel.types import Budget, CheckSpec, RepairSpec, Severity  # noqa: E402

THREAD = "eval/u1"
_DIGEST = hashlib.sha256(b"eval pack schema 2 configuration").hexdigest()
_BY = "eval"
_PASSING = "true"
_FAILING = "false"

# LangGraph-bound names. None until _load_langgraph() binds them (only in the venv).
Command = graph_runner = make_checkpointer = build_unit_graph = initial_state = None


def _load_langgraph() -> bool:
    """Bind the LangGraph machinery. False when langgraph is not installed (no venv).

    Only the ``langgraph`` import is guarded: an ImportError inside core.graph is a real bug
    and propagates.
    """
    global Command, graph_runner, make_checkpointer, build_unit_graph, initial_state
    if build_unit_graph is not None:
        return True
    try:
        from langgraph.types import Command as _Command
    except ImportError:
        return False
    from core.graph import runner as _graph_runner
    from core.graph.store import make_checkpointer as _make_checkpointer
    from core.graph.unit import build_unit_graph as _build_unit_graph, initial_state as _initial_state
    Command, graph_runner = _Command, _graph_runner
    make_checkpointer, build_unit_graph, initial_state = (_make_checkpointer, _build_unit_graph,
                                                          _initial_state)
    return True


# --- machinery: one unit graph per trial ----------------------------------------


def _config_for_trial(cfg, root: Path, failing_gate: str | None = None):
    """The pack's gate chain with stub check commands, rooted in the trial directory."""
    checks = tuple(CheckSpec(id=c.id, severity=Severity.FATAL,
                             command=_FAILING if c.id == failing_gate else _PASSING)
                   for c in cfg.checks)
    gates = tuple(dataclasses.replace(g, repair=RepairSpec(max_attempts=1))
                  if g.id == failing_gate else g for g in cfg.gates)
    return dataclasses.replace(cfg, root=root, checks=checks, gates=gates)


def _answer(item: dict) -> dict:
    """The external actor: attempt the work, approve approvals, abandon escalations."""
    kind = item["kind"]
    if kind == "work":
        return {"answer": "attempt", "by": _BY, "channel": "tty", "note": None}
    if kind == "approval":
        return {"answer": "approve", "by": _BY, "channel": "tty", "note": None}
    if kind == "escalation":
        return {"answer": "abandon", "by": _BY, "channel": "tty", "note": "eval scenario"}
    raise RuntimeError(f"unexpected interrupt kind {kind!r}")


class _Unit:
    """One unit in a trial directory, driven through the graph by the external actor."""

    def __init__(self, cfg, root: Path, failing_gate: str | None = None):
        self.cfg = _config_for_trial(cfg, root, failing_gate)
        self.unit_dir = root / "units" / "u1"
        self.unit_dir.mkdir(parents=True)
        self.artifact = self.unit_dir / "artifact.md"
        self.artifact.write_text("version one\n", encoding="utf-8")
        self.lock_dir = root / "locks"
        self.graph = build_unit_graph({
            "config": self.cfg,
            "unit_dir": self.unit_dir,
            "state_dir": root / "state",
            "config_digest": _DIGEST,
            "budget": Budget(max_iterations=200, max_model_calls=100),
            "checkpointer": make_checkpointer({"in_memory": True}),
        })

    def _run(self, value):
        return graph_runner.run(self.graph, THREAD, value, lambda snapshot: None,
                                lock_dir=self.lock_dir)

    def start(self) -> None:
        self._run(initial_state(project=self.cfg.project, unit="u1", config_digest=_DIGEST))

    def pending(self) -> list:
        return graph_runner.pending(self.graph, THREAD)

    def values(self) -> dict:
        return self.graph.get_state({"configurable": {"thread_id": THREAD}}).values

    def proceed(self, item: dict) -> None:
        """Answer one interrupt by its payload, whether or not the graph still lists it."""
        self._run(Command(resume=_answer(item)))

    def answer_one(self) -> bool:
        items = self.pending()
        if not items:
            return False
        self.proceed(items[0])
        return True

    def advance_until(self, gate: str, limit: int = 40) -> bool:
        for _ in range(limit):
            if gate in (self.values().get("effective") or ()):
                return True
            if not self.answer_one():
                return False
        return False

    def evidence_gates(self) -> list:
        return [rec["gate"] for rec in self.values()["evidence"]]


def _gates_with_surface(cfg) -> list:
    return [g for g in cfg.gates if g.surface is not None and g.surface.include]


def _scn_out_of_order(cfg, root: Path) -> bool:
    if len(cfg.gates) < 2:
        return False
    unit = _Unit(cfg, root)
    unit.start()
    waiting = unit.pending()[0]
    unit.graph.update_state({"configurable": {"thread_id": THREAD}},
                            {"target_gate": cfg.gates[1].id})
    unit.proceed(waiting)  # the injected target meets the answer to the waiting interrupt
    values = unit.values()
    refused = any(f.get("rule") == "seal.needs_not_effective"
                  for f in (values.get("findings") or ()))
    return not unit.evidence_gates() and not values.get("effective") and refused


def _scn_staleness(cfg, root: Path) -> bool:
    hashed = _gates_with_surface(cfg)
    if not hashed:
        return False
    gate = hashed[0].id
    unit = _Unit(cfg, root)
    unit.start()
    if not unit.advance_until(gate):
        return False
    unit.artifact.write_text("version two\n", encoding="utf-8")
    for _ in range(3):  # the next attempt meets the edit; reconcile marks the gate stale
        if gate in (unit.values().get("stale") or ()):
            break
        if not unit.answer_one():
            break
    values = unit.values()
    return gate in (values.get("stale") or ()) and gate not in (values.get("effective") or ())


def _scn_reset_invalidates(cfg, root: Path) -> bool:
    if len(cfg.gates) < 2:
        return False
    first, second = cfg.gates[0].id, cfg.gates[1].id
    unit = _Unit(cfg, root)
    unit.start()
    if not unit.advance_until(second):
        return False
    waiting = unit.pending()[0]
    unit.graph.update_state({"configurable": {"thread_id": THREAD}}, {"target_gate": first})
    unit.proceed(waiting)
    for _ in range(6):  # re-earn the first gate; the approval or work answers come in turn
        if unit.evidence_gates()[-1:] == [first] and unit.evidence_gates().count(first) == 2:
            break
        if not unit.answer_one():
            break
    effective = unit.values().get("effective") or ()
    return first in effective and second not in effective


def _scn_escalate_terminal(cfg, root: Path) -> bool:
    failing = next((g.id for g in cfg.gates if g.checks), None)
    if failing is None:
        return False
    unit = _Unit(cfg, root, failing_gate=failing)
    unit.start()
    for _ in range(40):
        if not unit.answer_one():
            break
    values = unit.values()
    kinds = [rec["kind"] for rec in values["evidence"]]
    return (values.get("status") == "abandoned" and kinds.count("escalation") == 1
            and kinds[-1:] == ["escalation"]
            and failing not in (values.get("effective") or ()) and not unit.pending())


_SCENARIOS = {
    "out_of_order_refused": _scn_out_of_order,
    "staleness_reopens": _scn_staleness,
    "reset_invalidates_later": _scn_reset_invalidates,
    "escalate_is_terminal": _scn_escalate_terminal,
}


def _machinery_trial(case: dict, cfg) -> bool:
    fn = _SCENARIOS[case["scenario"]]
    with tempfile.TemporaryDirectory() as td:
        return fn(cfg, Path(td))


# --- detection ------------------------------------------------------------------


def _detection_trial(case: dict, policies: list, seed: int) -> bool:
    text = corpus.perturb(case["artifact"], seed)
    fired = {f.rule for f in policy.scan_text(text, policies, case["path"])}
    exp = case["expect"]
    if any(p not in fired for p in exp["fire"]):
        return False
    if "*" in exp["quiet"]:
        return not fired
    return not any(p in fired for p in exp["quiet"])


# --- the corpus -----------------------------------------------------------------


def _bucket(records: "list[dict]") -> dict:
    trials = sum(len(r["passes"]) for r in records)
    passed = sum(sum(r["passes"]) for r in records)
    lo, hi = stats.wilson_ci(passed, trials)
    return {"n_cases": len(records), "trials": trials, "passed": passed,
            "pass_rate": (passed / trials) if trials else None,
            "ci": [round(lo, 4), round(hi, 4)]}


def run_corpus(pack_root, corpus_dir, *, seeds: int = 5, root: Path | None = None) -> dict:
    """Run every non-live case × ``seeds`` through the new engine. Returns the report dict.

    Machinery cases need LangGraph; without it they are left out of every bucket and counted in
    ``machinery_skipped``. With ``root``, each failed case lands in that project's failure ledger.
    """
    cfg = config.load(Path(pack_root))
    policies = policy.compile_policies(cfg)
    cases = corpus.load_corpus(Path(corpus_dir))
    have_langgraph = None
    case_recs: dict = {}
    histogram: dict = {}
    live_skipped = 0
    machinery_skipped = 0
    for case in cases:
        if case["kind"] == "live":
            live_skipped += 1
            continue
        if case["kind"] == "machinery":
            if have_langgraph is None:
                have_langgraph = _load_langgraph()
            if not have_langgraph:
                machinery_skipped += 1
                continue
        passes = []
        for s in range(seeds):
            if case["kind"] == "detection":
                passes.append(_detection_trial(case, policies, s))
            else:
                passes.append(_machinery_trial(case, cfg))
        case_recs[case["id"]] = {"kind": case["kind"], "gate": case["gate"], "set": case["set"],
                                 "adversarial": bool(case.get("adversarial")), "passes": passes}
        n_fail = passes.count(False)
        if n_fail:
            histogram[case["gate"]] = histogram.get(case["gate"], 0) + n_fail
            if root is not None:
                ledger.record(root, "eval_case", case=case["id"], gate=case["gate"],
                              case_kind=case["kind"], failed_trials=n_fail, trials=seeds)
    rows = [[int(p) for p in r["passes"]] for r in case_recs.values()]
    return {
        "trials_per_case": seeds,
        "cases": case_recs,
        "overall": _bucket(list(case_recs.values())),
        "sets": {s: _bucket([r for r in case_recs.values() if r["set"] == s])
                 for s in ("iterate", "heldout")},
        "adversarial": _bucket([r for r in case_recs.values() if r["adversarial"]]),
        "icc": stats.icc1(rows),
        "histogram": dict(sorted(histogram.items())),
        "live_cases_skipped": live_skipped,
        "machinery_skipped": machinery_skipped,
    }


def _fmt_bucket(name: str, bucket: dict) -> str:
    if not bucket["trials"]:
        return f"{name:<13} (no cases)"
    lo, hi = bucket["ci"]
    return (f"{name:<13} {bucket['passed']}/{bucket['trials']} passed, "
            f"{bucket['pass_rate']:.1%} (CI {lo:.1%}\u2013{hi:.1%})")


def format_report(rep: dict, ledger_path: "Path | None" = None) -> list:
    """The human-readable report, one string per line (shared by `make eval` and cli.py run)."""
    kinds: dict = {}
    for rec in rep["cases"].values():
        kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
    kind_text = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
    lines = [f"eval: {len(rep['cases'])} cases ({kind_text}; "
             f"{rep['live_cases_skipped']} live skipped) × {rep['trials_per_case']} seeds"]
    if rep["machinery_skipped"]:
        lines.append(f"machinery: {rep['machinery_skipped']} cases skipped (no venv); neither "
                     "passed nor failed. Run them with .venv/bin/python (make eval).")
    lines.append(_fmt_bucket("overall:", rep["overall"]))
    for s in ("iterate", "heldout"):
        lines.append(_fmt_bucket(f"{s}:", rep["sets"][s]))
    lines.append(_fmt_bucket("adversarial:", rep["adversarial"]))
    icc = rep["icc"]
    # F17: perturbations are verdict-preserving BY DESIGN, so 1.000 here is a
    # determinism check, not agent-level reliability.
    lines.append(f"ICC(1): {icc:.3f} (determinism check: 1.000 expected; "
                 "no live run here)" if icc is not None
                 else "ICC(1): n/a (needs ≥2 cases and ≥2 seeds)")
    if rep["histogram"]:
        lines.append("gate-failure histogram (failed trials per gate):")
        for gate, n in rep["histogram"].items():
            lines.append(f"  {gate:<20} {n}")
        failed = sorted(cid for cid, r in rep["cases"].items() if not all(r["passes"]))
        lines.append("failed cases: " + ", ".join(failed))
        if ledger_path is not None:
            lines.append(f"→ failures appended to {ledger_path}")
    else:
        lines.append("gate-failure histogram: empty, no failed trials")
    return lines


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=str(root),
                   help="project root for the failure ledger (harness/state/failures.jsonl)")
    p.add_argument("--pack", default=str(root / "eval" / "pack_v2"),
                   help="pack root holding harness/adapter/adapter.yaml (schema 2)")
    p.add_argument("--corpus", default=str(root / "eval" / "corpus"))
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--out", default=None, help="also write the JSON report here")
    args = p.parse_args(argv)
    project = Path(args.root).resolve()
    rep = run_corpus(args.pack, args.corpus, seeds=args.seeds, root=project)
    failed = bool(rep["histogram"])
    for line in format_report(rep, ledger.failures_path(project) if failed else None):
        print(line)
    if args.out:
        Path(args.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"report written: {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
