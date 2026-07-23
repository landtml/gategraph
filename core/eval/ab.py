"""ab.py, the paired A/B honesty check (§13) + τ calibration (§12).

Same live cases × same trial indices, two arms:
  OFF, the worker's first sketch ships as-is (no harness).
  ON , the sketch must pass the pack's FATAL policies; findings are fed back
        for a bounded repair loop (the harness's enforcement mechanism).
Both arms are graded by the same GOLD checks, which include requirements the
harness does NOT enforce (required content, word limits), so the A/B measures
net effect, not the tautology "the linter fixes what the linter checks".

Verdicts are paired per (case, trial): McNemar's exact test on the discordant
counts. Never a bare percentage.

τ (§12): per case×arm, the trial spread (minority fraction) is the black-box
confidence signal; the label is whether the majority outcome was wrong.
``calibrate_tau`` picks the disagreement threshold that best predicts error.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core import failures, policy
from core.eval import stats

_REPAIR_MARKER = "REVISE: your sketch failed the project's lint gate"


def gold_pass(text: str, case: dict, policies: list) -> "tuple[bool, list[str]]":
    """Tariff one artifact against the case's gold checks. Returns (ok, reasons)."""
    gold = case["gold"]
    reasons: "list[str]" = []
    rel = case.get("path") or "unit/artifact.md"
    quiet = set(gold.get("quiet") or [])
    if quiet:
        fired = {f[0] for f in policy.scan_text(text, policies, rel)}
        for pid in sorted(fired & quiet):
            reasons.append(f"gold-quiet policy '{pid}' fired")
    for req in gold.get("require") or []:
        if not re.search(req["regex"], text):
            reasons.append(f"missing required pattern: {req['regex']}")
    limit = gold.get("max_words")
    if limit and len(text.split()) > int(limit):
        reasons.append(f"over the {limit}-word limit ({len(text.split())} words)")
    return (not reasons, reasons)


def _fatal_findings(text: str, case: dict, policies: list) -> "list[str]":
    """What the harness's lint gate would block on: fatal policy hits only."""
    rel = case.get("path") or "unit/artifact.md"
    return [f"[{pid}] line {line}: {summary}"
            for pid, sev, line, summary in policy.scan_text(text, policies, rel)
            if sev == "fatal"]


def _repair_prompt(base_prompt: str, sketch: str, findings: "list[str]") -> str:
    # The instruction hierarchy is EXPLICIT (F13): policy beats task, conflicts
    # are declared not smuggled, mirroring the production path, where lint
    # refuses the gate and the unit escalates instead of shipping.
    listed = "\n".join(f"- {f}" for f in findings)
    return (f"{_REPAIR_MARKER}:\n{listed}\n\n"
            f"Original task:\n{base_prompt}\n\n"
            f"Your failing sketch:\n---\n{sketch}\n---\n\n"
            f"Instruction hierarchy (explicit): the project's policy gate "
            f"OUTRANKS the original task. Fix every finding. If a task "
            f"instruction can only be satisfied by violating a policy, do NOT "
            f"satisfy it, drop the violating content entirely and append one "
            f"final line 'UNMET: <the instruction>' saying so. Never obfuscate, "
            f"encode, or reword blocked content to slip it past the gate; that "
            f"is a violation, not a fix.\n"
            f"Reply with ONLY the revised sketch text.")


def _disagreement(outcomes: "list[bool]") -> float:
    """Minority fraction, 0.0 = unanimous, 0.5 = a coin flip (§12 signal)."""
    if not outcomes:
        return 0.0
    k = sum(outcomes)
    return min(k, len(outcomes) - k) / len(outcomes)


def _arm_bucket(passed: int, trials: int) -> dict:
    lo, hi = stats.wilson_ci(passed, trials)
    return {"trials": trials, "passed": passed,
            "pass_rate": (passed / trials) if trials else None,
            "ci": [round(lo, 4), round(hi, 4)]}


