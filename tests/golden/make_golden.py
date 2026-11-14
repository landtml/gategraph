#!/usr/bin/env python3
"""make_golden.py: record the LEGACY engine's behaviour under expected/.

FROZEN at legacy-0.4. This script refuses to run: the legacy engine (core/eval/runner.py,
core/gate.py) is deleted, so it cannot regenerate the snapshots. To regenerate them, check
out tag legacy-0.4 and run this file there. Current checks use verify_golden.py and
FROZEN.sha256.

Runs scenarios.yaml against a temporary stamped project (eval pack adapter)
by invoking core/gate.py and core/lint.py as subprocesses, exactly as an agent
would, and snapshots the observable state after every step. Also snapshots the
policy verdict of every detection case in eval/corpus (5 seeds) and the
runner's machinery verdicts.

  python3 tests/golden/make_golden.py          write expected/*.json

Stdlib + PyYAML only; no engine behaviour is reimplemented.
"""
from __future__ import annotations

import sys as _sys

REFUSAL = ("make_golden.py: snapshots are frozen at tag legacy-0.4; regenerate them by "
           "checking out that tag (the legacy engine is gone from this tree).")

if __name__ == "__main__":
    print(REFUSAL, file=_sys.stderr)
    _sys.exit(2)

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

from core import config, manifest, paths, policy  # noqa: E402
from core.eval import corpus  # noqa: E402
# core/eval/runner.py was deleted at the legacy-0.4 freeze (X1 B5a); the compute_* helpers below
# cannot run. The __main__ refusal above is the only supported entry point.
runner = None

PACK = REPO / "core" / "tests" / "kernel" / "fixtures" / "eval_pack_v1"  # schema-1 copy (D6)
CORPUS = REPO / "eval" / "corpus"
EXPECTED = HERE / "expected"
SEEDS = 5
GATE = [sys.executable, "-B", str(REPO / "core" / "gate.py")]
LINT = [sys.executable, "-B", str(REPO / "core" / "lint.py")]
CHAIN = ["scope_confirmed", "composed", "lint_passed", "pdf_verified", "packaged"]


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_scenarios() -> list:
    data = yaml.safe_load((HERE / "scenarios.yaml").read_text(encoding="utf-8"))
    ids = [s["id"] for s in data]
    assert len(ids) == len(set(ids)), "duplicate scenario id"
    return data


def stamp(root: Path, overrides: dict | None) -> None:
    raw = yaml.safe_load((PACK / "harness" / "adapter" / "adapter.yaml")
                         .read_text(encoding="utf-8"))
    if overrides:
        _deep_merge(raw, overrides)
    adapter = root / "harness" / "adapter" / "adapter.yaml"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    (root / "dossiers").mkdir()


def _run(cmd: list, root: Path) -> tuple:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("CLAUDE_PROJECT_DIR", None)
    p = subprocess.run(cmd + ["--root", str(root)], capture_output=True,
                       text=True, env=env, cwd=str(root))
    return p.returncode, p.stdout, p.stderr


def _expand_steps(steps: list) -> list:
    out = []
    for st in steps:
        if st["op"] != "earn":
            out.append(st)
            continue
        base = {k: v for k, v in st.items() if k not in ("op", "upto")}
        for g in CHAIN[:CHAIN.index(st["upto"]) + 1]:
            if g == "scope_confirmed":
                out.append({"op": "approve", "gate": g, **base})
            elif g == "lint_passed":
                out.append({"op": "lint", "set_gate": g, **base})
            else:
                out.append({"op": "tool_set", "gate": g, **base})
    return out


def _state(root: Path, cfg, unit: str) -> dict:
    s: dict = {}
    active = paths.read_active(root, cfg)
    s["active"] = active["slug"] if active else None
    if not unit or not paths.manifest_path(root, cfg, unit).exists():
        s["manifest"] = "missing"
        return s
    try:
        m = manifest.load_for(root, cfg, unit)
        s["status"] = m.status
        s["effective"] = list(m.effective_gates())
        s["stale"] = m.stale_gates()
        s["next"] = m.next_gate()
    except Exception as exc:  # legacy crashes are part of the snapshot
        s["manifest"] = f"error:{type(exc).__name__}"
        try:
            raw = yaml.safe_load(paths.manifest_path(root, cfg, unit)
                                 .read_text(encoding="utf-8"))
            s["status"] = raw.get("status") if isinstance(raw, dict) else None
        except Exception:
            s["status"] = None
    return s


