"""Fail-streak cases ported from the legacy core/tests/test_session_discipline.py (B3).

The legacy stop hook counted failures in harness/state/failstreak-<slug> and escalated the unit
itself. The shims keep no such file. The streak is graph state (``fail_streak``), and the graph
escalates at the threshold (kernel/route.py, rule 7). What the host owns is the reply: an
escalated unit never holds a stop, so a session at the threshold ends instead of looping. This
file pins the threshold decision, and that reply through the stop shim.

Run alone with: python3 core/tests/hosts/test_escalation.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.kernel import paths, route
from core.kernel.types import GateSpec, Spend

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_bridge import make_root  # noqa: E402  (shared fixture builder)
from test_session import write_unit  # noqa: E402  (shared unit builder)
from test_shims import run_shim, stop_event  # noqa: E402  (shared shim driver)


def _state(fail_streak):
    return {"project": "example", "unit": "u1", "actor": "native", "config_digest": "digest-a",
            "status": "in_progress", "effective": [], "stale": [], "target_gate": None,
            "surface_digest": None, "findings": [], "attempts": {},
            "fail_streak": fail_streak, "spend": Spend().to_dict()}


class TestStreakThreshold(unittest.TestCase):
    """The graph decision the legacy hook made by hand: the streak at the threshold escalates."""

    def _cfg(self):
        return SimpleNamespace(gates=[GateSpec(id="composed")], config_digest="digest-a",
                               fail_streak_to_escalate=3)

    def test_streak_at_threshold_escalates(self):
        self.assertEqual(route.next_step(_state(3), self._cfg()), route.ESCALATE)

    def test_streak_below_threshold_keeps_working(self):
        self.assertEqual(route.next_step(_state(2), self._cfg()), route.WORK)


class TestEscalatedUnitStopsCleanly(unittest.TestCase):
    """The host side: an escalated unit lets the stop through, with no block and no reason."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_root(self._tmp.name)
        (self.root / "units" / "u1").mkdir(parents=True, exist_ok=True)
        (self.root / "units" / "u1" / "note.md").write_text("sketch\n", encoding="utf-8")
        marker = paths.marker_path(self.root, "S1")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        # HANDOFF.md written after the marker, so only the unit state is under test
        (self.root / "HANDOFF.md").write_text("# HANDOFF\n\n## Now\ndone\n\n## Next\nnothing\n",
                                              encoding="utf-8")

    def test_escalated_unit_lets_the_stop_through(self):
        write_unit(self.root, "escalated", [], slug="u1")
        r = run_shim("stop_gate.py", stop_event("S1"), self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "", r.stdout + r.stderr)

    def test_in_progress_unit_still_holds_the_stop(self):
        write_unit(self.root, "in_progress", [], slug="u1")
        r = run_shim("stop_gate.py", stop_event("S1"), self.root)
        self.assertEqual(json.loads(r.stdout)["decision"], "block", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
