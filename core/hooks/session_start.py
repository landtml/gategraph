#!/usr/bin/env python3
"""session_start.py, SessionStart hook: context recovery, not re-derivation.

Prints one bounded block: the memory router pointer, working memory (now.md),
consolidated facts, the last 3 decisions, the active unit + next command
(from adapter.yaml gate_commands), and cached doctor warnings. Quiet on a
truly fresh project. Always exits 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

RULE = "─" * 60
MAX_NOW_LINES = 10
MAX_FACTS = 8


def main() -> int:
    root = c.project_root()
    from core import config, doctor, manifest, memory, paths
    cfg = config.load(root)

    active = paths.read_active(root, cfg)
    now = memory.read_now(root).strip()
    facts = memory.read_facts(root)
    decisions = memory.read_decisions(root, last=3)
    if not (active or now or facts or decisions):
        return 0  # truly fresh project; stay quiet

    lines = [f"── {cfg.project}: session context {RULE[:40]}",
             "  router: MEMORY.md (open it first; read at most 2–5 routed files)"]

    if active:
        slug = active["slug"]
        try:
            m = manifest.load_for(root, cfg, slug)
            eff = m.effective_gates()
            lines.append(f"  active {cfg.artifact_noun}: {slug}"
                         + (f": {m.title}" if m.title else "") + f"  [{m.status}]")
            lines.append(f"  gates passed: {', '.join(eff) if eff else '(none yet)'}")
            nxt = m.next_gate()
            lines.append(f"  next: {cfg.gate_command(nxt, slug) or nxt}" if nxt
                         else "  all gates passed: unit complete.")
            stale = m.stale_gates()
            if stale:
                lines.append(f"  STALE: {', '.join(stale)}, re-run the producing tool")
        except manifest.ManifestError:
            lines.append(f"  {slug}: manifest corrupt, run "
                         f"python3 harness/core/gate.py repair --unit {slug}")

    if now:
        lines.append("  working memory (now.md):")
        lines += [f"    {l}" for l in now.splitlines()[:MAX_NOW_LINES]]
    if facts:
        lines.append(f"  facts ({len(facts)}):")
        lines += [f"    - {f.get('text', '')}" for f in facts[-MAX_FACTS:]]
    if decisions:
        lines.append("  recent decisions:")
        lines += [f"    - [{d.get('type', '?')}] {d.get('decision', '')}"
                  for d in decisions]

    cache = doctor.read_cache(root)
    if cache:
        req_fail = [ch["name"] for ch in cache.get("checks", [])
                    if ch.get("required") and not ch.get("ok")]
        if req_fail:
            lines.append(f"  ⚠ toolchain: {', '.join(req_fail)} failing, "
                         f"run python3 harness/core/doctor.py")

    lines.append(RULE)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        c.log_exception("session_start: unhandled", exc)
        sys.exit(0)
