"""File tools (plan section 1.10, brief A1): confinement, blind reads, touched.

Standard library only. Runs with .venv/bin/python and with the system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import os
import tempfile
import types
import unittest

from core.kernel import guard
from core.kernel.types import Action, ActionKind, Budget, Verdict
from core.tools import fs
from core.tools import spec as S
from core.tools.registry import ToolRefused, Toolbox

PIN_POLICY = {"id": "pin", "regex": r"PIN-\d{4}", "severity": "fatal", "summary": "a pin"}


def fake_cfg(root, deny=()):
    """The policy fields guard.decide reads (same shape as core/tests/kernel/test_guard.py)."""
    return types.SimpleNamespace(root=str(root), deny=list(deny), ask=[],
                                 protected_paths=[], blind=[])


class _Journal:
    def __init__(self):
        self.store = {}

    def seen(self, key):
        return self.store.get(key)

    def record(self, key, result):
        self.store[key] = result


class FsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve()
        self.unit = self.root / "units" / "a"
        self.scratch = self.root / "scratch"
        self.unit.mkdir(parents=True)
        self.scratch.mkdir()
        (self.root / "secret").mkdir()
        (self.root / "secret" / "outside.txt").write_text("keep\n", encoding="utf-8")
        (self.unit / "notes.txt").write_text("alpha beta\n", encoding="utf-8")
        (self.unit / "rubrics").mkdir()
        (self.unit / "rubrics" / "gold.txt").write_text("answer\n", encoding="utf-8")
        self.ctx = S.ToolContext(
            root=self.root, unit_dir=self.unit, scratch_dir=self.scratch,
            writable=("units/a/**", "scratch/**"),
            blind=("units/a/rubrics/**",),
            budget=Budget(), journal=_Journal(),
        )
        self.tb = Toolbox(fs.specs(), self.ctx)

    def call(self, name, args, call_id="c"):
        return self.tb.call(name, args, thread_id="t", tool_call_id=call_id)


class TestWriteConfinement(FsTestCase):
    def test_write_outside_writable_is_refused_before_it_happens(self):
        with self.assertRaises(ToolRefused):
            self.call("write_file", {"path": "secret/new.txt", "content": "x"})
        self.assertFalse((self.root / "secret" / "new.txt").exists())
        with self.assertRaises(ToolRefused):
            self.call("write_file", {"path": "secret/outside.txt", "content": "pwned"})
        self.assertEqual((self.root / "secret" / "outside.txt").read_text(encoding="utf-8"),
                         "keep\n")

    def test_edit_outside_writable_is_refused_before_it_happens(self):
        with self.assertRaises(ToolRefused):
            self.call("edit_file", {"path": "secret/outside.txt",
                                    "old": "keep", "new": "pwned"})
        self.assertEqual((self.root / "secret" / "outside.txt").read_text(encoding="utf-8"),
                         "keep\n")

    def test_absolute_path_outside_root_is_refused(self):
        with tempfile.TemporaryDirectory() as other:
            target = pathlib.Path(other) / "x.txt"
            with self.assertRaises(ToolRefused):
                self.call("write_file", {"path": str(target), "content": "x"})
            self.assertFalse(target.exists())

    def test_parent_escape_is_refused(self):
        with self.assertRaises(ToolRefused):
            self.call("write_file", {"path": "units/a/../../secret/new.txt",
                                     "content": "x"})
        self.assertFalse((self.root / "secret" / "new.txt").exists())


class TestSymlinks(FsTestCase):
    def test_symlink_inside_unit_pointing_outside_is_refused(self):
        os.symlink(self.root / "secret" / "outside.txt", self.unit / "link.txt")
        with self.assertRaises(ToolRefused):
            self.call("write_file", {"path": "units/a/link.txt", "content": "pwned"})
        self.assertEqual((self.root / "secret" / "outside.txt").read_text(encoding="utf-8"),
                         "keep\n")

    def test_symlink_pointing_out_of_the_workspace_is_refused_for_read_and_write(self):
        with tempfile.TemporaryDirectory() as other:
            target = pathlib.Path(other) / "host.txt"
            target.write_text("host secret\n", encoding="utf-8")
            os.symlink(target, self.unit / "host_link.txt")
            with self.assertRaises(ToolRefused):
                self.call("read_file", {"path": "units/a/host_link.txt"})
            with self.assertRaises(ToolRefused):
                self.call("write_file", {"path": "units/a/host_link.txt", "content": "x"})
            self.assertEqual(target.read_text(encoding="utf-8"), "host secret\n")


class TestBlindReads(FsTestCase):
    def test_read_of_blind_path_is_refused(self):
        with self.assertRaises(ToolRefused):
            self.call("read_file", {"path": "units/a/rubrics/gold.txt"})

    def test_blind_path_is_not_listed_by_glob_or_grep(self):
        listing = self.call("glob", {"pattern": "**/*.txt", "path": "units/a"})
        self.assertIn("units/a/notes.txt", listing.content)
        self.assertNotIn("gold", listing.content)
        found = self.call("grep", {"pattern": "answer", "path": "units/a"})
        self.assertNotIn("gold", found.content)
        self.assertNotIn("answer", found.content)

    def test_blind_path_cannot_be_written(self):
        with self.assertRaises(ToolRefused):
            self.call("write_file", {"path": "units/a/rubrics/gold.txt", "content": "x"})
        self.assertEqual((self.unit / "rubrics" / "gold.txt").read_text(encoding="utf-8"),
                         "answer\n")


class TestTouched(FsTestCase):
    def test_write_file_reports_touched(self):
        result = self.call("write_file", {"path": "units/a/out.txt",
                                          "content": "hello world\n"})
        self.assertEqual(result.touched, ("units/a/out.txt",))
        self.assertEqual((self.unit / "out.txt").read_text(encoding="utf-8"),
                         "hello world\n")

    def test_edit_file_reports_touched(self):
        self.call("write_file", {"path": "units/a/out.txt", "content": "hello world\n"})
        result = self.call("edit_file", {"path": "units/a/out.txt",
                                         "old": "world", "new": "there"}, call_id="e1")
        self.assertEqual(result.touched, ("units/a/out.txt",))
        self.assertEqual((self.unit / "out.txt").read_text(encoding="utf-8"),
                         "hello there\n")

    def test_edit_file_with_missing_text_changes_nothing_and_touches_nothing(self):
        result = self.call("edit_file", {"path": "units/a/notes.txt",
                                         "old": "not present", "new": "x"})
        self.assertEqual(result.touched, ())
        self.assertEqual((self.unit / "notes.txt").read_text(encoding="utf-8"),
                         "alpha beta\n")

    def test_scratch_write_is_allowed_and_reported(self):
        result = self.call("write_file", {"path": "scratch/tmp.txt", "content": "s"})
        self.assertEqual(result.touched, ("scratch/tmp.txt",))


class TestActionCarriesContent(FsTestCase):
    """The guard judges an Action, so a write's Action must carry the text it writes."""

    def setUp(self):
        super().setUp()
        self.specs = {s.name: s for s in fs.specs()}

    def test_write_file_action_carries_content(self):
        action = self.specs["write_file"].action_for(
            {"path": "units/a/out.md", "content": "hello PIN-1234"})
        self.assertEqual(action.kind, ActionKind.WRITE)
        self.assertEqual(action.path, "units/a/out.md")
        self.assertEqual(action.content, "hello PIN-1234")

    def test_edit_file_action_carries_new_text(self):
        action = self.specs["edit_file"].action_for(
            {"path": "units/a/notes.txt", "old": "alpha", "new": "PIN-1234"})
        self.assertEqual(action.kind, ActionKind.EDIT)
        self.assertEqual(action.content, "PIN-1234",
                         "the replacement text is what the legacy pre-write hook scans")

    def test_edit_file_action_does_not_carry_the_text_it_removes(self):
        action = self.specs["edit_file"].action_for(
            {"path": "units/a/notes.txt", "old": "PIN-1234", "new": "clean"})
        self.assertEqual(action.content, "clean")

    def test_missing_or_non_string_content_is_refused_as_a_contract_error(self):
        with self.assertRaises(ValueError):
            self.specs["write_file"].action_for({"path": "units/a/out.md"})
        with self.assertRaises(ValueError):
            self.specs["write_file"].action_for({"path": "units/a/out.md", "content": 3})
        with self.assertRaises(ValueError):
            self.specs["edit_file"].action_for({"path": "units/a/notes.txt", "old": "a"})

    def test_guard_denies_write_file_whose_content_matches_a_deny_policy(self):
        cfg = fake_cfg(self.root, deny=[PIN_POLICY])
        args = {"path": "units/a/out.md", "content": "PIN-1234"}
        decision = guard.decide(self.specs["write_file"].action_for(args), cfg)
        self.assertEqual((decision.verdict, decision.rule), (Verdict.DENY, "pin"))
        clean = guard.decide(self.specs["write_file"].action_for(
            {"path": "units/a/out.md", "content": "no pin here"}), cfg)
        self.assertEqual(clean.verdict, Verdict.ALLOW)

    def test_guard_denies_edit_whose_new_text_matches_and_the_write_never_runs(self):
        cfg = fake_cfg(self.root, deny=[PIN_POLICY])
        args = {"path": "units/a/notes.txt", "old": "alpha", "new": "PIN-1234"}
        decision = guard.decide(self.specs["edit_file"].action_for(args), cfg)
        self.assertEqual((decision.verdict, decision.rule), (Verdict.DENY, "pin"))
        # A gate that honours the verdict never calls the tool, so the file is unchanged.
        self.assertEqual((self.unit / "notes.txt").read_text(encoding="utf-8"),
                         "alpha beta\n")


class TestReads(FsTestCase):
    def test_read_file_returns_text(self):
        result = self.call("read_file", {"path": "units/a/notes.txt"})
        self.assertIn("alpha beta", result.content)
        self.assertEqual(result.touched, ())

    def test_list_dir_shows_entries(self):
        result = self.call("list_dir", {"path": "units/a"})
        self.assertIn("notes.txt", result.content)


if __name__ == "__main__":
    unittest.main()
