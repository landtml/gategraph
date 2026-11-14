"""Tests for core/kernel/memory.py: corrupt facts (B08), concurrent writers (B08),
--root before the subcommand (B29), and files written by the legacy module.

Standard library plus PyYAML. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core.kernel import memory

REPO = _bootstrap.REPO
MODULE_PATH = REPO / "core" / "kernel" / "memory.py"


def make_root(base):
    """A project root: the directory that holds the adapter file."""
    adapter = pathlib.Path(base) / "harness" / "adapter"
    adapter.mkdir(parents=True, exist_ok=True)
    (adapter / "adapter.yaml").write_text("project: example\n", encoding="utf-8")
    return pathlib.Path(base).resolve()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = make_root(pathlib.Path(tmp.name))
        self.state = self.root / "harness" / "state"


class TestCorruptFacts(Base):
    def test_B08_corrupt_facts_not_overwritten(self):
        facts = self.state / "facts.yaml"
        facts.parent.mkdir(parents=True)
        original = "facts:\n  - {at: '2026-01-01T00:00:00', text: 'keep me'\n"
        facts.write_text(original, encoding="utf-8")

        with self.assertRaises(memory.FactsCorrupt) as ctx:
            memory.commit_fact(self.root, "new fact")

        # The unreadable file is moved aside, not rewritten with only the new fact.
        self.assertFalse(facts.exists())
        aside = self.state / "facts.yaml.corrupt"
        self.assertTrue(aside.exists())
        self.assertEqual(aside.read_text(encoding="utf-8"), original)
        self.assertIn("facts.yaml.corrupt", str(ctx.exception))
        self.assertIn("nothing was committed", str(ctx.exception))

    def test_B08_second_corrupt_move_does_not_overwrite_first(self):
        facts = self.state / "facts.yaml"
        facts.parent.mkdir(parents=True)
        (self.state / "facts.yaml.corrupt").write_text("older copy\n", encoding="utf-8")
        facts.write_text("facts: [\n", encoding="utf-8")
        with self.assertRaises(memory.FactsCorrupt):
            memory.commit_fact(self.root, "x")
        self.assertEqual((self.state / "facts.yaml.corrupt").read_text(encoding="utf-8"),
                         "older copy\n")
        self.assertTrue((self.state / "facts.2.corrupt").exists())

    def test_read_facts_raises_on_unparseable_file_and_leaves_it(self):
        facts = self.state / "facts.yaml"
        facts.parent.mkdir(parents=True)
        facts.write_text("facts: [\n", encoding="utf-8")
        with self.assertRaises(memory.FactsCorrupt):
            memory.read_facts(self.root)
        self.assertTrue(facts.exists())

    def test_B08_cli_fact_exits_nonzero_with_guidance(self):
        facts = self.state / "facts.yaml"
        facts.parent.mkdir(parents=True)
        facts.write_text("facts: [\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--root", str(self.root),
             "fact", "--text", "new"],
            capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("facts.yaml.corrupt", proc.stderr)
        self.assertFalse(facts.exists())


WRITER = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from pathlib import Path\n"
    "from core.kernel import memory\n"
    "root, tag = Path(sys.argv[2]), sys.argv[3]\n"
    "for i in range(15):\n"
    "    memory.commit_fact(root, f'{tag} fact {i}')\n"
)


class TestConcurrentWriters(Base):
    def test_B08_concurrent_commits_both_survive(self):
        procs = [subprocess.Popen(
                     [sys.executable, "-c", WRITER, str(REPO), str(self.root), tag],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for tag in ("alpha", "beta")]
        for p in procs:
            _out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
        texts = {f["text"] for f in memory.read_facts(self.root)}
        expected = {f"{tag} fact {i}" for tag in ("alpha", "beta") for i in range(15)}
        self.assertEqual(expected - texts, set())
        self.assertEqual(len(texts), 30)


class TestRootOption(Base):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = memory.main(argv)
        return code, out.getvalue()

    def test_B29_root_before_subcommand_is_honoured(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        other = make_root(pathlib.Path(tmp.name))
        code, _ = self.run_cli(["--root", str(other), "fact", "--text", "before"])
        self.assertEqual(code, 0)
        self.assertEqual([f["text"] for f in memory.read_facts(other)], ["before"])
        self.assertEqual(memory.read_facts(self.root), [])

    def test_B29_root_after_subcommand_still_works(self):
        code, _ = self.run_cli(["fact", "--text", "after", "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual([f["text"] for f in memory.read_facts(self.root)], ["after"])

    def test_B29_root_before_now_subcommand(self):
        code, _ = self.run_cli(["--root", str(self.root), "now", "--set", "working"])
        self.assertEqual(code, 0)
        self.assertEqual(memory.read_now(self.root), "working\n")


class TestLegacyFilesStillLoad(Base):
    def test_files_written_by_the_old_module_load(self):
        self.state.mkdir(parents=True)
        (self.state / "now.md").write_text("working on n1\n", encoding="utf-8")
        (self.state / "decisions.jsonl").write_text(
            '{"ts":"2026-01-02T03:04:05","type":"choice","decision":"use A",'
            '"rationale":"simpler","files":["units/u1/note.md"],"impact":""}\n'
            "not json\n", encoding="utf-8")
        (self.state / "facts.yaml").write_text(
            "facts:\n"
            "- at: '2026-01-01T00:00:00'\n"
            "  text: the build needs lualatex >= 1.15\n"
            "- bare string entry\n", encoding="utf-8")

        self.assertEqual(memory.read_now(self.root), "working on n1\n")
        decisions = memory.read_decisions(self.root)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["files"], ["units/u1/note.md"])
        facts = memory.read_facts(self.root)
        self.assertEqual([f["text"] for f in facts],
                         ["the build needs lualatex >= 1.15", "bare string entry"])

        memory.commit_fact(self.root, "a new fact")
        self.assertEqual(len(memory.read_facts(self.root)), 3)


class TestDecisionsAndNow(Base):
    def test_decisions_append_only(self):
        memory.append_decision(self.root, "choice", "use A", rationale="r",
                               files=["a.md"])
        memory.append_decision(self.root, "fix", "pin regex")
        decisions = memory.read_decisions(self.root)
        self.assertEqual([d["decision"] for d in decisions], ["use A", "pin regex"])
        self.assertEqual(len(memory.read_decisions(self.root, last=1)), 1)

    def test_now_lifecycle(self):
        self.assertEqual(memory.read_now(self.root), "")
        memory.write_now(self.root, "task\n")
        memory.clear_now(self.root)
        self.assertEqual(memory.read_now(self.root), "")


if __name__ == "__main__":
    unittest.main()
