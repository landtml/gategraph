#!/usr/bin/env python3
"""cli.py, `make eval` entry (§13): corpus run + the paired live A/B.

  run , deterministic corpus × N seeds; prints pass-rate with a 95% CI,
         ICC(1) across seeds, and the per-gate failure histogram. Exit 1 on
         any case regression (this is the anti-rot net for the policy pack).
  ab  , the paired McNemar A/B on the corpus's live cases (real `claude -p`
         workers; costs money, never run implicitly) + τ calibration data.

Layout conventions:
  --root    project root (ledger + default corpus/pack location)
  --corpus  case dir; default <root>/harness/eval/corpus, else <root>/eval/corpus
  --pack    policy-pack root holding harness/adapter/adapter.yaml;
            default <root> if it has an adapter, else <root>/eval/pack
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import config  # noqa: E402
from core.eval import ab, corpus, runner  # noqa: E402


def _default_corpus(root: Path) -> "Path | None":
    for cand in (root / "harness" / "eval" / "corpus", root / "eval" / "corpus"):
        if cand.is_dir():
            return cand
    return None


def _default_pack(root: Path) -> Path:
    if config.adapter_path(root).is_file():
        return root
    return root / "eval" / "pack"


def _fmt_bucket(name: str, bucket: dict) -> str:
    if not bucket["trials"]:
        return f"{name:<13} (no cases)"
    lo, hi = bucket["ci"]
    return (f"{name:<13} {bucket['passed']}/{bucket['trials']} passed, "
            f"{bucket['pass_rate']:.1%} (CI {lo:.1%}–{hi:.1%})")


def cmd_run(args, root: Path, cfg, cases) -> int:
    rep = runner.run_corpus(root, cfg, cases, seeds=args.seeds)
    n_kind = {}
    for r in rep["cases"].values():
        n_kind[r["kind"]] = n_kind.get(r["kind"], 0) + 1
    kinds = ", ".join(f"{v} {k}" for k, v in sorted(n_kind.items()))
    print(f"eval: {len(rep['cases'])} cases ({kinds}; "
          f"{rep['live_cases_skipped']} live skipped) × {args.seeds} seeds")
    print(_fmt_bucket("overall:", rep["overall"]))
    for s in ("iterate", "heldout"):
        print(_fmt_bucket(f"{s}:", rep["sets"][s]))
    print(_fmt_bucket("adversarial:", rep["adversarial"]))
    icc = rep["icc"]
    # F17: perturbations are verdict-preserving BY DESIGN, so 1.000 here is a
    # determinism check, not agent-level reliability, `ab` prints the live one.
    print(f"ICC(1): {icc:.3f} (determinism check: 1.000 expected; "
          f"live ICC comes from `make eval-ab`)" if icc is not None
          else "ICC(1): n/a (needs ≥2 cases and ≥2 seeds)")
    if rep["histogram"]:
        print("gate-failure histogram (failed trials per gate):")
        for gate, n in rep["histogram"].items():
            print(f"  {gate:<20} {n}")
        failed = sorted(cid for cid, r in rep["cases"].items() if not all(r["passes"]))
        print("failed cases: " + ", ".join(failed))
        print("→ failures appended to harness/state/failures.jsonl")
    else:
        print("gate-failure histogram: empty, no failed trials")
    if args.out:
        Path(args.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"report written: {args.out}")
    return 1 if rep["histogram"] else 0


def cmd_ab(args, root: Path, cfg, cases) -> int:
    # A committed A/B report is EVIDENCE of a pre-registered run (docs/
    # DESIGN.md); a new confirmatory run needs a fresh pre-registration.
    # Refuse the overwrite before any worker spends a cent.
    if args.out and Path(args.out).exists() and not args.force:
        print(f"error: {args.out} already exists. It is committed evidence "
              f"of a pre-registered run. Pre-register the new run in "
              f"docs/DESIGN.md first, then pass --force (or use another --out).",
              file=sys.stderr)
        return 1
    live = [c for c in cases if c["kind"] == "live"
            and (args.set == "all" or c["set"] == args.set)]
    if not live:
        print(f"error: no live cases in set {args.set!r}: the A/B needs kind: live "
              f"cases in the corpus", file=sys.stderr)
        return 1
    print(f"A/B: {len(live)} live cases × {args.seeds} paired trials, "
          f"model={args.model}, max_repairs={args.max_repairs} (live workers, "
          f"this spends real time/quota)")
    invoke = ab.worker_invoke(model=args.model, budget_usd=args.budget,
                              timeout_s=args.timeout)
    # F15: per-trial artifacts (sketches + meta) under a run-stamped dir, and a
    # flushed progress line per case, a 40-minute run must be auditable and
    # distinguishable from a hang while it happens.
    run_dir = (root / "harness" / "state" / "eval"
               / time.strftime("ab-%Y%m%dT%H%M%SZ", time.gmtime()))
    print(f"per-trial artifacts: {run_dir}", flush=True)
    rep = ab.run_ab(root, cfg, live, seeds=args.seeds, invoke=invoke,
                    max_repairs=args.max_repairs, out_dir=run_dir, progress=True)
    print(_fmt_bucket("OFF (bare):", rep["off"]))
    print(_fmt_bucket("ON (harness):", rep["on"]))
    print(f"discordant pairs: b={rep['b']} (off-only pass), c={rep['c']} (on-only pass)")
    print(f"McNemar exact p = {rep['p_value']:.4g}")
    if rep["refused"]:
        print(f"refused (fatal findings survived all repairs: production "
              f"blocks the gate there): {rep['refused']} trial(s)")
    live_icc = rep["live_icc"]
    print(f"live ICC(1) over stochastic trials: "
          + (f"{live_icc:.3f}" if live_icc is not None
             else "n/a (needs ≥2 case-arms and ≥2 seeds)"))
    print(f"cost: ${rep['cost_usd']:.2f}, {rep['tokens']} tokens")
    # τ calibration points: (disagreement, majority-was-wrong) per case×arm.
    points = []
    for pc in rep["per_case"].values():
        for arm in ("off", "on"):
            outcomes = pc[arm]
            erred = int(sum(outcomes) <= len(outcomes) / 2)
            points.append((pc[f"{arm}_disagreement"], erred))
    tau, table = ab.calibrate_tau(points)
    if tau is not None:
        print(f"τ calibration: {len(points)} points → τ = {tau:.3f}")
        for row in table:
            print(f"  τ={row['tau']:.3f} accuracy={row['accuracy']:.2f}")
    if args.out:
        rep["tau"] = tau
        rep["tau_points"] = points
        rep["tau_table"] = table
        Path(args.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"report written: {args.out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "ab"):
        s = sub.add_parser(name)
        s.add_argument("--root", default=".")
        s.add_argument("--corpus", default=None)
        s.add_argument("--pack", default=None)
        s.add_argument("--seeds", type=int, default=5)
        s.add_argument("--out", default=None)
        if name == "ab":
            s.add_argument("--model", default="haiku")
            s.add_argument("--set", default="all", choices=("iterate", "heldout", "all"))
            s.add_argument("--max-repairs", type=int, default=2, dest="max_repairs")
            s.add_argument("--budget", type=float, default=0.25)
            s.add_argument("--timeout", type=int, default=240)
            s.add_argument("--force", action="store_true",
                           help="overwrite an existing --out report "
                                "(requires a fresh pre-registration)")
    args = p.parse_args(argv)
    root = Path(args.root).resolve()
    corpus_dir = Path(args.corpus).resolve() if args.corpus else _default_corpus(root)
    if corpus_dir is None or not corpus_dir.is_dir():
        print("error: no eval corpus found: create cases under "
              "harness/eval/corpus/*.yaml (see docs/EVAL.md in the template)",
              file=sys.stderr)
        return 1
    pack = Path(args.pack).resolve() if args.pack else _default_pack(root)
    try:
        cfg = config.load(pack)
        cases = corpus.load_corpus(corpus_dir)
    except (config.ConfigError, corpus.CorpusError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return (cmd_run if args.cmd == "run" else cmd_ab)(args, root, cfg, cases)


if __name__ == "__main__":
    sys.exit(main())
