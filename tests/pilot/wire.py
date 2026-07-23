#!/usr/bin/env python3
"""wire.py <project-root>, install the consumer-A review layer (adapter-only).

The pilot's whole point (§17 P5): the `reviewed` gate becomes a §11 evidence
wall with ZERO hand-edits to harness/core. Everything lands in the adapter
layer of an already-stamped project:

  1. copy cross_review.py + review_gate.py into harness/adapter/checks/
  2. adapter.yaml: gate_commands.reviewed -> review_gate.py (the producing tool);
     reviewer.cross_model enabled with the cross_review.py cmd (advisory: false);
     a fatal `review-fresh` check (enforces: reviewed-evidence) that re-validates
     review.json on every lint scan once it exists
  3. CLAUDE.md: the <!-- rule:reviewed-evidence --> annotation, so rule-coverage
     (gating) holds in both directions

Idempotent: re-running refreshes the scripts and leaves single entries.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required (pip install pyyaml)") from exc

SCRIPTS = ["cross_review.py", "review_gate.py"]
RULE_ID = "reviewed-evidence"
RULE_TEXT = (
    f"<!-- rule:{RULE_ID} -->\n"
    "- The `reviewed` gate is earned ONLY from a schema-valid, sha256-anchored\n"
    "  `review.json` with verdict `pass`, written by the blinded reviewer\n"
    "  (`cross_review.py`) and recorded by `review_gate.py`, never by manual\n"
    "  approve. Editing the note after review mechanically reopens the gate.\n")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: wire.py <project-root>", file=sys.stderr)
        return 2
    root = Path(argv[1]).resolve()
    adapter_path = root / "harness/adapter/adapter.yaml"
    if not adapter_path.is_file():
        print(f"wire: not a stamped project (missing {adapter_path})",
              file=sys.stderr)
        return 1

    here = Path(__file__).resolve().parent
    checks_dir = root / "harness/adapter/checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        shutil.copy2(here / name, checks_dir / name)

    adapter = yaml.safe_load(adapter_path.read_text(encoding="utf-8"))
    unit_dir = adapter["unit_dir"]
    wf = adapter["workflow"]
    if "reviewed" not in wf["gates"]:
        print("wire: project has no 'reviewed' gate, nothing to wire",
              file=sys.stderr)
        return 1

    wf["gate_commands"]["reviewed"] = (
        "python3 harness/adapter/checks/review_gate.py --unit {slug}")
    adapter.setdefault("reviewer", {})["cross_model"] = {
        "enabled": True,
        "cmd": (f"python3 harness/adapter/checks/cross_review.py"
                f" --artifact {unit_dir}/{{slug}}/note.md"
                f" --rubric harness/adapter/review_rubric.md"
                f" --out {unit_dir}/{{slug}}/review.json"),
        "advisory": False,
    }
    checks = adapter.setdefault("checks", []) or []
    checks = [c for c in checks if c.get("id") != "review-fresh"]
    checks.append({
        "id": "review-fresh",
        "cmd": ("python3 harness/adapter/checks/review_gate.py"
                " --unit {slug} --validate-only"),
        "on": [f"{unit_dir}/*/review.json"],
        "severity": "fatal",
        "enforces": RULE_ID,
    })
    adapter["checks"] = checks
    adapter_path.write_text(yaml.safe_dump(adapter, sort_keys=False,
                                           allow_unicode=True),
                            encoding="utf-8")

    claude_md = root / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8")
    if f"rule:{RULE_ID}" not in text:
        claude_md.write_text(text.rstrip("\n") + "\n\n" + RULE_TEXT,
                             encoding="utf-8")

    # gate_commands changed -> regenerate the stamped fh:gen doc views through
    # the project's OWN core (also schema-validates the adapter.yaml we wrote)
    sys.path.insert(0, str(root / "harness"))
    from core import config, gen
    cfg = config.load(root)
    regenerated = gen.regenerate_project(root, cfg)

    print(f"wire: installed {', '.join(SCRIPTS)} into harness/adapter/checks/")
    print("wire: adapter.yaml: gate_commands.reviewed -> review_gate.py; "
          "reviewer.cross_model enabled; review-fresh check added")
    print(f"wire: CLAUDE.md: <!-- rule:{RULE_ID} --> annotation present")
    for doc in regenerated:
        print(f"wire: regenerated fh:gen blocks in {doc.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
