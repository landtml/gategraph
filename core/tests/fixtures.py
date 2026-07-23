"""Shared fixture helpers for the core test suite.

Every test builds its own throwaway project root in tmp (pure data, the code
under test is the containing core/ package), so the suite is self-contained
and runs identically in the template repo and in any stamped project.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

CORE = Path(__file__).resolve().parents[1]   # the core/ package dir
PKG_PARENT = CORE.parent                     # dir that makes `import core` work

# Assembled at runtime so this file never stores a literal the guard would block.
ACCT_TOKEN = "010190" + "-1234"
ACCT_REGEX = r"\d{6}\s*-?\s*\d{4}"


def default_adapter(**overrides) -> dict:
    cfg = {
        "fh_schema": 1,
        "project": "fixtureproj",
        "artifact_noun": "note",
        "unit_dir": "units",
        "framework_dirs": [".claude", "harness"],
        "instance_token_patterns": [],
        "workflow": {
            "gates": ["composed", "checked", "verified"],
            "hashed_gates": ["verified"],
            "tool_set_gates": ["verified"],
            "gate_commands": {
                "composed": "python3 harness/core/gate.py approve composed --unit {slug}",
                "checked": "python3 harness/core/gate.py approve checked --unit {slug}",
                "verified": "python3 harness/core/lint.py --unit {slug} --set-gate verified",
            },
        },
        "policies": {
            "deny": [
                {"id": "acct", "regex": ACCT_REGEX, "scope": ["any"],
                 "summary": "no account number on disk", "enforces": "no-acct"},
                {"id": "todo-advisory", "regex": "ADVISORY-MARKER", "scope": ["units/**"],
                 "summary": "advisory marker", "severity": "warn", "pretool": False},
            ],
            "ask": [{"paths": ["templates/**"], "reason": "read-only by convention"}],
        },
        "protected_paths": ["units/*/manifest.yaml", "units/.active"],
        "checks": [],
        "reviewer": {"rubric": "harness/adapter/review_rubric.md",
                     "evidence_file": "review.json", "mode": "blinded",
                     "cross_model": {"enabled": False, "cmd": None, "advisory": True}},
        "doctor": {"required": [{"id": "python3", "cmd": "python3 --version"}],
                   "advisory": [{"id": "missing-tool",
                                 "cmd": "definitely-not-a-real-binary-fh --version"}]},
        "state_reap": {"patterns": ["harness/state/touched-*",
                                    "harness/state/stopcount-*"],
                       "max_age_days": 7},
        "escalation": {"confidence_signal": "self_consistency_disagreement",
                       "fail_streak_to_escalate": 3, "error_prob_threshold": None},
        "loop": {"enabled": False, "oracle": "harness.adapter.oracle:Oracle",
                 "gates": {"metric_floors": {}},
                 "self_consistency": {"enabled": False, "n": 8, "aggregate": "majority"},
                 "reflexion": {"enabled": False, "max_iters": 2},
                 "backstops": {"max_iterations": 25, "cost_budget_usd": 10.0,
                               "wall_clock_minutes": 180,
                               "drift": {"max_attempts_per_variant": 3,
                                         "max_minutes_per_variant": 20}}},
    }
    cfg.update(overrides)
    return cfg


HOOK_SETTINGS = {
    "hooks": {
        "PreToolUse": [{"matcher": "Write|Edit|NotebookEdit", "hooks": [
            {"type": "command",
             "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hooks/pretool_guard.py\""}]}],
        "PostToolUse": [
            {"matcher": "Write|Edit|NotebookEdit", "hooks": [
                {"type": "command",
                 "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hooks/posttool_lint.py\""}]},
            {"matcher": "Bash", "hooks": [
                {"type": "command",
                 "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hooks/posttool_lint.py\" --bash-relint"}]},
        ],
        "Stop": [{"hooks": [
            {"type": "command",
             "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hooks/stop_gate.py\""}]}],
        "SessionStart": [{"hooks": [
            {"type": "command",
             "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hooks/session_start.py\""}]}],
    }
}


def mkroot(tmp: Path, adapter: dict | None = None) -> Path:
    """Build a minimal fixture project root (data only) in tmp."""
    (tmp / "CLAUDE.md").write_text(
        "# fixture marker\n\n<!-- rule:no-acct -->\n"
        "- Never write a account number to disk.\n")
    (tmp / "MEMORY.md").write_text("# MEMORY: router\n\n| Task | Read |\n")
    (tmp / "HANDOFF.md").write_text(
        "# HANDOFF\n\n## Now\n\nfresh fixture\n\n## Next\n\nrun make check\n\n"
        "## Decisions this session\n\n(none)\n\n## Gotchas\n\n(none)\n")
    (tmp / "Makefile").write_text(
        "check:\n\tpython3 harness/core/harness_lint.py --tier gating\n")
    (tmp / "harness" / "adapter" / "checks").mkdir(parents=True)
    (tmp / "harness" / "state").mkdir(parents=True)
    (tmp / "harness" / "logs").mkdir(parents=True)
    # placeholder core scripts so doc-cmd-drift path checks resolve (the real
    # code under test lives in the containing core/ checkout, run via run_cli)
    (tmp / "harness" / "core").mkdir(parents=True)
    for stub in ("gate.py", "lint.py", "harness_lint.py"):
        (tmp / "harness" / "core" / stub).write_text("# fixture stub\n")
    (tmp / "units").mkdir()
    write_adapter(tmp, adapter or default_adapter())
    (tmp / ".claude" / "agents").mkdir(parents=True)
    (tmp / ".claude" / "settings.json").write_text(json.dumps(HOOK_SETTINGS, indent=2))
    (tmp / "harness" / "adapter" / "review_rubric.md").write_text("# rubric\n")
    write_workflow(tmp)
    return tmp


def write_workflow(root: Path) -> None:
    """Stamp the canonical workflow doc with a freshly rendered gen block."""
    from core import config, gen
    cfg = config.load(root)
    skills = root / ".claude" / "skills" / cfg.project
    skills.mkdir(parents=True, exist_ok=True)
    text = ("# workflow (canonical)\n\n## Gate chain\n\n"
            "<!-- fh:gen gate-table -->\n(pending)\n<!-- /fh:gen -->\n")
    new_text, _ = gen.regenerate_text(text, cfg)
    (skills / "workflow.md").write_text(new_text)


def age_file(path: Path, seconds: int) -> None:
    """Backdate a file's mtime by `seconds` (deterministic staleness control)."""
    import os
    st = path.stat()
    os.utime(path, (st.st_atime - seconds, st.st_mtime - seconds))


def write_adapter(root: Path, adapter: dict) -> None:
    (root / "harness" / "adapter" / "adapter.yaml").write_text(
        yaml.safe_dump(adapter, sort_keys=False))


def run_cli(script: str, *args, root: Path, stdin: str | None = None):
    """Run a core CLI (gate.py / lint.py / doctor.py) against a fixture root."""
    return subprocess.run(
        [sys.executable, str(CORE / script), *args, "--root", str(root)],
        input=stdin, capture_output=True, text=True)


def run_hook(script: str, event: dict | str, root: Path, *args: str):
    """Drive a hook exactly as Claude Code does: JSON on stdin + env root."""
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    payload = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run([sys.executable, str(CORE / "hooks" / script), *args],
                          input=payload, capture_output=True, text=True, env=env)


def pre_event(root, tool, tool_input, sid="S1"):
    return {"session_id": sid, "cwd": str(root), "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": tool_input}


def post_event(root, tool, tool_input, sid="S1"):
    return {"session_id": sid, "cwd": str(root), "hook_event_name": "PostToolUse",
            "tool_name": tool, "tool_input": tool_input}


def stop_event(root, sid="S1"):
    return {"session_id": sid, "cwd": str(root), "hook_event_name": "Stop"}
