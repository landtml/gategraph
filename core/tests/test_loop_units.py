"""Layer 2 unit tests, the pure modules under core/loop/.

Everything here is deterministic and in-process: schemas, the metric-floor
threshold layer, the fixed phase statefile, the append-only registry, the
decision function, backstops (incl. refuse-if-unbounded), dedup degrade,
brief digest, receipts, pidfile, run-manifest, error routing, the stdlib
contract validator, and the self-consistency aggregator.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.loop import backstops, brief, contracts, dedup, decision, errors  # noqa: E402
from core.loop import events, gates, manifest, pidfile, search, smoke  # noqa: E402
from core.loop import statefile, telemetry, usage  # noqa: E402
from core.loop.registry import Registry  # noqa: E402
from core.loop.schemas import GateResult, ProposalCard, Verdict, VerdictCard  # noqa: E402


class TmpBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


def card(cid="c001", cell="axis-a"):
    return ProposalCard(id=cid, hypothesis="h", mechanism="m", cell=cell,
                        kill_criteria="k")


def gr(cid="c001", passes=None, metrics=None, tripwires=None):
    return GateResult(variant_id=cid,
                      passes=passes if passes is not None else {"a": True, "b": True},
                      metrics=metrics or {},
                      tripwires=tripwires or {})


def reject_card(cid, cell_voids=True, why="dead"):
    return VerdictCard(variant_id=cid, soft_passes={"mechanism_lead": False,
                                                      "robustness": False},
                       gaming_clean=True, mechanism_sentence="",
                       verdict=Verdict.REJECT, why=why, voids_cell=cell_voids)


# ── schemas ─────────────────────────────────────────────────────────────────
class TestSchemas(unittest.TestCase):
    def test_gate_result_generic_shape_roundtrip(self):
        g = GateResult(variant_id="c1", passes={"p": True}, metrics={"m": 0.5},
                       tripwires={"leak": False}, notes="n")
        d = g.to_dict()
        self.assertEqual(d["kind"], "gate_result")
        g2 = GateResult.from_dict(d)
        self.assertEqual(g2.passes, {"p": True})
        self.assertEqual(g2.tripwires, {"leak": False})
        self.assertEqual(g2.notes, "n")

    def test_tripwire_ok_semantics(self):
        self.assertTrue(gr(tripwires={}).tripwire_ok)
        self.assertTrue(gr(tripwires={"leak": False}).tripwire_ok)
        self.assertFalse(gr(tripwires={"leak": True, "other": False}).tripwire_ok)

    def test_n_passed(self):
        self.assertEqual(gr(passes={"a": True, "b": False, "c": True}).n_passed, 2)

    def test_from_dict_tolerates_missing_optional_fields(self):
        g = GateResult.from_dict({"variant_id": "c", "passes": {}, "metrics": {}})
        self.assertEqual(g.tripwires, {})
        self.assertEqual(g.notes, "")

    def test_proposal_card_requires_core_fields(self):
        with self.assertRaises(ValueError):
            ProposalCard(id="c", hypothesis="", mechanism="m", cell="x",
                         kill_criteria="k")

    def test_verdict_card_roundtrip_keeps_voids_cell(self):
        v = reject_card("c1", cell_voids=False)
        v2 = VerdictCard.from_dict(v.to_dict())
        self.assertFalse(v2.voids_cell)
        self.assertIs(v2.verdict, Verdict.REJECT)


# ── metric floors (the generic threshold layer) ─────────────────────────────
class TestMetricFloors(unittest.TestCase):
    def test_floor_pass_and_fail(self):
        out = gates.evaluate_metric_floors({"score": 0.9, "cov": 0.5},
                                           {"score": 0.8, "cov": 0.6})
        self.assertEqual(out, {"score": True, "cov": False})

    def test_missing_metric_fails_its_floor(self):
        out = gates.evaluate_metric_floors({}, {"score": 0.8})
        self.assertEqual(out, {"score": False})

    def test_apply_floors_overrides_oracle_pass_for_floored_metric(self):
        g = gr(passes={"score": True, "other": True}, metrics={"score": 0.1})
        g = gates.apply_metric_floors(g, {"score": 0.8})
        self.assertFalse(g.passes["score"])   # floor owns this pass
        self.assertTrue(g.passes["other"])    # oracle-computed pass preserved

    def test_no_floors_is_a_no_op(self):
        g = gr(passes={"a": True})
        self.assertEqual(gates.apply_metric_floors(g, {}).passes, {"a": True})


# ── statefile (kill -9 spine) ───────────────────────────────────────────────
class TestStatefile(TmpBase):
    def test_phase_order_fixed_and_done_terminal(self):
        self.assertEqual(statefile.PHASE_ORDER[0], statefile.Phase.BRIEF)
        self.assertEqual(statefile.next_phase(statefile.Phase.DONE),
                         statefile.Phase.DONE)
        self.assertEqual(statefile.next_phase(statefile.Phase.ORACLE),
                         statefile.Phase.CHECK)

    def test_save_load_roundtrip_and_latest(self):
        store = statefile.StateStore(self.root)
        s = statefile.IterationState(iteration=3, variant_id="c003")
        s.advance(statefile.Phase.CHECK)
        store.save(s)
        store.save(statefile.IterationState(iteration=1))
        latest = store.load_latest()
        self.assertEqual(latest.iteration, 3)
        self.assertEqual(latest.phase_enum, statefile.Phase.CHECK)
        self.assertFalse(list(store.dir.glob("*.tmp")), "atomic tmp must be gone")

    def test_advance_resets_strikes_keeps_attempts(self):
        s = statefile.IterationState(iteration=0, strikes=2, attempts=1)
        s.advance(statefile.Phase.SMOKE)
        self.assertEqual(s.strikes, 0)
        self.assertEqual(s.attempts, 1)


# ── registry ────────────────────────────────────────────────────────────────
class TestRegistry(TmpBase):
    def reg(self):
        return Registry(self.root / "registry.jsonl")

    def test_record_and_reduce(self):
        r = self.reg()
        r.record(card("c1", cell="axis-a"))
        r.record(reject_card("c1"))
        r.record(card("c2", cell="axis-b"))
        r.record(VerdictCard(variant_id="c2", soft_passes={}, gaming_clean=True,
                             mechanism_sentence="m", verdict=Verdict.GRADUATE))
        self.assertEqual(r.graduated_ids(), ["c2"])
        self.assertEqual(r.dead_end_cells(), {"axis-a"})

    def test_tripwire_reject_does_not_burn_cell(self):
        r = self.reg()
        r.record(card("c1", cell="axis-a"))
        r.record(reject_card("c1", cell_voids=False))
        self.assertEqual(r.dead_end_cells(), set())

    def test_torn_final_line_tolerated_interior_raises(self):
        r = self.reg()
        r.record(card("c1"))
        with r.path.open("a", encoding="utf-8") as fh:
            fh.write('{"kind": "torn...')
        self.assertEqual(len(r.events()), 1)  # torn tail dropped
        with r.path.open("w", encoding="utf-8") as fh:
            fh.write('{"bad json\n')
            fh.write(json.dumps(card("c2").to_dict()) + "\n")
        with self.assertRaises(json.JSONDecodeError):
            r.events()

    def test_latest_probation_id(self):
        r = self.reg()
        for cid in ("c1", "c2"):
            r.record(card(cid, cell=f"cell-{cid}"))
            r.record(VerdictCard(variant_id=cid, soft_passes={}, gaming_clean=True,
                                 mechanism_sentence="", verdict=Verdict.PROBATION,
                                 target_pass="a"))
        self.assertEqual(r.latest_probation_id(), "c2")


# ── decision (parameter-free thresholds) ────────────────────────────────────
class TestDecision(unittest.TestCase):
    def kw(self, **over):
        base = dict(soft_passes={"mechanism_lead": True, "robustness": True},
                    gaming_clean=True, mechanism_sentence="clear mechanism",
                    structurally_promising=True, exhausted=False)
        base.update(over)
        return base

    def test_all_pass_with_mechanism_graduates(self):
        out = decision.decide(gr(), **self.kw())
        self.assertIs(out.verdict, Verdict.GRADUATE)

    def test_tripwire_vetoes(self):
        out = decision.decide(gr(tripwires={"leak": True}), **self.kw())
        self.assertIs(out.verdict, Verdict.REJECT)

    def test_gaming_vetoes(self):
        out = decision.decide(gr(), **self.kw(gaming_clean=False))
        self.assertIs(out.verdict, Verdict.REJECT)

    def test_no_mechanism_blocks_graduation(self):
        out = decision.decide(gr(), **self.kw(mechanism_sentence=""))
        self.assertIsNot(out.verdict, Verdict.GRADUATE)

    def test_near_miss_promising_goes_probation_with_target(self):
        g = gr(passes={"a": True, "b": False})
        out = decision.decide(g, **self.kw())
        self.assertIs(out.verdict, Verdict.PROBATION)
        self.assertEqual(out.target_pass, "b")

    def test_exhausted_rejects(self):
        g = gr(passes={"a": True, "b": False})
        out = decision.decide(g, **self.kw(exhausted=True))
        self.assertIs(out.verdict, Verdict.REJECT)
        self.assertIn("exhausted", out.reason)

    def test_below_bar_rejects(self):
        g = gr(passes={"a": False, "b": False})
        out = decision.decide(g, **self.kw(
            soft_passes={"mechanism_lead": False, "robustness": False},
            structurally_promising=False))
        self.assertIs(out.verdict, Verdict.REJECT)


# ── backstops + refuse-if-unbounded ─────────────────────────────────────────
class TestBackstops(unittest.TestCase):
    def test_each_bound_stops(self):
        s = backstops.should_stop({"graduated": 3}, graduate_target=3)
        self.assertTrue(s.stop)
        s = backstops.should_stop({"iterations": 5}, graduate_target=99,
                                  max_iterations=5)
        self.assertTrue(s.stop)
        s = backstops.should_stop({"cost_usd": 10.0}, graduate_target=99,
                                  cost_budget_usd=10.0)
        self.assertTrue(s.stop)
        s = backstops.should_stop({"elapsed_minutes": 200.0}, graduate_target=99,
                                  wall_clock_minutes=180)
        self.assertTrue(s.stop)
        s = backstops.should_stop({}, graduate_target=99)
        self.assertFalse(s.stop)

    def test_is_drifting(self):
        self.assertTrue(backstops.is_drifting({"attempts": 4}, max_attempts=3,
                                              max_minutes=20))
        self.assertFalse(backstops.is_drifting({"attempts": 1, "minutes": 5},
                                               max_attempts=3, max_minutes=20))

    def test_refuses_all_null_backstops(self):
        loop = {"enabled": True, "oracle": "x:Y",
                "backstops": {"max_iterations": None, "cost_budget_usd": None,
                              "wall_clock_minutes": None}}
        problems = backstops.validate_loop_config(loop)
        self.assertTrue(any("backstop" in p for p in problems))

    def test_unbounded_allowed_with_explicit_flag(self):
        loop = {"enabled": True, "oracle": "x:Y", "i_understand_unbounded": True,
                "backstops": {}}
        self.assertEqual(backstops.validate_loop_config(loop), [])

    def test_one_bound_is_enough(self):
        loop = {"enabled": True, "oracle": "x:Y",
                "backstops": {"max_iterations": 5}}
        self.assertEqual(backstops.validate_loop_config(loop), [])

    def test_enabled_requires_oracle_spec(self):
        loop = {"enabled": True, "oracle": "",
                "backstops": {"max_iterations": 5}}
        problems = backstops.validate_loop_config(loop)
        self.assertTrue(any("oracle" in p for p in problems))


# ── dedup (degraded: registry-key + optional judge; no taxonomy) ───────────
class TestDedup(TmpBase):
    def reg_with_dead(self, cell="axis-a"):
        r = Registry(self.root / "r.jsonl")
        r.record(card("c1", cell=cell))
        r.record(reject_card("c1", why="aha: falsified"))
        return r

    def test_exact_dead_end_match_blocks(self):
        r = self.reg_with_dead("Axis-A")
        res = dedup.dedup_check(card("c2", cell="axis-a"), r)
        self.assertFalse(res.distinct)
        self.assertEqual(res.source, "exact")

    def test_no_judge_detariffs_to_registry_key_only(self):
        r = self.reg_with_dead("axis-a")
        res = dedup.dedup_check(card("c2", cell="axis-b"), r)
        self.assertTrue(res.distinct)

    def test_judge_duplicate_high_confidence_blocks(self):
        r = self.reg_with_dead("axis-a")
        res = dedup.dedup_check(
            card("c2", cell="axis-a-renamed"), r,
            judge_fn=lambda c, n: {"decision": "DUPLICATE", "confidence": 0.9,
                                   "reason": "same mechanism"})
        self.assertFalse(res.distinct)
        self.assertEqual(res.source, "judge")

    def test_judge_duplicate_low_confidence_allowed_flagged(self):
        r = self.reg_with_dead("axis-a")
        res = dedup.dedup_check(
            card("c2", cell="axis-a-renamed"), r,
            judge_fn=lambda c, n: {"decision": "DUPLICATE", "confidence": 0.3,
                                   "reason": "maybe"})
        self.assertTrue(res.distinct)
        self.assertTrue(res.flagged)

    def test_nearest_dead_ends_ranks_by_overlap(self):
        r = Registry(self.root / "r.jsonl")
        r.record(ProposalCard(id="c1", hypothesis="momentum decay signal",
                              mechanism="momentum decay in trending regimes",
                              cell="momentum-decay", kill_criteria="k"))
        r.record(reject_card("c1"))
        r.record(ProposalCard(id="c2", hypothesis="unrelated",
                              mechanism="calendar seasonality",
                              cell="calendar", kill_criteria="k"))
        r.record(reject_card("c2"))
        near = dedup.nearest_dead_ends(
            ProposalCard(id="c3", hypothesis="momentum decay revisited",
                         mechanism="decay of momentum", cell="momentum-two",
                         kill_criteria="k"), r, k=1)
        self.assertEqual(near[0]["cell"], "momentum-decay")


# ── brief (deterministic digest) ────────────────────────────────────────────
class TestBrief(TmpBase):
    def test_digest_lists_dead_cells_and_never_raises(self):
        r = Registry(self.root / "r.jsonl")
        self.assertIn("none recorded", brief.dead_ends_digest(r))
        r.record(card("c1", cell="axis-a"))
        r.record(reject_card("c1"))
        self.assertIn("axis-a", brief.dead_ends_digest(r))

    def test_word_cap(self):
        r = Registry(self.root / "r.jsonl")
        for i in range(200):
            r.record(card(f"c{i}", cell=f"cell-{i}-long-name"))
            r.record(reject_card(f"c{i}"))
        text = brief.dead_ends_digest(r, max_words=50)
        self.assertLessEqual(len(text.split()), 51)

    def test_llm_refinement_falls_back_on_failure(self):
        r = Registry(self.root / "r.jsonl")
        fn = brief.make_brief_fn(r, llm_fn=lambda material: (_ for _ in ()).throw(
            RuntimeError("boom")))
        self.assertIn("DEAD-ENDS", fn())


# ── events / receipts ───────────────────────────────────────────────────────
class TestEvents(TmpBase):
    def test_emit_reads_back_with_stamp(self):
        log = events.EventLog(self.root / "events.jsonl", stamp={"v": "1"})
        log.emit(0, "brief", "propose", detail="d", cost_usd=0.5)
        e = log.events()[0]
        self.assertEqual(e["from"], "brief")
        self.assertEqual(e["stamp"], {"v": "1"})
        self.assertEqual(log.total_cost_usd(), 0.5)

    def test_render_status_includes_banner_and_phase(self):
        md = events.render_status({"n_graduated": 1, "target": 3},
                                  {"iteration": 2, "phase": "check"},
                                  [], banner="PAUSED")
        self.assertIn("PAUSED", md)
        self.assertIn("check", md)


# ── pidfile ─────────────────────────────────────────────────────────────────
class TestPidfile(TmpBase):
    def test_acquire_release_and_stale_reclaim(self):
        pf = pidfile.PidFile(self.root / "h.pid")
        pf.path.write_text("999999999")  # dead pid → reclaimable
        pf.acquire()
        self.assertEqual(pf.read_pid(), os.getpid())
        pf.release()
        self.assertFalse(pf.path.exists())

    def test_live_peer_refused(self):
        pf = pidfile.PidFile(self.root / "h.pid")
        pf.path.write_text("1")  # pid 1 is alive and not ours
        with self.assertRaises(pidfile.AlreadyRunning):
            pf.acquire()


# ── run manifest (protected methodology files) ──────────────────────────────
class TestLoopManifest(TmpBase):
    def setUp(self):
        super().setUp()
        (self.root / "harness" / "adapter").mkdir(parents=True)
        (self.root / "harness" / "core" / "loop").mkdir(parents=True)
        (self.root / "harness" / "adapter" / "adapter.yaml").write_text("a: 1\n")
        (self.root / "harness" / "core" / "loop" / "driver.py").write_text("x=1\n")

    def test_stamp_and_diff_detect_mid_run_edit(self):
        m1 = manifest.compute_manifest(self.root)
        self.assertIn("harness_version", m1.stamp())
        (self.root / "harness" / "adapter" / "adapter.yaml").write_text("a: 2\n")
        m2 = manifest.compute_manifest(self.root)
        d = manifest.diff(m1, m2)
        self.assertIn("harness/adapter/adapter.yaml", d["modified"])
        self.assertNotEqual(m1.fingerprint, m2.fingerprint)


# ── error routing + escalation receipts ─────────────────────────────────────
class TestErrors(TmpBase):
    def test_variant_vs_infra_routing(self):
        from core.loop.workers import ContractError, RateLimited
        r = errors.classify(errors.SmokeError("bug", "tb"))
        self.assertEqual(r.route, errors.Route.VARIANT)
        r = errors.classify(ContractError("m", ["e"], 3))
        self.assertEqual(r.route, errors.Route.VARIANT)
        r = errors.classify(RateLimited("slow down"))
        self.assertTrue(r.backoff)
        self.assertFalse(r.escalate)
        r = errors.classify(RuntimeError("oracle blew up"))
        self.assertEqual(r.route, errors.Route.INFRA)
        self.assertTrue(r.escalate)

    def test_escalation_requested_routes_infra_escalate(self):
        r = errors.classify(errors.EscalationRequested("disagreement"))
        self.assertEqual(r.route, errors.Route.INFRA)
        self.assertTrue(r.escalate)

    def test_write_escalation_coalesces_repeats(self):
        exc = RuntimeError("same fault")
        p1 = errors.write_escalation(self.root, iteration=1, phase="check",
                                     exc=exc, variant="c1")
        p2 = errors.write_escalation(self.root, iteration=2, phase="check",
                                     exc=RuntimeError("same fault"), variant="c1")
        self.assertEqual(p1, p2)
        self.assertIn("occurrences: 2", p2.read_text())


# ── smoke (generic, oracle-overridable) ─────────────────────────────────────
class TestSmoke(TmpBase):
    def test_python_variant_import_failure_is_smoke_error(self):
        bad = self.root / "variant.py"
        bad.write_text("def broken(:\n")
        res = smoke.smoke_check(bad)
        self.assertFalse(res.ok)
        self.assertTrue(res.traceback_text)
        with self.assertRaises(errors.SmokeError):
            smoke.smoke_or_raise(bad)

    def test_valid_python_variant_passes(self):
        good = self.root / "variant.py"
        good.write_text("VALUE = 1\n")
        self.assertTrue(smoke.smoke_check(good).ok)

    def test_missing_or_empty_variant_fails(self):
        self.assertFalse(smoke.smoke_check(self.root / "nope.py").ok)
        empty = self.root / "variant.py"
        empty.write_text("")
        self.assertFalse(smoke.smoke_check(empty).ok)

    def test_oracle_custom_smoke_wins(self):
        good = self.root / "variant.py"
        good.write_text("VALUE = 1\n")

        class O:
            def smoke(self, path):
                return smoke.SmokeResult(False, "custom veto")
        self.assertFalse(smoke.smoke_check(good, oracle=O()).ok)


# ── contracts: stdlib validator ─────────────────────────────────────────────
class TestContracts(unittest.TestCase):
    VALID_MAKER = {"id": "c1", "hypothesis": "h", "mechanism": "m", "cell": "x",
                   "kill_criteria": "k", "dead_end_citations": ["brief:none"]}

    def test_valid_maker_payload_passes(self):
        self.assertEqual(contracts.validate(self.VALID_MAKER,
                                            contracts.MAKER_CONTRACT), [])

    def test_missing_required_reported(self):
        bad = dict(self.VALID_MAKER)
        del bad["mechanism"]
        errs = contracts.validate(bad, contracts.MAKER_CONTRACT)
        self.assertTrue(any("mechanism" in e for e in errs))

    def test_additional_properties_rejected(self):
        bad = dict(self.VALID_MAKER, extra_field=1)
        errs = contracts.validate(bad, contracts.MAKER_CONTRACT)
        self.assertTrue(any("extra_field" in e for e in errs))

    def test_empty_string_violates_min_length(self):
        bad = dict(self.VALID_MAKER, hypothesis="")
        self.assertTrue(contracts.validate(bad, contracts.MAKER_CONTRACT))

    def test_empty_citations_violate_min_items(self):
        bad = dict(self.VALID_MAKER, dead_end_citations=[])
        self.assertTrue(contracts.validate(bad, contracts.MAKER_CONTRACT))

    def test_judge_enum_and_number_bounds(self):
        ok = {"decision": "DISTINCT", "confidence": 0.5, "reason": "r"}
        self.assertEqual(contracts.validate(ok, contracts.JUDGE_CONTRACT), [])
        errs = contracts.validate({"decision": "MAYBE", "confidence": 1.5,
                                   "reason": "r"}, contracts.JUDGE_CONTRACT)
        self.assertGreaterEqual(len(errs), 2)

    def test_checker_nested_object_validated(self):
        ok = {"variant_id": "c", "soft_passes": {"mechanism_lead": True,
                                                   "robustness": False},
              "gaming_clean": True, "mechanism_sentence": "",
              "structurally_promising": True, "exhausted": False}
        self.assertEqual(contracts.validate(ok, contracts.CHECKER_CONTRACT), [])
        bad = dict(ok, soft_passes={"mechanism_lead": True})
        self.assertTrue(contracts.validate(bad, contracts.CHECKER_CONTRACT))

    def test_wrong_type_reported(self):
        bad = dict(self.VALID_MAKER, dead_end_citations="not-a-list")
        self.assertTrue(contracts.validate(bad, contracts.MAKER_CONTRACT))


# ── self-consistency aggregation ────────────────────────────────────────────
class TestSearch(unittest.TestCase):
    def j(self, promising=True, mech="m"):
        return {"variant_id": "c1",
                "soft_passes": {"mechanism_lead": promising, "robustness": True},
                "gaming_clean": True, "mechanism_sentence": mech,
                "structurally_promising": promising, "exhausted": False}

    def test_unanimous_has_zero_disagreement(self):
        agg, d = search.aggregate_judgements([self.j(), self.j(), self.j()])
        self.assertEqual(d, 0.0)
        self.assertTrue(agg["structurally_promising"])

    def test_majority_wins_and_disagreement_positive(self):
        agg, d = search.aggregate_judgements(
            [self.j(True), self.j(True), self.j(False)])
        self.assertTrue(agg["structurally_promising"])
        self.assertGreater(d, 0.0)
        self.assertLess(d, 1.0)

    def test_mechanism_sentence_majority(self):
        agg, _ = search.aggregate_judgements(
            [self.j(mech="alpha"), self.j(mech="alpha"), self.j(mech="beta")])
        self.assertEqual(agg["mechanism_sentence"], "alpha")


# ── telemetry + usage ───────────────────────────────────────────────────────
class TestTelemetryUsage(TmpBase):
    def test_telemetry_totals(self):
        t = telemetry.Telemetry(self.root / "t.jsonl")
        t.record(model="haiku", phase="check", cost_usd=0.1)
        t.record(model="opus", phase="check", cost_usd=0.2)
        self.assertAlmostEqual(t.total_cost_usd(), 0.3)
        self.assertAlmostEqual(t.by_model()["opus"], 0.2)

    def test_usage_window_and_threshold(self):
        now = time.time()
        recs = [{"ts": now - 100, "tokens": 50},
                {"ts": now - 6 * 3600, "tokens": 999}]
        self.assertEqual(usage.tokens_in_window(recs, now), 50)
        self.assertTrue(usage.over_threshold(97, 100, 97))
        self.assertFalse(usage.over_threshold(96, 100, 97))
        self.assertFalse(usage.over_threshold(999, None, 97))


if __name__ == "__main__":
    unittest.main(verbosity=2)
