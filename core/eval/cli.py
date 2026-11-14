#!/usr/bin/env python3
"""cli.py, the stamped `make eval` entry (§13): the deterministic corpus run, `run` only.

  run , deterministic corpus × N seeds; prints pass-rate with a 95% CI,
         ICC(1) across seeds, and the per-gate failure histogram. Exit 1 on
         any case regression (this is the anti-rot net for the policy pack).

There is no live A/B command.

Layout conventions:
  --root    project root (ledger + default corpus/pack location)
  --corpus  case dir; default <root>/harness/eval/corpus, else <root>/eval/corpus
  --pack    policy-pack root holding harness/adapter/adapter.yaml;
            default <root> if it has an adapter, else <root>/eval/pack_v2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.eval import corpus, runner_v2  # noqa: E402
from core.kernel import config as kernel_config, ledger  # noqa: E402

ADAPTER_REL = Path("harness") / "adapter" / "adapter.yaml"


def _default_corpus(root: Path) -> "Path | None":
    for cand in (root / "harness" / "eval" / "corpus", root / "eval" / "corpus"):
        if cand.is_dir():
            return cand
    return None


def _default_pack(root: Path) -> Path:
    if (root / ADAPTER_REL).is_file():
        return root
    return root / "eval" / "pack_v2"


def cmd_run(args, root: Path, pack: Path, corpus_dir: Path) -> int:
    rep = runner_v2.run_corpus(pack, corpus_dir, seeds=args.seeds, root=root)
    failed = bool(rep["histogram"])
    for line in runner_v2.format_report(rep, ledger.failures_path(root) if failed else None):
        print(line)
    if args.out:
        Path(args.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(f"report written: {args.out}")
    return 1 if failed else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run")
    s.add_argument("--root", default=".")
    s.add_argument("--corpus", default=None)
    s.add_argument("--pack", default=None)
    s.add_argument("--seeds", type=int, default=5)
    s.add_argument("--out", default=None)
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
        return cmd_run(args, root, pack, corpus_dir)
    except (kernel_config.ConfigError, corpus.CorpusError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
