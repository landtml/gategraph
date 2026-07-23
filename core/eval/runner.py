"""runner.py, the deterministic corpus runner (§13's `make eval` engine).

Runs every detection + machinery case at N seeded trials against the REAL
policy engine and manifest code (never a reimplementation), and reports the
three honest numbers: pass-rate with a Wilson CI, ICC(1) across seeds, and the
per-gate failure histogram, the number that reveals which gates are theater.
Every failed case lands in the failure ledger. Live cases belong to the A/B
(`core/eval/ab.py`) and are skipped here.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from core import failures, manifest, policy
from core.eval import corpus, stats


# --- machinery scenarios: the evidence chain exercised via the real Manifest ---


def _mach_out_of_order(cfg, unit_dir: Path) -> bool:
    if len(cfg.gates) < 2:
        return False
    m = manifest.Manifest.new("evalmach", unit_dir, cfg.gates, cfg.hashed_gates)
    try:
        m.set_gate(cfg.gates[1])
        return False  # skipping a predecessor must refuse
    except manifest.ManifestError:
        return True


def _mach_staleness(cfg, unit_dir: Path) -> bool:
    hashed = [g for g in cfg.gates if g in cfg.hashed_gates]
    if not hashed:
        return False
    g = hashed[0]
    f = unit_dir / "artifact.md"
    f.write_text("version one\n", encoding="utf-8")
    m = manifest.Manifest.new("evalmach", unit_dir, cfg.gates, cfg.hashed_gates)
    for prior in cfg.gates[:cfg.gates.index(g)]:
        m.set_gate(prior)
    m.set_gate(g, sha256={"artifact.md": manifest.sha256_file(f)})
    if not m.gate_passed(g):
        return False
    f.write_text("version two\n", encoding="utf-8")
    return g in m.stale_gates() and not m.gate_passed(g)


def _mach_reset_invalidates(cfg, unit_dir: Path) -> bool:
    if len(cfg.gates) < 2:
        return False
    m = manifest.Manifest.new("evalmach", unit_dir, cfg.gates, cfg.hashed_gates)
    m.set_gate(cfg.gates[0])
    m.set_gate(cfg.gates[1])
    m.set_gate(cfg.gates[0])  # re-earning an early gate…
    return not m.gate_passed(cfg.gates[1])  # …must drop everything after it


def _mach_escalate_terminal(cfg, unit_dir: Path) -> bool:
    m = manifest.Manifest.new("evalmach", unit_dir, cfg.gates, cfg.hashed_gates)
    m.escalate("eval scenario")
    return m.status == "escalated" and m.data["escalation"]["note"] == "eval scenario"


_SCENARIOS = {
    "out_of_order_refused": _mach_out_of_order,
    "staleness_reopens": _mach_staleness,
    "reset_invalidates_later": _mach_reset_invalidates,
    "escalate_is_terminal": _mach_escalate_terminal,
}


# --- trials --------------------------------------------------------------------


def _detection_trial(case: dict, policies: list, seed: int) -> bool:
    text = corpus.perturb(case["artifact"], seed)
    fired = {f[0] for f in policy.scan_text(text, policies, case["path"])}
    exp = case["expect"]
    if any(p not in fired for p in exp["fire"]):
        return False
    if "*" in exp["quiet"]:
        return not fired
    return not any(p in fired for p in exp["quiet"])


def _machinery_trial(case: dict, cfg) -> bool:
    fn = _SCENARIOS[case["scenario"]]
    with tempfile.TemporaryDirectory() as td:
        return fn(cfg, Path(td))


def _bucket(records: "list[dict]") -> dict:
    trials = sum(len(r["passes"]) for r in records)
    passed = sum(sum(r["passes"]) for r in records)
    lo, hi = stats.wilson_ci(passed, trials)
    return {"n_cases": len(records), "trials": trials, "passed": passed,
            "pass_rate": (passed / trials) if trials else None,
            "ci": [round(lo, 4), round(hi, 4)]}


def run_corpus(root: Path, cfg, cases: "list[dict]", seeds: int = 5) -> dict:
    """Run all deterministic cases × seeds. Returns the JSON-serializable report."""
    policies = policy.compile_policies(cfg)
    case_recs: dict = {}
    histogram: dict = {}
    live_skipped = 0
    for case in cases:
        if case["kind"] == "live":
            live_skipped += 1
            continue
        passes = []
        for s in range(seeds):
            if case["kind"] == "detection":
                passes.append(_detection_trial(case, policies, s))
            else:
                passes.append(_machinery_trial(case, cfg))
        rec = {"kind": case["kind"], "gate": case["gate"], "set": case["set"],
               "adversarial": bool(case.get("adversarial")), "passes": passes}
        case_recs[case["id"]] = rec
        n_fail = passes.count(False)
        if n_fail:
            histogram[case["gate"]] = histogram.get(case["gate"], 0) + n_fail
            failures.record(root, "eval_case", case=case["id"], gate=case["gate"],
                            case_kind=case["kind"], failed_trials=n_fail, trials=seeds)
    rows = [[int(p) for p in r["passes"]] for r in case_recs.values()]
    report = {
        "trials_per_case": seeds,
        "cases": case_recs,
        "overall": _bucket(list(case_recs.values())),
        "sets": {s: _bucket([r for r in case_recs.values() if r["set"] == s])
                 for s in ("iterate", "heldout")},
        "adversarial": _bucket([r for r in case_recs.values() if r["adversarial"]]),
        "icc": stats.icc1(rows),
        "histogram": dict(sorted(histogram.items())),
        "live_cases_skipped": live_skipped,
    }
    return report