def _step(op_spec: dict, root: Path, cfg, default_unit: str) -> int:
    op = op_spec["op"]
    unit = op_spec.get("unit", default_unit)
    udir = paths.unit_path(root, cfg, unit) if unit else None
    if op in ("write", "edit", "add"):
        f = udir / op_spec["file"]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(corpus._expand(op_spec["content"]), encoding="utf-8")
        return 0
    if op == "delete":
        (udir / op_spec["file"]).unlink()
        return 0
    if op == "manifest_text":
        paths.manifest_path(root, cfg, unit).write_text(op_spec["text"],
                                                        encoding="utf-8")
        return 0
    if op == "manifest_edit":
        mp = paths.manifest_path(root, cfg, unit)
        data = yaml.safe_load(mp.read_text(encoding="utf-8"))
        data.update(copy.deepcopy(op_spec["set"]))
        mp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return 0
    if op == "tool_set":
        gate = op_spec["gate"]
        try:
            m = manifest.load_for(root, cfg, unit)
            ev = {}
            if gate in cfg.hashed_gates:
                names = sorted(p.relative_to(udir).as_posix()
                               for p in udir.rglob("*")
                               if p.is_file() and p.name != "manifest.yaml")
                names += list(op_spec.get("hash_extra") or [])
                ev["sha256"] = {n: manifest.sha256_file(udir / n) for n in names}
            m.set_gate(gate, **ev)
            m.save(paths.manifest_path(root, cfg, unit))
            return 0
        except Exception:
            return 1
    if op in ("start", "approve", "pause", "proceed", "abandon", "repair",
              "escalate", "status"):
        argv = [op]
        if op == "approve":
            argv.append(op_spec["gate"])
        if unit:
            argv += ["--unit", unit]
        if op == "start" and op_spec.get("title"):
            argv += ["--title", op_spec["title"]]
        if op in ("start", "proceed") and op_spec.get("session"):
            argv += ["--session-id", op_spec["session"]]
        if op == "escalate":
            argv += ["--note", op_spec["note"]]
        rc, out, err = _run(GATE + argv, root)
        _step.last_out = out
        return rc
    if op == "lint":
        argv = []
        if op_spec.get("file"):
            argv += ["--file", str(udir / op_spec["file"])]
        else:
            argv += ["--unit", unit]
        if op_spec.get("set_gate"):
            argv += ["--set-gate", op_spec["set_gate"]]
        rc, out, err = _run(LINT + argv, root)
        _step.last_out = out
        return rc
    raise SystemExit(f"unknown op {op!r}")


_step.last_out = ""


def run_scenario(sc: dict) -> dict:
    default_unit = sc.get("unit", "u1")
    with tempfile.TemporaryDirectory(prefix="golden-") as td:
        root = Path(td).resolve()
        stamp(root, sc.get("adapter"))
        cfg = config.load(root)
        records = []
        for i, st in enumerate(_expand_steps(sc["steps"])):
            _step.last_out = ""
            rc = _step(st, root, cfg, default_unit)
            unit = st.get("unit", default_unit)
            rec = {"i": i, "op": st["op"]}
            if st.get("gate") or st.get("set_gate"):
                rec["gate"] = st.get("gate") or st.get("set_gate")
            if unit != default_unit:
                rec["unit"] = unit
            rec["exit"] = rc
            rec["says_unit_complete"] = "unit complete" in _step.last_out
            rec.update(_state(root, cfg, unit))
            records.append(rec)
    out = {"id": sc["id"], "steps": records}
    if sc.get("legacy_bug"):
        out["legacy_bug"] = sc["legacy_bug"]
    return out


def compute_detection() -> dict:
    cfg = config.load(PACK)
    pols = policy.compile_policies(cfg)
    res = {}
    for case in corpus.load_corpus(CORPUS):
        if case["kind"] != "detection":
            continue
        base = corpus.perturb(case["artifact"], 0)
        findings = [list(f) for f in policy.scan_text(base, pols, case["path"])]
        seeds = []
        for s in range(SEEDS):
            text = corpus.perturb(case["artifact"], s)
            fired = sorted({f[0] for f in policy.scan_text(text, pols, case["path"])})
            seeds.append({"seed": s, "fired": fired,
                          "trial_pass": runner._detection_trial(case, pols, s)})
        res[case["id"]] = {"path": case["path"], "findings": findings,
                           "seeds": seeds}
    return res


def compute_machinery() -> dict:
    cfg = config.load(PACK)
    return {c["id"]: {"scenario": c["scenario"],
                      "pass": runner._machinery_trial(c, cfg)}
            for c in corpus.load_corpus(CORPUS) if c["kind"] == "machinery"}


def compute_all() -> dict:
    """{relative file name: json-able payload} for everything under expected/."""
    out = {}
    for sc in load_scenarios():
        out[f"{sc['id']}.json"] = run_scenario(sc)
    out["_detection.json"] = compute_detection()
    out["_machinery.json"] = compute_machinery()
    return out


def dump(payload) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    EXPECTED.mkdir(exist_ok=True)
    for old in EXPECTED.glob("*.json"):
        old.unlink()
    allout = compute_all()
    for name, payload in allout.items():
        (EXPECTED / name).write_text(dump(payload), encoding="utf-8")
    n = sum(1 for k in allout if not k.startswith("_"))
    print(f"wrote {n} scenario snapshots + detection + machinery to {EXPECTED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
