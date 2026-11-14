#!/usr/bin/env python3
"""wire.py <project-root>, install the consumer-A review layer (adapter-only, schema 2).

The pilot's whole point (§17 P5): the `reviewed` gate becomes an evidence wall with ZERO
hand-edits to harness/core. Everything lands in the adapter layer of an already-stamped project:

  1. copy cross_review.py + review_gate.py into harness/adapter/checks/;
  2. adapter.yaml: a `review` check (review_gate.py, fatal) and the `reviewed` gate's checks
     become [policy, review], so `gate attempt reviewed` earns the gate only when the review
     is a fresh pass;
  3. CLAUDE.md: the <!-- rule:reviewed-evidence --> prose, and the fh:gen blocks regenerated
     through the project's own core (this also validates the adapter it wrote).

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
GATE = "reviewed"
CHECK_ID = "review"
EVIDENCE = "review.json"
RULE_ID = "reviewed-evidence"
RULE_TEXT = (
    f"<!-- rule:{RULE_ID} -->\n"
    "- The `reviewed` gate is earned ONLY from a schema-valid, sha256-anchored\n"
    "  `review.json` with verdict `pass`, written by the blinded reviewer\n"
    "  (`cross_review.py`) and checked by the `review` check (`review_gate.py`)\n"
    "  under `gate attempt reviewed`, never by a direct set. Editing the note after\n"
    "  review makes the review stale, and the check refuses until it is re-run.\n")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: wire.py <project-root>", file=sys.stderr)
        return 2
    root = Path(argv[1]).resolve()
    adapter_path = root / "harness/adapter/adapter.yaml"
    if not adapter_path.is_file():
        print(f"wire: not a stamped project (missing {adapter_path})", file=sys.stderr)
        return 1

    here = Path(__file__).resolve().parent
    checks_dir = root / "harness/adapter/checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        shutil.copy2(here / name, checks_dir / name)

    adapter = yaml.safe_load(adapter_path.read_text(encoding="utf-8"))
    gates = adapter.get("gates") or []
    target = next((g for g in gates if g.get("id") == GATE), None)
    if target is None:
        print(f"wire: project has no '{GATE}' gate, nothing to wire", file=sys.stderr)
        return 1

    checks = [c for c in (adapter.get("checks") or []) if c.get("id") != CHECK_ID]
    checks.append({
        "id": CHECK_ID,
        "command": "python3 harness/adapter/checks/review_gate.py --unit {unit}",
        "severity": "fatal",
    })
    adapter["checks"] = checks
    # the policy scan stays on the gate; the review check joins it
    policy_checks = [c for c in target.get("checks", []) if c != CHECK_ID]
    target["checks"] = policy_checks + [CHECK_ID]
    # review.json is written by the reviewer after the earlier gates passed: the reviewed gate
    # produces it, so earlier gates' surfaces do not change (and reopen) when it appears
    target["produces"] = [EVIDENCE]
    adapter_path.write_text(yaml.safe_dump(adapter, sort_keys=False, allow_unicode=True),
                            encoding="utf-8")

    claude_md = root / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8")
    if f"rule:{RULE_ID}" not in text:
        claude_md.write_text(text.rstrip("\n") + "\n\n" + RULE_TEXT, encoding="utf-8")

    # the adapter changed -> regenerate the stamped fh:gen doc views through the project's own
    # core (also schema-validates the adapter.yaml we wrote)
    sys.path.insert(0, str(root / "harness"))
    from core.kernel import config, gen
    cfg = config.load(root)
    regenerated = gen.regenerate(root, cfg)

    print(f"wire: installed {', '.join(SCRIPTS)} into harness/adapter/checks/")
    print(f"wire: adapter.yaml: check '{CHECK_ID}' added; gate '{GATE}' checks = "
          f"{', '.join(target['checks'])}")
    print(f"wire: CLAUDE.md: <!-- rule:{RULE_ID} --> annotation present")
    for doc in regenerated:
        print(f"wire: regenerated fh:gen blocks in {doc.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
