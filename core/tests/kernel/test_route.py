"""Tests for core/kernel/route.py: the dispatch decision table (plan 1.5, 1.7; bugs B03, B20).

Standard library only. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import unittest
from types import SimpleNamespace

from core.kernel import route
from core.kernel.types import Budget, GateSpec, RepairSpec, Spend
from core.graph import contracts

SCOPED = GateSpec(id="scoped")
COMPOSED = GateSpec(id="composed", checks=("required-sections",))
VERIFIED = GateSpec(id="verified", checks=("policy",), repair=RepairSpec(max_attempts=2))
PACKAGED = GateSpec(id="packaged", needs=("verified",), seals_unit=True)
GATES = [SCOPED, COMPOSED, VERIFIED, PACKAGED]
ALL_IDS = [gate.id for gate in GATES]


def make_state(**overrides):
    state = {
        "project": "example",
        "unit": "u1",
        "actor": "native",
        "config_digest": "digest-a",
        "status": "in_progress",
        "effective": [],
        "stale": [],
        "target_gate": None,
        "surface_digest": None,
        "attempts": {},
        "fail_streak": 0,
        "spend": Spend().to_dict(),
    }
    state.update(overrides)
    return state


def make_cfg(gates=None, **attrs):
    cfg = SimpleNamespace(gates=list(GATES if gates is None else gates),
                          config_digest="digest-a")
    for key, value in attrs.items():
        setattr(cfg, key, value)
    return cfg


class TestStepConstants(unittest.TestCase):
    def test_steps_match_the_graph_contract(self):
        self.assertEqual(route.STEPS, contracts.STEPS)
        self.assertEqual(route.WORK, "work")
        self.assertEqual(route.CHECK, "check")
        self.assertEqual(route.ESCALATE, "escalate")
        self.assertEqual(route.FINALIZE, "finalize")
        self.assertEqual(route.END, "end")


class TestNextStepTable(unittest.TestCase):
    """Each row: a name, state overrides, cfg overrides, and the expected step."""

    CASES = [
        # name, state overrides, cfg overrides, expected
        ("fresh_unit_has_work_to_do", {}, {}, "work"),
        ("explicit_in_progress_nothing_requested", {"status": "in_progress"}, {}, "work"),
        ("attempt_requested_first_gate", {"target_gate": "scoped"}, {}, "check"),
        ("attempt_requested_after_one_gate", {"target_gate": "composed",
                                              "effective": ["scoped"]}, {}, "check"),
        ("attempt_requested_partial_progress", {"target_gate": "verified",
                                                "effective": ["scoped", "composed"]}, {}, "check"),
        ("attempt_within_repair_budget", {"target_gate": "verified",
                                          "attempts": {"verified": 1}}, {}, "check"),
        ("gate_without_repair_spec_is_not_capped", {"target_gate": "scoped",
                                                    "attempts": {"scoped": 99}}, {}, "check"),
        ("complete_status_ends", {"status": "complete"}, {}, "end"),
        ("complete_status_with_target_ends", {"status": "complete",
                                              "target_gate": "scoped"}, {}, "end"),
        ("escalated_status_ends", {"status": "escalated"}, {}, "end"),
        ("escalated_with_target_ends", {"status": "escalated",
                                        "target_gate": "scoped"}, {}, "end"),
        ("escalated_with_all_effective_still_ends", {"status": "escalated",
                                                     "effective": ALL_IDS}, {}, "end"),
        ("abandoned_status_ends", {"status": "abandoned"}, {}, "end"),
        ("paused_status_ends", {"status": "paused"}, {}, "end"),
        ("terminal_beats_changed_config", {"status": "escalated"},
         {"config_digest": "digest-b"}, "end"),
        ("config_digest_changed_escalates", {}, {"config_digest": "digest-b"}, "escalate"),
        ("config_digest_changed_with_attempt", {"target_gate": "scoped"},
         {"config_digest": "digest-b"}, "escalate"),
        ("config_digest_changed_beats_finalize", {"effective": ALL_IDS},
         {"config_digest": "digest-b"}, "escalate"),
        ("config_digest_same_is_work", {"config_digest": "digest-a"}, {}, "work"),
        ("budget_usd_spent_escalates", {"spend": {"usd": 5.0}}, {"budget": Budget(max_usd=5.0)},
         "escalate"),
        ("budget_tokens_spent_escalates", {"spend": {"tokens": 1000}},
         {"budget": Budget(max_usd=9.0, max_tokens=1000)}, "escalate"),
        ("budget_model_calls_spent_escalates", {"spend": {"model_calls": 4}},
         {"budget": Budget(max_usd=9.0, max_model_calls=4)}, "escalate"),
        ("budget_tool_calls_spent_escalates", {"spend": {"tool_calls": 7}},
         {"budget": Budget(max_usd=9.0, max_tool_calls=7)}, "escalate"),
        ("budget_wall_clock_spent_escalates", {"spend": {"elapsed_s": 80.0}},
         {"budget": Budget(max_usd=9.0, max_wall_s=80.0)}, "escalate"),
        ("budget_iterations_spent_escalates", {"spend": {"iterations": 3}},
         {"budget": Budget(max_usd=9.0, max_iterations=3)}, "escalate"),
        ("budget_not_yet_spent_is_work", {"spend": {"usd": 4.99}},
         {"budget": Budget(max_usd=5.0)}, "work"),
        ("budget_spent_but_all_effective_finalizes", {"effective": ALL_IDS,
                                                      "spend": {"usd": 9.0}},
         {"budget": Budget(max_usd=5.0)}, "finalize"),
        ("budget_spent_with_attempt_escalates", {"target_gate": "scoped",
                                                 "spend": {"usd": 9.0}},
         {"budget": Budget(max_usd=5.0)}, "escalate"),
        ("no_budget_on_cfg_never_escalates_on_spend", {"spend": {"usd": 1e6}}, {}, "work"),
        ("fail_streak_at_threshold_escalates", {"fail_streak": 3},
         {"fail_streak_to_escalate": 3}, "escalate"),
        ("fail_streak_below_threshold_is_work", {"fail_streak": 2},
         {"fail_streak_to_escalate": 3}, "work"),
        ("fail_streak_without_threshold_is_work", {"fail_streak": 50}, {}, "work"),
        ("attempts_exhausted_escalates", {"target_gate": "verified",
                                          "attempts": {"verified": 2}}, {}, "escalate"),
        ("attempts_over_limit_escalates", {"target_gate": "verified",
                                           "attempts": {"verified": 5}}, {}, "escalate"),
        ("target_gate_not_in_config_escalates", {"target_gate": "no-such-gate"}, {},
         "escalate"),
        ("every_gate_effective_finalizes", {"effective": ALL_IDS}, {}, "finalize"),
        ("every_gate_effective_with_target_finalizes", {"effective": ALL_IDS,
                                                        "target_gate": "packaged"}, {},
         "finalize"),
        ("one_gate_missing_is_not_finalize", {"effective": ["scoped", "composed", "verified"]},
         {}, "work"),
        ("stale_gate_is_not_effective", {"effective": ["scoped", "composed", "packaged"],
                                         "stale": ["verified"]}, {}, "work"),
        ("no_gates_never_finalizes", {"effective": []}, {"gates": []}, "work"),
    ]

    def test_table_has_at_least_25_states(self):
        self.assertGreaterEqual(len(self.CASES), 25)

    def test_each_case(self):
        for name, state_over, cfg_over, expected in self.CASES:
            with self.subTest(case=name):
                state = make_state(**state_over)
                cfg = make_cfg(**cfg_over)
                self.assertEqual(route.next_step(state, cfg), expected)

    def test_result_is_always_one_of_the_five_steps(self):
        for name, state_over, cfg_over, _ in self.CASES:
            with self.subTest(case=name):
                self.assertIn(route.next_step(make_state(**state_over), make_cfg(**cfg_over)),
                              contracts.STEPS)

    def test_routing_is_pure(self):
        state = make_state(target_gate="scoped")
        cfg = make_cfg()
        before = repr(state)
        route.next_step(state, cfg)
        route.next_step(state, cfg)
        self.assertEqual(repr(state), before)

    def test_B03_terminal_status_routes_to_end(self):
        # Bug B03: approve and set_gate ignored unit status. A terminal status must end the
        # routing before any other rule, so seal (reached only through check) is unreachable.
        for status in ("complete", "escalated", "abandoned", "paused"):
            with self.subTest(status=status):
                state = make_state(status=status, target_gate="scoped")
                self.assertEqual(route.next_step(state, make_cfg()), "end")

    def test_missing_optional_keys_default_safely(self):
        self.assertEqual(route.next_step({}, make_cfg()), "work")
        self.assertEqual(route.next_step({"status": "complete"}, make_cfg()), "end")

    def test_verifier_spread_escalates(self):
        # Plan 1.5 (judge_verdicts row): a vote spread above the threshold escalates instead of
        # repairing. The verifier node records it as a fatal finding with this rule id.
        spread = {"rule": "verifier.spread_escalate", "severity": "fatal", "gate": "verified"}
        state = make_state(target_gate="verified", findings=[spread])
        self.assertEqual(route.SPREAD_ESCALATE_RULE, "verifier.spread_escalate")
        self.assertEqual(route.next_step(state, make_cfg()), "escalate")
        # Also when no attempt is requested and when the repair budget is not used up.
        self.assertEqual(route.next_step(make_state(findings=[spread]), make_cfg()), "escalate")
        # Object-style findings (with a .rule attribute) are tolerated too.
        obj = SimpleNamespace(rule="verifier.spread_escalate", severity="fatal")
        self.assertEqual(route.next_step(make_state(findings=[obj]), make_cfg()), "escalate")

    def test_spread_escalate_follows_terminal_and_digest_rules(self):
        spread = {"rule": "verifier.spread_escalate", "severity": "fatal"}
        # Terminal status still ends first.
        self.assertEqual(route.next_step(make_state(status="complete", findings=[spread]),
                                         make_cfg()), "end")
        # A changed config digest escalates either way; the result is still escalate.
        self.assertEqual(route.next_step(make_state(findings=[spread]),
                                         make_cfg(config_digest="digest-b")), "escalate")
        # Spread escalation comes before finalize: all gates effective still escalates.
        self.assertEqual(route.next_step(make_state(effective=ALL_IDS, findings=[spread]),
                                         make_cfg()), "escalate")

    def test_integrity_finding_escalates(self):
        # Reconcile writes integrity.* findings when the chain is broken or a surface is missing.
        # The unit is not terminal, so the finding must reach escalate, not end.
        for rule in ("integrity.chain_broken", "integrity.surface_missing"):
            with self.subTest(rule=rule):
                finding = {"rule": rule, "severity": "fatal", "path": None, "line": None,
                           "message": "m", "remedy": "r"}
                self.assertEqual(route.INTEGRITY_RULE_PREFIX, "integrity.")
                self.assertEqual(route.next_step(make_state(findings=[finding]), make_cfg()),
                                 "escalate")
                self.assertEqual(route.next_step(make_state(target_gate="verified",
                                                            findings=[finding]), make_cfg()),
                                 "escalate")
                self.assertEqual(route.next_step(make_state(effective=ALL_IDS,
                                                            findings=[finding]), make_cfg()),
                                 "escalate")
                obj = SimpleNamespace(rule=rule, severity="fatal")
                self.assertEqual(route.next_step(make_state(findings=[obj]), make_cfg()),
                                 "escalate")
                # Terminal status still ends first.
                self.assertEqual(route.next_step(make_state(status="escalated",
                                                            findings=[finding]), make_cfg()),
                                 "end")

    def test_ordinary_fatal_finding_still_routes_as_before(self):
        # A fatal finding with another rule does not change routing: work without a target,
        # check with a target that is within its repair budget.
        other = {"rule": "check.policy", "severity": "fatal", "gate": "verified"}
        self.assertEqual(route.next_step(make_state(findings=[other]), make_cfg()), "work")
        self.assertEqual(route.next_step(make_state(target_gate="verified", findings=[other]),
                                         make_cfg()), "check")


if __name__ == "__main__":
    unittest.main()
