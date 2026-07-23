#!/usr/bin/env python3
"""handoff.py, handoff staleness, structure validation, and the deterministic
fallback stub (plan §9).

A handoff is a consolidation event, not a dump: working memory is compressed,
a decision is appended to the episodic log, durable facts are promoted. The
agent does that itself; when it fails 3 times (stop-gate cap) this module
APPENDS an auto section, timestamp, active unit + gates + next command, git
status, recent decisions, tagged with AUTO_TAG so lint can count decay
(>= 2 consecutive stubs = the handoff discipline is failing).

Staleness model: this session's touched-<sid> marker is newer than HANDOFF.md
=> the session did work the handoff doesn't reflect => stale.

CLI:  handoff.py check --session-id <sid>   exit 0 fresh / 1 stale
      handoff.py stub                        append the fallback stub
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, manifest, memory, paths  # noqa: E402

AUTO_TAG = "<!-- fh:auto-handoff -->"
REQUIRED_SECTIONS = ["## Now", "## Next"]
HANDOFF_NAME = "HANDOFF.md"


def handoff_path(root: Path) -> Path:
    return Path(root) / HANDOFF_NAME


def is_stale(root: Path, session_id: str | None) -> bool:
    """True iff this session touched the unit after HANDOFF.md was written."""
    if not session_id:
        return False
    marker = paths.marker_path(root, session_id)
    if not marker.exists():
        return False  # session did no tracked work, nothing to consolidate
    h = handoff_path(root)
    if not h.exists():
        return True
    return h.stat().st_mtime < marker.stat().st_mtime


def validate(root: Path) -> list[str]:
    """Structural problems with the handoff (empty file, missing sections)."""
    h = handoff_path(root)
    if not h.exists():
        return [f"{HANDOFF_NAME} is missing"]
    text = h.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return [f"{HANDOFF_NAME} is empty"]
    return [f"{HANDOFF_NAME} missing required section {s!r}"
            for s in REQUIRED_SECTIONS if s not in text]


def trailing_stub_count(root: Path) -> int:
    """Consecutive auto-stub sections at the tail of the handoff, the decay
    signal (>= 2 means nobody is consolidating; the lint warns on it).

    Deterministic: walk the `## ` section headings in order; auto sections
    increment the run, any other section resets it. A proper consolidation
    adds (or rewrites to) a real section, resetting the count."""
    h = handoff_path(root)
    if not h.exists():
        return 0
    count = 0
    for line in h.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            count = count + 1 if line.startswith("## Auto-handoff") else 0
    return count


def _git_status(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                             capture_output=True, text=True, timeout=15)
        lines = out.stdout.strip().splitlines()[:15]
        return "\n".join(f"  {l}" for l in lines) if lines else "  (clean)"
    except (OSError, subprocess.SubprocessError):
        return "  (git unavailable)"


def write_fallback_stub(root: Path, cfg) -> None:
    """Append the deterministic auto-handoff section. Never overwrites."""
    ts = datetime.now().replace(microsecond=0).isoformat()
    lines = ["", AUTO_TAG, f"## Auto-handoff {ts}", ""]

    active = paths.read_active(root, cfg)
    if active:
        slug = active["slug"]
        try:
            m = manifest.load_for(root, cfg, slug)
            eff = m.effective_gates()
            nxt = m.next_gate()
            lines.append(f"- active {cfg.artifact_noun}: **{slug}** [{m.status}], "
                         f"gates passed: {', '.join(eff) if eff else '(none)'}")
            if nxt:
                lines.append(f"- next: `{cfg.gate_command(nxt, slug) or nxt}`")
            else:
                lines.append("- all gates passed: unit complete")
        except manifest.ManifestError:
            lines.append(f"- active {cfg.artifact_noun}: {slug} (manifest corrupt, "
                         f"run gate.py repair --unit {slug})")
    else:
        lines.append(f"- no active {cfg.artifact_noun}")

    now = memory.read_now(root).strip()
    if now:
        compressed = "\n".join(now.splitlines()[:15])
        lines += ["", "### Working state (now.md)", "", compressed]

    lines += ["", "### git status", "", _git_status(root)]

    decisions = memory.read_decisions(root, last=3)
    if decisions:
        lines += ["", "### Recent decisions", ""]
        lines += [f"- [{d.get('type', '?')}] {d.get('decision', '')}"
                  for d in decisions]
    lines.append("")

    h = handoff_path(root)
    existing = h.read_text(encoding="utf-8") if h.exists() else ""
    h.write_text(existing + "\n".join(lines), encoding="utf-8")
    memory.append_decision(
        root, "auto-handoff",
        "stop-gate cap reached; deterministic fallback stub appended to HANDOFF.md",
        rationale="agent did not consolidate; stub preserves state for the next session")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["check", "stub"])
    p.add_argument("--session-id", dest="session_id")
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_repo_root()
    if args.cmd == "check":
        problems = validate(root)
        stale = is_stale(root, args.session_id)
        for x in problems:
            print(f"  ✗ {x}")
        if stale:
            print("  ✗ handoff is stale for this session: consolidate before stopping")
        if problems or stale:
            return 1
        print("handoff: fresh and well-formed.")
        return 0
    write_fallback_stub(root, config.load(root))
    print("fallback stub appended to HANDOFF.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
