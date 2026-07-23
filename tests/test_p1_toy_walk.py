"""P1 exit test: a freshly stamped toy project walks a full 3-gate chain via
the core CLIs, and the hooks respond correctly to piped JSON events, all
inside the stamped layout (harness/core/...), proving the stamp is not just
structurally correct but operational."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from test_fh_init import run_init

ANSWERS = {
    "project": "toywalk",
    "artifact_noun": "note",
    "unit_dir": "notes",
    "gates": ["composed", "checked", "verified"],
    "hashed_gates": ["verified"],
    "seed_deny": True,
    "loop_enabled": False,
}
ACCT_TOKEN = "010190" + "-1234"  # assembled so this file never stores a literal


class TestToyWalk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "toywalk"
        proc = run_init(cls.root, answers=ANSWERS)
        if proc.returncode != 0:
            raise RuntimeError(f"init failed:\n{proc.stdout}\n{proc.stderr}")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def cli(self, script, *args):
        return subprocess.run(
            [sys.executable, str(self.root / "harness/core" / script), *args,
             "--root", str(self.root)],
            capture_output=True, text=True)

    def hook(self, script, event, *args):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)}
        return subprocess.run(
            [sys.executable, str(self.root / "harness/core/hooks" / script), *args],
            input=json.dumps(event), capture_output=True, text=True, env=env)

    def test_full_lifecycle_with_hooks(self):
        # 1. start a unit: .active + manifest appear under the stamped layout
        r = self.cli("gate.py", "start", "--unit", "n1", "--session-id", "SW")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.root / "notes/.active").exists())

        # 2. stop mid-workflow is blocked, naming the seeded gate command
        r = self.hook("stop_gate.py", {"session_id": "SW", "hook_event_name": "Stop"})
        block = json.loads(r.stdout)
        self.assertEqual(block["decision"], "block")
        self.assertIn("approve composed --unit n1", block["reason"])

        # 3. pretool guard denies the seeded ACCT policy in written content
        ev = {"session_id": "SW", "hook_event_name": "PreToolUse",
              "tool_name": "Write",
              "tool_input": {"file_path": str(self.root / "notes/n1/note.md"),
                             "content": "id " + ACCT_TOKEN + "\n"}}
        out = json.loads(self.hook("pretool_guard.py", ev).stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        # 4. walk the chain: composed -> checked -> verified (tool-set, hashed)
        (self.root / "notes/n1/note.md").write_text("a clean research note\n")
        for gate in ("composed", "checked"):
            r = self.cli("gate.py", "approve", gate, "--unit", "n1")
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("gate.py", "approve", "verified", "--unit", "n1")
        self.assertEqual(r.returncode, 1, "tool-set gate must refuse manual approve")
        r = self.cli("lint.py", "--unit", "n1", "--set-gate", "verified")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        manifest = yaml.safe_load((self.root / "notes/n1/manifest.yaml").read_text())
        self.assertIn("verified", manifest["gates"])
        self.assertIn("note.md", manifest["gates"]["verified"]["sha256"])

        # 5a. workflow is complete, but the session's handoff is stale ->
        #     the stop gate demands consolidation first
        r = self.hook("stop_gate.py", {"session_id": "SW", "hook_event_name": "Stop"})
        block = json.loads(r.stdout)
        self.assertEqual(block["decision"], "block")
        self.assertIn("handoff", block["reason"].lower())

        # 5b. consolidate (update HANDOFF.md after the work) -> stop allowed
        import time
        time.sleep(0.02)  # ensure handoff mtime > marker mtime
        (self.root / "HANDOFF.md").write_text(
            "# HANDOFF\n\n## Now\nn1 complete\n\n## Next\nstart n2\n")
        r = self.hook("stop_gate.py", {"session_id": "SW", "hook_event_name": "Stop"})
        self.assertEqual(r.stdout.strip(), "",
                         "consolidated complete unit must not block stop")

        # 6. session_start reports the completed unit without crashing
        r = self.hook("session_start.py", {"session_id": "SW",
                                           "hook_event_name": "SessionStart"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("n1", r.stdout)

        # 7. staleness: editing the note reopens the hashed gate
        (self.root / "notes/n1/note.md").write_text("edited after verification\n")
        r = self.cli("gate.py", "status")
        self.assertIn("STALE", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
