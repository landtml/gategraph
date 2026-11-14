"""G2: the reconcile node (plan sections 1.5, 1.7, 1.8).

Calls the node as a plain function with a state dict. Reconcile re-hashes the
surfaces on disk and never trusts the effective or stale values in state.
Runs on the pinned venv; the node itself imports only core.kernel.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.graph.contracts import WRITES
from core.graph.nodes import reconcile as reconcile_mod
from core.graph.nodes.reconcile import reconcile
from core.kernel.evidence import make_record, verify_chain
from core.kernel.route import next_step
from core.kernel.surface import hash_surface, store_surface
from core.kernel.types import Actor, ActorKind, Finding, GateSpec, RecordKind, Severity

AT = "2026-11-13T10:00:00+00:00"
CONFIG = "c" * 64
GATES = (
    GateSpec(id="composed"),
    GateSpec(id="reviewed", needs=("composed",)),
)
CFG = SimpleNamespace(gates=GATES, config_digest=CONFIG)


def _seal(unit: Path, state_dir: Path, chain: list, gate_id: str) -> list:
    """Append a passing record for ``gate_id`` the way seal does (digest only)."""
    surface = hash_surface(unit, gate_id, GATES)
    digest = store_surface(state_dir, surface)
    rec = make_record(chain, kind=RecordKind.GATE, gate=gate_id, at=AT,
                      surface_digest=digest, producers=(), config_digest=CONFIG,
                      actor=Actor(kind=ActorKind.TOOL, id="test", channel=None))
    return list(chain) + [rec.to_dict()]


class ReconcileTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.unit = root / "unit"
        self.unit.mkdir()
        (self.unit / "a.md").write_text("alpha\n", encoding="utf-8")
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.cfg = SimpleNamespace(gates=GATES, config_digest=CONFIG,
                                   unit_dir=self.unit, state_dir=self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def state(self, **extra) -> dict:
        base = {"project": "p", "unit": "u1", "actor": "native",
                "config_digest": CONFIG, "evidence": [], "status": "in_progress",
                "effective": [], "stale": [], "target_gate": None,
                "surface_digest": None, "attempts": {}, "fail_streak": 0,
                "spend": {}, "decisions": [], "findings": [], "check_results": [],
                "verdicts": []}
        base.update(extra)
        return base


class TestReconcileRecomputes(ReconcileTestBase):
    def test_recomputes_effective_from_disk_and_ignores_state(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        state = self.state(evidence=chain, effective=["reviewed", "bogus"], stale=["composed"])
        update = reconcile(state, self.cfg)
        self.assertEqual(update["effective"], ["composed"])
        self.assertEqual(update["stale"], [])

    def test_empty_chain_ignores_effective_values_in_state(self):
        state = self.state(effective=["composed", "reviewed"])
        update = reconcile(state, self.cfg)
        self.assertEqual(update["effective"], [])
        self.assertEqual(update["stale"], [])

    def test_changed_file_turns_gate_stale(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        chain = _seal(self.unit, self.state_dir, chain, "reviewed")
        state = self.state(evidence=chain)
        self.assertEqual(reconcile(state, self.cfg)["effective"], ["composed", "reviewed"])
        (self.unit / "a.md").write_text("changed\n", encoding="utf-8")
        update = reconcile(state, self.cfg)
        self.assertEqual(update["effective"], [])
        self.assertEqual(update["stale"], ["composed", "reviewed"])

    def test_writes_only_keys_the_contract_allows(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        update = reconcile(self.state(evidence=chain), self.cfg)
        self.assertTrue(set(update) <= WRITES["reconcile"], set(update))


class TestReconcileConfigDigest(ReconcileTestBase):
    def test_config_digest_mismatch_routes_to_escalate(self):
        # The contract has no writable flag key for reconcile, so the mismatch is
        # enforced by route.next_step, which compares the same two digests.
        state = self.state(config_digest="1" * 64)
        update = reconcile(state, self.cfg)
        self.assertEqual(next_step({**state, **update}, self.cfg), "escalate")

    def test_matching_config_digest_does_not_escalate_on_its_own(self):
        state = self.state()
        update = reconcile(state, self.cfg)
        self.assertNotEqual(update.get("status"), "escalated")
        self.assertEqual(next_step({**state, **update}, self.cfg), "work")


class TestReconcileBrokenChain(ReconcileTestBase):
    def test_broken_chain_routes_to_escalate_not_end(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        broken = json.loads(json.dumps(chain))
        broken[0]["at"] = "2030-01-01T00:00:00+00:00"   # digest no longer matches
        with self.assertRaises(Exception):
            verify_chain(broken)
        state = self.state(evidence=broken)
        update = reconcile(state, self.cfg)
        self.assertNotIn("status", update)                 # status is not changed here
        self.assertEqual(update["effective"], [])
        self.assertEqual(update["stale"], [])
        self.assertEqual([f["rule"] for f in update["findings"]], ["integrity.chain_broken"])
        self.assertEqual(update["findings"][0]["severity"], "fatal")
        self.assertIn("gate verify --unit", update["findings"][0]["remedy"])
        self.assertEqual(next_step({**state, **update}, self.cfg), "escalate")

    def test_missing_surface_file_routes_to_escalate_not_stale(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        for path in (self.state_dir / "surfaces").glob("*.json"):
            path.unlink()
        state = self.state(evidence=chain)
        update = reconcile(state, self.cfg)
        self.assertNotIn("status", update)
        self.assertEqual(update["stale"], [])
        self.assertEqual([f["rule"] for f in update["findings"]], ["integrity.surface_missing"])
        self.assertIn("gate verify --unit", update["findings"][0]["remedy"])
        self.assertEqual(next_step({**state, **update}, self.cfg), "escalate")

    def test_integrity_path_writes_only_contract_keys(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        broken = json.loads(json.dumps(chain))
        broken[0]["at"] = "2030-01-01T00:00:00+00:00"
        for evidence in (broken, chain):
            update = reconcile(self.state(evidence=evidence), self.cfg)
            self.assertTrue(set(update) <= WRITES["reconcile"], set(update))
        for path in (self.state_dir / "surfaces").glob("*.json"):
            path.unlink()
        update = reconcile(self.state(evidence=chain), self.cfg)
        self.assertTrue(set(update) <= WRITES["reconcile"], set(update))


def _finding(rule: str, severity: str = "fatal") -> dict:
    """A Finding.to_dict() for a rule, as the other nodes write them."""
    return Finding(rule=rule, severity=Severity(severity), path=None, line=None,
                   message=f"{rule} message", remedy=None).to_dict()


class TestReconcileIntegrityRecovery(ReconcileTestBase):
    def _broken(self, chain: list) -> list:
        broken = json.loads(json.dumps(chain))
        broken[0]["at"] = "2030-01-01T00:00:00+00:00"   # digest no longer matches
        return broken

    def test_integrity_finding_cleared_after_repair(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        stale_finding = _finding("integrity.chain_broken")
        state = self.state(evidence=chain, findings=[stale_finding])
        update = reconcile(state, self.cfg)
        self.assertIn("findings", update)
        self.assertEqual(update["findings"], [])
        self.assertEqual(next_step({**state, **update}, self.cfg), "work")

    def test_integrity_findings_not_duplicated(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        state = self.state(evidence=self._broken(chain))
        first = reconcile(state, self.cfg)
        state = {**state, **first}
        second = reconcile(state, self.cfg)
        findings = second.get("findings", state["findings"])
        rules = [f["rule"] for f in findings if f["rule"].startswith("integrity.")]
        self.assertEqual(rules, ["integrity.chain_broken"])
        self.assertEqual(next_step({**state, **second}, self.cfg), "escalate")

    def test_integrity_finding_replaced_when_surface_breaks_later(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        state = self.state(evidence=chain, findings=[_finding("integrity.chain_broken")])
        for path in (self.state_dir / "surfaces").glob("*.json"):
            path.unlink()
        update = reconcile(state, self.cfg)
        self.assertEqual([f["rule"] for f in update["findings"]], ["integrity.surface_missing"])

    def test_other_findings_untouched(self):
        chain = _seal(self.unit, self.state_dir, [], "composed")
        work_fatal = _finding("work.lint_failed")
        verify_warn = _finding("check.style", severity="warn")
        other = [work_fatal, verify_warn]

        # Chain valid: integrity findings go, the others stay in order.
        state = self.state(evidence=chain,
                           findings=[_finding("integrity.chain_broken"), work_fatal,
                                     verify_warn])
        update = reconcile(state, self.cfg)
        self.assertEqual(update["findings"], other)

        # Chain valid and nothing integrity-related: findings is not written at all.
        state = self.state(evidence=chain, findings=list(other))
        self.assertNotIn("findings", reconcile(state, self.cfg))

        # Chain still broken: the old integrity finding is replaced, others kept in order.
        state = self.state(evidence=self._broken(chain),
                           findings=[work_fatal, _finding("integrity.surface_missing"),
                                     verify_warn])
        update = reconcile(state, self.cfg)
        self.assertEqual([f["rule"] for f in update["findings"]],
                         ["work.lint_failed", "check.style", "integrity.chain_broken"])
        self.assertTrue(set(update) <= WRITES["reconcile"], set(update))


class TestReconcileSource(unittest.TestCase):
    def test_never_calls_interrupt_and_imports_no_langgraph(self):
        source = inspect.getsource(reconcile_mod)
        self.assertNotIn("interrupt", source)
        self.assertNotIn("langgraph", source)


if __name__ == "__main__":
    unittest.main()
