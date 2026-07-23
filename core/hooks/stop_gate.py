#!/usr/bin/env python3
"""stop_gate.py, Stop hook: no abandoning a unit mid-workflow, no ending an
armed session with a stale handoff, without ever blocking unrelated sessions.

Gate logic (only for sessions whose touched-<session_id> marker exists):
  1. Fail-streak escalation: when state/failstreak-<slug> has reached
     escalation.fail_streak_to_escalate, the unit is set to `escalated`
     (terminal, with written evidence) and the stop is ALLOWED, escalate,
     don't loop.
  2. Mid-workflow: active unit in_progress with the final gate unpassed ->
     block, naming the next gate's command (from adapter.yaml gate_commands).
  3. Stale handoff: HANDOFF.md older than this session's work -> block:
     consolidate before stopping (PROTOCOL.md §5).

Deadlock guard: after 3 blocks in one session, handoff.write_fallback_stub()
appends the deterministic auto-handoff section and the stop is allowed with a
loud warning. Fail-open: any error -> exit 0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

BLOCK_CAP = 3


def _count_path(root: Path, sid: str) -> Path:
    return root / "harness" / "state" / f"stopcount-{sid}"


def _read_count(root: Path, sid: str) -> int:
    try:
        return int(_count_path(root, sid).read_text().strip())
    except Exception:
        return 0


def _write_count(root: Path, sid: str, n: int) -> None:
    p = _count_path(root, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(n), encoding="utf-8")


def _fail_streak(root: Path, slug: str) -> int:
    try:
        return int((root / "harness" / "state" / f"failstreak-{slug}")
                   .read_text().strip())
    except Exception:
        return 0


def _escalate(root, cfg, m, slug: str, streak: int) -> None:
    """Terminal escalation with written evidence (plan §12, Layer-1 trigger)."""
    from core import memory, paths
    note = (f"auto-escalated by stop_gate: {streak} consecutive gate failures "
            f"(threshold {cfg.escalation.get('fail_streak_to_escalate')}). "
            f"The harness cannot supply what this unit is missing: "
            f"a human (or different model) should look at it.")
    m.escalate(note)
    m.save(paths.manifest_path(root, cfg, slug))
    eff = m.effective_gates()
    (paths.unit_path(root, cfg, slug) / "escalation.md").write_text(
        f"# Escalation: {slug}\n\n- when: {m.data['escalation']['at']}\n"
        f"- trigger: fail streak {streak}\n"
        f"- gates passed: {', '.join(eff) if eff else '(none)'}\n"
        f"- next unpassed gate: {m.next_gate() or '(none)'}\n\n"
        f"## Why stuck\n\n{note}\n", encoding="utf-8")
    paths.clear_active(root, cfg)
    memory.append_decision(root, "escalation",
                           f"unit {slug} escalated on fail streak {streak}",
                           rationale=note, files=[f"{cfg.unit_dir}/{slug}"])


def main() -> int:
    event = c.read_event()
    root = c.project_root()
    sid = event.get("session_id")

    from core import config, handoff, manifest, paths
    cfg = config.load(root)
    if not paths.marker_exists(root, sid):
        return 0  # this session did no tracked work, never block it

    reasons = []
    active = paths.read_active(root, cfg)
    if active:
        slug = active["slug"]
        try:
            m = manifest.load_for(root, cfg, slug)
        except Exception as exc:
            c.log_exception("stop_gate: manifest load", exc)
            m = None
        if m and m.status == "in_progress" and not m.complete:
            threshold = cfg.escalation.get("fail_streak_to_escalate")
            streak = _fail_streak(root, slug)
            if threshold and streak >= threshold:
                _escalate(root, cfg, m, slug, streak)
                print(f"stop_gate: {cfg.artifact_noun} '{slug}' ESCALATED after "
                      f"{streak} consecutive gate failures, see "
                      f"{cfg.unit_dir}/{slug}/escalation.md. Stop allowed.",
                      file=sys.stderr)
                return 0
            nxt = m.next_gate() or cfg.gates[-1]
            step = cfg.gate_command(nxt, slug) or "run the next workflow step"
            reasons.append(
                f"{cfg.artifact_noun} '{slug}': next gate is '{nxt}': {step}. "
                f"Or stop cleanly: python3 harness/core/gate.py pause --unit {slug}.")

    if handoff.is_stale(root, sid):
        reasons.append(
            "HANDOFF.md is stale: this session did work it does not reflect. "
            "Consolidate before stopping: update Now/Next/Decisions/Gotchas, "
            "promote durable facts (python3 harness/core/memory.py fact --text ...). "
            "See harness/core/PROTOCOL.md §5.")

    if not reasons:
        return 0

    count = _read_count(root, sid)
    if count >= BLOCK_CAP:
        try:
            handoff.write_fallback_stub(root, cfg)
            stubbed = "deterministic auto-handoff stub appended to HANDOFF.md"
        except Exception as exc:
            c.log_exception("stop_gate: fallback stub", exc)
            stubbed = "auto-handoff stub FAILED (see harness/logs/hooks.log)"
        print(f"stop_gate: allowing stop after {count} blocks this session; "
              f"{stubbed}. Still unresolved: {' | '.join(reasons)}",
              file=sys.stderr)
        return 0

    _write_count(root, sid, count + 1)
    print(json.dumps({"decision": "block", "reason": " ALSO: ".join(reasons)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        c.log_exception("stop_gate: unhandled", exc)
        sys.exit(0)
