"""Claude Code translation (brief H1): the pre-tool and post-tool decisions the shims make,
including the B05 prevention half, B09, B10 and B31.

Run alone with: python3 core/tests/hosts/test_claude_code.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import json
import os
import tempfile
import time
import unittest

import yaml

from core.hosts import claude_code
from core.kernel import paths

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_bridge import ACCT, make_root  # noqa: E402  (shared fixture builder)


def pre_bash(sid, command):
    return {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}}


def post_bash(sid, command):
    return {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}, "tool_response": {"stdout": "", "stderr": ""}}


def write_tool(sid, path, content, post=False):
    return {"session_id": sid, "hook_event_name": "PostToolUse" if post else "PreToolUse",
            "tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_root(self._tmp.name)
        (self.root / "units" / ".active").write_text("slug: unit-001\n", encoding="utf-8")
        (self.root / "units" / "unit-001").mkdir()

    def pre(self, event):
        return claude_code.run("pre_tool", json.dumps(event), root=self.root)

    def post(self, event):
        return claude_code.run("post_tool", json.dumps(event), root=self.root)

    @staticmethod
    def decision(stdout):
        return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]


class TestPreToolDecisions(Base):
    def test_clean_write_is_silent(self):
        out, err, code = self.pre(write_tool("S1", self.root / "notes/todo.md", "hello\n"))
        self.assertEqual((out, err, code), ("", "", 0))

    def test_protected_write_denied_with_nested_and_flat_keys(self):
        out, _, code = self.pre(write_tool("S1", self.root / "units/unit-001/manifest.yaml", "x\n"))
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(data["permissionDecision"], "deny")
        self.assertIn("protected path", data["permissionDecisionReason"])

    def test_denial_reason_is_guard_denial_message_text(self):
        # The host reason is the kernel's denial text: rule, reason, remedy, policy sentence.
        from core.kernel.guard import POLICY_SENTENCE
        out, _, _ = self.pre(write_tool("S1", self.root / "units/unit-001/manifest.yaml", "x\n"))
        reason = json.loads(out)["permissionDecisionReason"]
        self.assertIn("Rule: protected-path", reason)
        self.assertIn("Reason: ", reason)
        self.assertIn(POLICY_SENTENCE, reason)

    def test_ask_path_returns_ask(self):
        out, _, code = self.pre(write_tool("S1", self.root / "templates/t.md", "x\n"))
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(out), "ask")
        self.assertEqual(json.loads(out)["permissionDecision"], "ask")

    def test_B10_fullwidth_digits_denied_before_write(self):
        fullwidth = "".join(chr(0xFF10 + int(c)) if c.isdigit() else
                            ("－" if c == "-" else c) for c in ACCT)
        out, _, code = self.pre(write_tool("S1", self.root / "notes/id.md", "ID " + fullwidth + "\n"))
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(out), "deny")

    def test_B09_warn_policy_is_advice_not_deny(self):
        deny = [{"id": "advisory", "regex": "ADVISORY-MARKER", "scope": ["any"],
                 "summary": "advisory marker", "severity": "warn", "pretool": True}]
        root = make_root(tempfile.mkdtemp(dir=self._tmp.name), deny=deny)
        event = write_tool("S1", root / "notes/todo.md", "ADVISORY-MARKER here\n")
        out, err, code = claude_code.run("pre_tool", json.dumps(event), root=root)
        self.assertEqual((out, err, code), ("", "", 0))

    def test_B05_shell_naming_a_protected_path_is_denied(self):
        for command in ("sed -i s/a/b/ units/unit-001/manifest.yaml",
                        "echo x > units/.active"):
            out, _, code = self.pre(pre_bash("S1", command))
            self.assertEqual(code, 0, command)
            self.assertEqual(self.decision(out), "deny", command)

    def test_shell_command_naming_no_protected_path_is_silent(self):
        self.assertEqual(self.pre(pre_bash("S1", "ls -la")), ("", "", 0))

    def test_unloadable_config_fails_closed_for_writes(self):
        (self.root / "harness/adapter/adapter.yaml").write_text("{ corrupt: [\n", encoding="utf-8")
        out, _, code = self.pre(write_tool("S1", self.root / "notes/todo.md", "hello\n"))
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(out), "deny")

    def test_unloadable_config_still_allows_repairing_the_adapter(self):
        (self.root / "harness/adapter/adapter.yaml").write_text("{ corrupt: [\n", encoding="utf-8")
        event = write_tool("S1", self.root / "harness/adapter/adapter.yaml", "fh_schema: 1\n")
        out, _, code = self.pre(event)  # the stderr note about the broken config is allowed
        self.assertEqual((out, code), ("", 0))


class TestPostToolLint(Base):
    def test_post_write_fatal_finding_blocks_with_exit_2(self):
        path = self.root / "notes/id.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("ID " + ACCT + "\n", encoding="utf-8")
        out, err, code = self.post(write_tool("S1", path, "", post=True))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("BLOCKED", err)

    def test_post_write_clean_file_is_silent(self):
        path = self.root / "notes/todo.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("hello\n", encoding="utf-8")
        self.assertEqual(self.post(write_tool("S1", path, "", post=True)), ("", "", 0))

    def test_post_write_in_active_unit_arms_the_session(self):
        path = self.root / "units/unit-001/notes.md"
        path.write_text("hello\n", encoding="utf-8")
        self.post(write_tool("S7", path, "", post=True))
        self.assertTrue(paths.marker_path(self.root, "S7").exists())


class TestB31ShellScan(Base):
    def stamp(self, sid):
        out, _, code = self.pre(pre_bash(sid, "python3 build.py"))
        self.assertEqual((out, code), ("", 0))

    def test_B31_post_shell_scans_only_touched_paths(self):
        old = self.root / "units/unit-001/old.md"
        old.write_text("ID " + ACCT + "\n", encoding="utf-8")  # a violation, but not touched
        past = time.time() - 3600
        os.utime(old, (past, past))
        self.stamp("S1")
        (self.root / "units/unit-001/clean.md").write_text("hello\n", encoding="utf-8")
        out, err, code = self.post(post_bash("S1", "python3 build.py"))
        self.assertEqual((code, err), (0, ""), "an untouched file must not be scanned")

        (self.root / "units/unit-001/bad.md").write_text("ID " + ACCT + "\n", encoding="utf-8")
        out, err, code = self.post(post_bash("S1", "python3 build.py"))
        self.assertEqual(code, 2)
        self.assertIn("bad.md", err)
        self.assertNotIn("old.md", err)

    def test_B31_post_shell_arms_the_session(self):
        self.stamp("S1")
        (self.root / "units/unit-001/new.md").write_text("hello\n", encoding="utf-8")
        self.post(post_bash("S1", "python3 build.py"))
        self.assertTrue(paths.marker_path(self.root, "S1").exists())

        self.stamp("S2")
        self.post(post_bash("S2", "python3 build.py"))
        self.assertFalse(paths.marker_path(self.root, "S2").exists())

    def test_B31_no_stamp_means_nothing_is_scanned_or_armed(self):
        (self.root / "units/unit-001/new.md").write_text("ID " + ACCT + "\n", encoding="utf-8")
        out, err, code = self.post(post_bash("S9", "python3 build.py"))
        self.assertEqual((code, err), (0, ""))
        self.assertFalse(paths.marker_path(self.root, "S9").exists())


if __name__ == "__main__":
    unittest.main()
