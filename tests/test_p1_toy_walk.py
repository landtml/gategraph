"""P1 exit test: a freshly stamped toy project walks a full 3-gate chain via the schema-2
gate CLI (`harness/core/cli/gate.py`), and the hooks respond to piped JSON events, all inside
the stamped layout (harness/core/..., harness/core/hosts/shims/...).

Re-expressed for the schema-2 flow (M2 part B). Gates advance only through `gate attempt <gate>`
(check gates) or `gate proceed --decision approve --token=<tok>` (approval gates). A direct set
is refused (bug B01): the legacy entry points refuse a schema-2 project, and no verb takes a
gate name to set.

The stamped gate CLI needs LangGraph, so the project gets a bare venv that points at the
template's locked site-packages (tests/test_stamp_v2.py `seed_venv`). Without the template
`.venv` the class is skipped with the message that says how to build it (`make venv`).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from test_fh_init import run_init
from test_stamp_v2 import require_template_venv, seed_venv

ANSWERS = {
    "project": "toywalk",
    "artifact_noun": "note",
    "unit_dir": "notes",
    "gates": ["composed", "checked", "verified"],
    "seed_deny": True,
}
ACCT_TOKEN = "010190" + "-1234"  # assembled so this file never stores a literal
UNIT = "n1"


class TestToyWalk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_template_venv()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "toywalk"
        proc = run_init(cls.root, answers=ANSWERS)
        if proc.returncode != 0:
            raise RuntimeError(f"init failed:\n{proc.stdout}\n{proc.stderr}")
        cls.py = seed_venv(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ---------------------------------------------------------------- helpers

    def gate(self, *args):
        """The stamped gate CLI, run from the project root with the project's venv."""
        return subprocess.run([str(self.py), "harness/core/cli/gate.py", *args],
                              cwd=self.root, capture_output=True, text=True)

    def legacy(self, script, *args):
        """A legacy top-level entry point that stamped core still carries (refused on schema 2)."""
        return subprocess.run([sys.executable, str(self.root / "harness/core" / script), *args],
                              cwd=self.root, capture_output=True, text=True)

    def hook(self, script, event):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)}
        return subprocess.run(
            [sys.executable, str(self.root / "harness/core/hosts/shims" / script)],
            input=json.dumps(event), capture_output=True, text=True, env=env)

    def manifest_text(self):
        return (self.root / "notes" / UNIT / "manifest.yaml").read_text()

    # ---------------------------------------------------------------- the walk

    def test_full_lifecycle_with_hooks(self):
        # 1. start a unit: .active + manifest appear, and the unit waits at its first request
        r = self.gate("start", "--unit", UNIT, "--session-id", "SW")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.root / "notes/.active").exists())
        pending = self.gate("pending", "--unit", UNIT)
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertIn(f"gate attempt composed --unit {UNIT}", pending.stdout)
        self.assertIn("in_progress", self.gate("status", "--unit", UNIT).stdout)
        # session start recovers the open unit (it names the active unit and its next gate)
        started = self.hook("session_start.py", {"session_id": "SW",
                                                 "hook_event_name": "SessionStart"})
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn(f"active unit: {UNIT}", started.stdout)

        # 2. the stop hook holds this armed session on the stale handoff, and on its unit: a unit
        #    waiting at a work request is in_progress, so the stop names the gate command (the
        #    design ruling on the stop hook; plan 1.16).
        r = self.hook("stop_gate.py", {"session_id": "SW", "hook_event_name": "Stop"})
        block = json.loads(r.stdout)
        self.assertEqual(block["decision"], "block")
        self.assertIn("handoff", block["reason"].lower())
        self.assertIn(f"gate attempt composed --unit {UNIT}", block["reason"],
                      "a unit waiting for work must block on its gate command")

        # 3. pretool guard denies the seeded ACCT policy in written content
        ev = {"session_id": "SW", "hook_event_name": "PreToolUse",
              "tool_name": "Write",
              "tool_input": {"file_path": str(self.root / "notes/n1/note.md"),
                             "content": "id " + ACCT_TOKEN + "\n"}}
        out = json.loads(self.hook("pretool_guard.py", ev).stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        # 4. walk the chain: each check gate is earned by `gate attempt`
        # 4a. a failing attempt exits 1 and prints its findings; the work request is re-issued
        (self.root / "notes/n1/note.md").write_text("id " + ACCT_TOKEN + "\n")
        failed = self.gate("attempt", "composed", "--unit", UNIT)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("finding", failed.stdout)
        self.assertIn("notes/n1/note.md", failed.stdout)
        self.assertIn(f"next: gate attempt composed --unit {UNIT}", failed.stdout)
        (self.root / "notes/n1/note.md").write_text("a clean research note\n")
        for gate in ("composed", "checked", "verified"):
            r = self.gate("attempt", gate, "--unit", UNIT)
            self.assertEqual(r.returncode, 0, gate + ": " + r.stdout + r.stderr)
        status = self.gate("status", "--unit", UNIT)
        self.assertIn("complete", status.stdout)
        self.assertIn("gates passed: composed, checked, verified", status.stdout)

        # 4b. B01: a gate passes only through its checks or an approval. A direct set is refused:
        #     no verb takes a gate name, the legacy scripts refuse a schema-2 project, and the
        #     manifest carries no sha256 map written by a set.
        r = self.legacy("gate.py", "approve", "composed", "--unit", UNIT)
        self.assertNotEqual(r.returncode, 0, "legacy approve must be refused (B01)")
        r = self.legacy("lint.py", "--unit", UNIT, "--set-gate", "verified")
        self.assertNotEqual(r.returncode, 0, "legacy --set-gate must be refused (B01)")
        r = self.gate("approve", "composed", "--unit", UNIT)
        self.assertEqual(r.returncode, 2, "gate CLI has no approve verb (B01)")
        self.assertNotIn("sha256", self.manifest_text(),
                         "no set may write a hash map into manifest.yaml (B01)")
        self.assertIn("complete", self.gate("status", "--unit", UNIT).stdout)

        # 5a. workflow is complete, but the session's handoff is stale ->
        #     the stop gate demands consolidation first
        r = self.hook("stop_gate.py", {"session_id": "SW", "hook_event_name": "Stop"})
        block = json.loads(r.stdout)
        self.assertEqual(block["decision"], "block")
        self.assertIn("handoff", block["reason"].lower())

        # 5b. consolidate (update HANDOFF.md after the work) -> stop allowed
        time.sleep(0.02)  # ensure handoff mtime > marker mtime
        (self.root / "HANDOFF.md").write_text(
            "# HANDOFF\n\n## Now\nn1 complete\n\n## Next\nstart n2\n")
        r = self.hook("stop_gate.py", {"session_id": "SW", "hook_event_name": "Stop"})
        self.assertEqual(r.stdout.strip(), "",
                         "consolidated complete unit must not block stop")

        # 6. session_start after completion runs cleanly and names no active unit: a terminal
        #    unit releases the .active pointer
        r = self.hook("session_start.py", {"session_id": "SW",
                                           "hook_event_name": "SessionStart"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("active unit", r.stdout)
        self.assertFalse((self.root / "notes/.active").exists())

        # 7. staleness: editing the note after the walk makes the evidence chain fail its
        #    surface step, so `gate verify` refuses and names the gates that went stale
        (self.root / "notes/n1/note.md").write_text("edited after verification\n")
        r = self.gate("verify", "--unit", UNIT)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("stale", r.stdout)
        self.assertIn("verified", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
