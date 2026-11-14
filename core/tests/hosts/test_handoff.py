"""Handoff and working memory on the host side, ported from the legacy core/tests/test_memory.py
(TestHandoff) and the legacy test_harness_lint handoff checks (X1 B5b).

The legacy core/handoff.py is now core/hosts/session.py: staleness is is_stale, the structure
check is validate_handoff, the automatic stub is write_fallback_stub. Behaviour changed on
purpose: a stub replaces the previous automatic section (B07), so two writes leave one stub.
The decay signal is trailing_stub_count, which counts consecutive automatic sections.

Run alone with: python3 core/tests/hosts/test_handoff.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import os
import time
import unittest

from core.hosts import session
from core.kernel import memory, paths
from test_session import CFG, SessionCase, write_unit


class TestStaleness(SessionCase):
    def test_fresh_when_session_never_touched(self):
        self.assertFalse(session.is_stale(self.root, "S1"))
        self.assertFalse(session.is_stale(self.root, None))

    def test_stale_when_touched_after_handoff_write(self):
        session.arm(self.root, "S1")
        h = self.write_handoff("# HANDOFF\n\n## Now\nold\n\n## Next\nold\n", age_s=60)
        marker = paths.marker_path(self.root, "S1")
        os.utime(marker, (time.time(), time.time()))
        self.assertTrue(session.is_stale(self.root, "S1"), h)

    def test_fresh_again_after_handoff_updated(self):
        session.arm(self.root, "S1")
        marker = paths.marker_path(self.root, "S1")
        past = time.time() - 60
        os.utime(marker, (past, past))
        self.write_handoff("# HANDOFF\n\n## Now\nupdated\n\n## Next\nx\n")
        self.assertFalse(session.is_stale(self.root, "S1"))

    def test_missing_handoff_is_stale_for_armed_session(self):
        session.arm(self.root, "S1")
        (self.root / session.HANDOFF_NAME).unlink(missing_ok=True)
        self.assertTrue(session.is_stale(self.root, "S1"))


class TestHandoffStructure(SessionCase):
    def test_validate_flags_missing_sections(self):
        self.write_handoff("just some text, no structure\n")
        problems = session.validate_handoff(self.root)
        self.assertTrue(any("'## Now'" in p for p in problems), problems)
        self.assertTrue(any("'## Next'" in p for p in problems), problems)

    def test_empty_handoff_is_a_problem(self):
        # legacy adapter-complete check: an empty HANDOFF.md
        self.write_handoff("")
        self.assertEqual(session.validate_handoff(self.root),
                         [f"{session.HANDOFF_NAME} is empty"])

    def test_missing_handoff_is_a_problem(self):
        self.assertEqual(session.validate_handoff(self.root),
                         [f"{session.HANDOFF_NAME} is missing"])


class TestFallbackStub(SessionCase):
    def test_fallback_stub_appends_never_overwrites(self):
        original = "# HANDOFF\n\n## Now\nkept\n\n## Next\nkept\n"
        self.write_handoff(original)
        write_unit(self.root, "in_progress", [], slug="u1")
        memory.write_now(self.root, "was sketching the intro\n")
        memory.append_decision(self.root, "choice", "kept scope small")
        session.write_fallback_stub(self.root, CFG)
        text = (self.root / session.HANDOFF_NAME).read_text(encoding="utf-8")
        self.assertTrue(text.startswith(original), "stub must append, not overwrite")
        self.assertIn(session.AUTO_TAG, text)
        self.assertIn("u1", text)                       # active unit named
        self.assertIn("`build`", text)                  # next gate named
        self.assertIn("was sketching the intro", text)   # now.md compressed in
        self.assertIn("kept scope small", text)         # recent decision

    def test_second_stub_replaces_the_first(self):
        self.write_handoff("## Now\nx\n\n## Next\ny\n")
        session.write_fallback_stub(self.root, CFG)
        session.write_fallback_stub(self.root, CFG)
        text = (self.root / session.HANDOFF_NAME).read_text(encoding="utf-8")
        self.assertEqual(text.count(session.AUTO_TAG), 1)

    def test_consolidation_is_an_episodic_event(self):
        self.write_handoff("## Now\nx\n\n## Next\ny\n")
        session.write_fallback_stub(self.root, CFG)
        kinds = [d["type"] for d in memory.read_decisions(self.root)]
        self.assertIn("auto-handoff", kinds)


class TestHandoffDecay(SessionCase):
    """Legacy handoff-decay: two consecutive automatic sections mean nobody consolidates."""

    def test_one_auto_section_is_not_decay(self):
        self.write_handoff("## Now\nx\n\n## Auto-handoff 2026-01-01T00:00:00\n\nstub\n")
        self.assertEqual(session.trailing_stub_count(self.root), 1)

    def test_two_consecutive_auto_sections_are_decay(self):
        self.write_handoff("## Now\nx\n\n## Auto-handoff 2026-01-01T00:00:00\n\nstub\n"
                           "\n## Auto-handoff 2026-01-02T00:00:00\n\nstub\n")
        self.assertEqual(session.trailing_stub_count(self.root), 2)

    def test_a_real_section_resets_the_run(self):
        self.write_handoff("## Auto-handoff 2026-01-01T00:00:00\n\nstub\n\n## Now\nx\n")
        self.assertEqual(session.trailing_stub_count(self.root), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
