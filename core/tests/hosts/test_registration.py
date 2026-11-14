"""Hook registration (D5): core/hosts/claude_code.check_registration, the adapter-complete hook
check ported from the legacy core/tests/test_harness_lint.py.

Run alone with: python3 core/tests/hosts/test_registration.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import copy
import json
import tempfile
import unittest
from pathlib import Path

from core.hosts import claude_code
# The stamped settings, as `gategraph init` writes them (the four shims and the Bash relint).
HOOK_SETTINGS = {
    "hooks": {
        "PreToolUse": [{"matcher": "Write|Edit|NotebookEdit", "hooks": [
            {"type": "command",
             "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hosts/shims/pretool_guard.py\""}]}],
        "PostToolUse": [
            {"matcher": "Write|Edit|NotebookEdit", "hooks": [
                {"type": "command",
                 "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hosts/shims/posttool_lint.py\""}]},
            {"matcher": "Bash", "hooks": [
                {"type": "command",
                 "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hosts/shims/posttool_lint.py\" --bash-relint"}]},
        ],
        "Stop": [{"hooks": [
            {"type": "command",
             "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hosts/shims/stop_gate.py\""}]}],
        "SessionStart": [{"hooks": [
            {"type": "command",
             "command": "python3 \"$CLAUDE_PROJECT_DIR/harness/core/hosts/shims/session_start.py\""}]}],
    }
}

SEED_SETTINGS = _bootstrap.REPO / "seed" / "dot_claude" / "settings.json"


class TestRegistration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write_settings(self, data):
        (self.root / ".claude").mkdir(exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data)
        (self.root / ".claude" / "settings.json").write_text(text, encoding="utf-8")

    def test_full_registration_has_no_problems(self):
        self.write_settings(HOOK_SETTINGS)
        self.assertEqual(claude_code.check_registration(self.root), [])

    def test_missing_stop_registration_is_reported(self):
        settings = copy.deepcopy(HOOK_SETTINGS)
        del settings["hooks"]["Stop"]
        self.write_settings(settings)
        problems = claude_code.check_registration(self.root)
        self.assertEqual(problems, ["hook not registered in settings.json: stop_gate.py"])

    def test_missing_bash_relint_is_reported(self):
        text = json.dumps(HOOK_SETTINGS).replace(" --bash-relint", "")
        self.write_settings(text)
        problems = claude_code.check_registration(self.root)
        self.assertTrue(any("--bash-relint" in p for p in problems), problems)

    def test_legacy_hook_path_does_not_count_as_registered(self):
        text = json.dumps(HOOK_SETTINGS).replace("core/hosts/shims/", "core/hooks/")
        self.write_settings(text)
        problems = claude_code.check_registration(self.root)
        self.assertEqual(len([p for p in problems if "not registered" in p]), 4, problems)

    def test_missing_settings_file_is_reported(self):
        problems = claude_code.check_registration(self.root)
        self.assertEqual(problems, [".claude/settings.json missing (hooks unregistered)"])

    def test_unreadable_settings_file_is_reported(self):
        self.write_settings("{not json")
        problems = claude_code.check_registration(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("unreadable", problems[0])

    def test_seed_settings_registers_every_shim(self):
        if not SEED_SETTINGS.is_file():
            self.skipTest("no seed in this tree (stamped project)")
        self.write_settings(SEED_SETTINGS.read_text(encoding="utf-8"))
        self.assertEqual(claude_code.check_registration(self.root), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