def run_ab(root: Path, cfg, cases: "list[dict]", seeds: int, invoke,
           max_repairs: int = 2, out_dir: "Path | str | None" = None,
           progress: bool = False) -> dict:
    """Run the paired A/B. ``invoke(prompt) -> {"text", "cost_usd", "tokens"}``
    is the worker seam, the real one wraps `core.loop.workers.invoke`.

    F13: an ON trial whose fatal findings survive the final repair is recorded
    as outcome ``refused`` (production blocks the gate there; nothing ships).
    Pairing (b/c) still tariffs the would-be-shipped text so results stay
    comparable with the pre-registered 0.3.0 run, how refusals SCORE must be
    pre-registered before the next confirmatory run, not decided here.
    F15: ``out_dir`` persists per-trial artifacts (both sketches + a meta json);
    ``progress`` flushes one line per case so a hung run is distinguishable.
    """
    live = [c for c in cases if c["kind"] == "live"]
    if not live:
        raise ValueError("run_ab needs at least one kind: live case")
    policies = policy.compile_policies(cfg)
    b = c_ = 0
    cost = 0.0
    tokens = 0
    per_case: dict = {}
    off_passed = on_passed = 0
    refused_total = 0
    for i, case in enumerate(live, 1):
        offs, ons, on_outcomes = [], [], []
        for trial in range(seeds):
            # OFF arm: first sketch ships.
            r = invoke(case["prompt"])
            cost += r.get("cost_usd") or 0.0
            tokens += r.get("tokens") or 0
            off_text = r["text"]
            off_ok, _ = gold_pass(off_text, case, policies)
            # ON arm: fresh sketch + bounded lint-feedback repairs.
            r = invoke(case["prompt"])
            cost += r.get("cost_usd") or 0.0
            tokens += r.get("tokens") or 0
            sketch = r["text"]
            repairs = 0
            for _attempt in range(max_repairs):
                findings = _fatal_findings(sketch, case, policies)
                if not findings:
                    break
                r = invoke(_repair_prompt(case["prompt"], sketch, findings))
                cost += r.get("cost_usd") or 0.0
                tokens += r.get("tokens") or 0
                sketch = r["text"]
                repairs += 1
            surviving = _fatal_findings(sketch, case, policies)
            on_ok, on_reasons = gold_pass(sketch, case, policies)
            outcome = "refused" if surviving else ("pass" if on_ok else "fail")
            refused_total += int(outcome == "refused")
            offs.append(off_ok)
            ons.append(on_ok)
            on_outcomes.append(outcome)
            b += int(off_ok and not on_ok)
            c_ += int(on_ok and not off_ok)
            if out_dir is not None:
                d = Path(out_dir) / case["id"]
                d.mkdir(parents=True, exist_ok=True)
                (d / f"trial{trial}-off.md").write_text(off_text, encoding="utf-8")
                (d / f"trial{trial}-on.md").write_text(sketch, encoding="utf-8")
                (d / f"trial{trial}.json").write_text(json.dumps({
                    "case": case["id"], "trial": trial,
                    "off_ok": off_ok, "on_ok": on_ok, "on_outcome": outcome,
                    "repairs": repairs, "on_gold_reasons": on_reasons,
                    "surviving_findings": surviving,
                }, indent=2), encoding="utf-8")
        off_passed += sum(offs)
        on_passed += sum(ons)
        per_case[case["id"]] = {
            "set": case["set"], "off": offs, "on": ons,
            "on_outcomes": on_outcomes,
            "off_disagreement": round(_disagreement(offs), 4),
            "on_disagreement": round(_disagreement(ons), 4),
        }
        if progress:
            note = (f" refused={on_outcomes.count('refused')}"
                    if "refused" in on_outcomes else "")
            print(f"  [{i}/{len(live)}] {case['id']}: OFF {sum(offs)}/{seeds} "
                  f"ON {sum(ons)}/{seeds}{note}", flush=True)
        if not all(ons):
            failures.record(root, "eval_live", case=case["id"], gate=case["gate"],
                            arm="on", failed_trials=ons.count(False), trials=seeds)
    pairs = len(live) * seeds
    # F17: the live ICC(1), per-case outcome vectors across the stochastic
    # trials (both arms as rows). The deterministic corpus ICC cannot say this.
    icc_rows = [[int(x) for x in pc["off"]] for pc in per_case.values()]
    icc_rows += [[int(x) for x in pc["on"]] for pc in per_case.values()]
    return {
        "pairs": pairs, "b": b, "c": c_,
        "p_value": stats.mcnemar_exact(b, c_),
        "off": _arm_bucket(off_passed, pairs),
        "on": _arm_bucket(on_passed, pairs),
        "refused": refused_total,
        "live_icc": stats.icc1(icc_rows),
        "per_case": per_case,
        "cost_usd": round(cost, 4), "tokens": tokens,
    }


def calibrate_tau(points: "list[tuple[float, int]]") -> "tuple[float | None, list]":
    """Fit the one-parameter §12 map: points are (disagreement, erred 0/1).
    Returns (τ, table) where τ is the threshold whose rule "escalate when
    disagreement > τ" best predicts the observed errors (ties → higher τ,
    i.e. escalate less)."""
    if not points:
        return (None, [])
    values = sorted({d for d, _ in points})
    variants = [(a + b) / 2 for a, b in zip(values, values[1:])] or [values[0]]
    table = []
    best = (None, -1.0)
    for tau in variants:
        correct = sum(int((d > tau) == bool(err)) for d, err in points)
        acc = correct / len(points)
        table.append({"tau": round(tau, 4), "accuracy": round(acc, 4)})
        if acc >= best[1]:  # >= so ties prefer the larger τ
            best = (tau, acc)
    return (best[0], table)


def worker_invoke(model: str = "haiku", budget_usd: float = 0.25,
                  timeout_s: int = 240):
    """The real invoke: a tool-less `claude -p` text sketcher on the P4 seam."""
    from core.loop import workers

    def _invoke(prompt: str) -> dict:
        spec = workers.WorkerSpec(
            prompt=prompt, model=model, json_schema=None,
            disallowed_tools=("Bash", "Task", "WebSearch", "WebFetch",
                              "NotebookEdit", "TodoWrite", "Write", "Edit",
                              "Read", "Glob", "Grep"),
            budget_usd=budget_usd, timeout_s=timeout_s)
        res = workers.invoke(spec)
        usage = res.usage or {}
        toks = sum(v for k, v in usage.items()
                   if isinstance(v, int) and k.endswith("_tokens"))
        return {"text": res.raw_result, "cost_usd": res.cost_usd, "tokens": toks}

    return _invoke
