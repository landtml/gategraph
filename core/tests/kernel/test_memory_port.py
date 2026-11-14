"""Memory stores, ported from the legacy core/tests/test_memory.py (X1 B5b).

Covered already by core/tests/kernel/test_memory.py (not repeated here): the working note
lifecycle, append-only decisions, corrupt facts, concurrent writers, the root option.
Ported here: malformed episodic lines are skipped, fact dedupe, and the CLI round trip,
which runs the kernel module as the agent runs it (python3 -m core.kernel.memory).

Run alone with: python3 core/tests/kernel/test_memory_port.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from core.kernel import memory
from fixtures_v2 import make_project


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")

    def run_memory(self, *args):
        return subprocess.run([sys.executable, "-m", "core.kernel.memory", "--root",
                               str(self.root), *args], cwd=str(_bootstrap.REPO),
                              capture_output=True, text=True)


class TestEpisodicLog(Base):
    def test_malformed_decision_lines_skipped(self):
        p = self.root / "harness/state/decisions.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"ts":"t","type":"a","decision":"good"}\nnot json\n')
        decisions = memory.read_decisions(self.root)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "good")


class TestSemanticFacts(Base):
    def test_facts_commit_and_dedupe(self):
        self.assertEqual(memory.read_facts(self.root), [])
        self.assertTrue(memory.commit_fact(self.root, "the build needs lualatex >= 1.15"))
        self.assertFalse(memory.commit_fact(self.root, "the build needs lualatex >= 1.15"))
        self.assertTrue(memory.commit_fact(self.root, "unit slugs are kebab-case"))
        facts = memory.read_facts(self.root)
        self.assertEqual(len(facts), 2)
        self.assertTrue(all("at" in f and "text" in f for f in facts))
        raw = yaml.safe_load((self.root / "harness/state/facts.yaml").read_text())
        self.assertIn("facts", raw)


class TestCliRoundTrip(Base):
    def test_cli_roundtrip(self):
        for args in (("decision", "--type", "choice", "--decision", "went with B"),
                     ("fact", "--text", "B is the default now"),
                     ("now", "--set", "mid-task on u1")):
            r = self.run_memory(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_memory("show")
        self.assertEqual(r.returncode, 0, r.stderr)
        for needle in ("went with B", "B is the default now", "mid-task on u1"):
            self.assertIn(needle, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
