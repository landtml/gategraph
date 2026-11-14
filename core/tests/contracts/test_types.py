"""Contract tests for core/kernel/types.py and the lazy public surface core/kernel/api.py.

Standard library only. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import dataclasses
import enum
import json
import subprocess
import unittest

from core.kernel import types as T
from core.kernel import api

REPO = _bootstrap.REPO
HEX_A = "a" * 64
HEX_B = "b" * 64

# Every public type of types.py (frozen dataclasses and enums).
TYPE_NAMES = (
    "Action", "ActionKind", "Actor", "ActorKind", "Approval", "Budget",
    "CheckResult", "CheckSpec", "CheckStatus", "Decision", "EvidenceRecord",
    "FaultKind", "FaultRoute", "Finding", "GateSpec", "HumanAnswer", "HumanDecision",
    "HumanKind", "HumanRequest", "ProducerKind", "ProducerRecord", "RecordKind",
    "RepairSpec", "Severity", "Spend", "Surface", "SurfaceSpec", "Verdict",
    "VerifierSpec", "VerifierVerdict",
)

# The module-to-names map of the public API (brief C1, step 3).
API_MAP = {
    "paths": ("find_root", "unit_path", "state_dir", "valid_slug", "atomic_write"),
    "locks": ("file_lock", "LockHeld"),
    "config": ("load", "Config", "ConfigError", "config_digest"),
    "policy": ("compile_policies", "scan_text", "scan_path", "normalize"),
    "globs": ("glob_match",),
    "extract": ("read_artifact",),
    "surface": ("hash_surface", "effective_gates", "stale_gates", "store_surface",
                "load_surface", "SurfaceMissing"),
    "evidence": ("make_record", "verify_chain", "ChainError"),
    "checks": ("run_check", "select_checks"),
    "guard": ("decide", "denial_message"),
    "budget": ("should_stop", "refuse_unbounded"),
    "faults": ("classify", "is_retryable"),
    "route": ("next_step",),
    "memory": ("read_now", "write_now", "append_decision", "read_facts", "commit_fact"),
    "ledger": ("record", "read_ledger"),
    "projection": ("write_projections", "read_manifest"),
    "verify": ("verify_unit", "VerifyReport"),
    "selflint": ("run_lint",),
    "gen": ("regenerate",),
    "doctor": ("run_doctor",),
    "reap": ("reap",),
    "packs": ("load_packs",),
}


def _samples():
    """One realistic instance of every public type. No personal data."""
    finding = T.Finding(rule="no-secrets", severity=T.Severity.FATAL,
                        path="notes/a.md", line=3,
                        message="matched a pattern", remedy="remove the text")
    warn = T.Finding(rule="style", severity=T.Severity.WARN, path=None,
                     line=None, message="long line", remedy=None)
    producer = T.ProducerRecord(id="policy", kind=T.ProducerKind.CHECK,
                                digest=HEX_A, exit=0, verdict=None,
                                findings_digest=None)
    verifier_producer = T.ProducerRecord(id="review", kind=T.ProducerKind.VERIFIER,
                                         digest=HEX_B, exit=None, verdict="pass",
                                         findings_digest=HEX_A)
    actor = T.Actor(kind=T.ActorKind.TOOL, id="write_file", channel=None)
    record = T.EvidenceRecord(
        v=1, seq=0, kind=T.RecordKind.GATE, gate="composed",
        at="2026-11-13T10:00:00+00:00", surface_digest=HEX_A,
        producers=(producer, verifier_producer), config_digest=HEX_B,
        actor=actor, prev="", digest=HEX_A, mac=None)
    return {
        "Action": T.Action(kind=T.ActionKind.WRITE, tool="write_file",
                           path="notes/a.md", content="text", command=None, url=None),
        "ActionKind": T.ActionKind.EXEC,
        "Actor": actor,
        "ActorKind": T.ActorKind.HUMAN,
        "Approval": T.Approval(required=True, channel="tty"),
        "Budget": T.Budget(max_usd=5.0, max_tokens=100000, max_model_calls=50,
                           max_tool_calls=200, max_wall_s=3600.0, max_iterations=12),
        "CheckResult": T.CheckResult(id="policy", exit=1, status=T.CheckStatus.FATAL,
                                     findings=(finding, warn), duration_s=0.25,
                                     digest=HEX_B),
        "CheckSpec": T.CheckSpec(id="policy", command="python3 scripts/policy.py",
                                 severity=T.Severity.FATAL, timeout_s=30.0,
                                 on=("**/*.md",)),
        "CheckStatus": T.CheckStatus.DID_NOT_RUN,
        "Decision": T.Decision(verdict=T.Verdict.ASK, rule="state-dir",
                               reason="names the state directory",
                               remedy="write under the unit directory",
                               findings=(finding,)),
        "EvidenceRecord": record,
        "FaultKind": T.FaultKind.VARIANT,
        "FaultRoute": T.FaultRoute(fault=T.FaultKind.INFRASTRUCTURE, retryable=True, strike=False, backoff_s=2.0, reason="rate limit"),
        "Finding": finding,
        "GateSpec": T.GateSpec(
            id="verified", needs=("composed",), checks=("policy", "style"),
            verifiers=(T.VerifierSpec(id="review", rubric="rubric.md", n=3,
                                      model="reviewer"),),
            approval=T.Approval(required=False, channel="tty"),
            surface=T.SurfaceSpec(include=("**/*.md",), exclude=("build/**",)),
            produces=("build/**",), repair=T.RepairSpec(max_attempts=2),
            seals_unit=False),
        "HumanAnswer": T.HumanAnswer.RETRY,
        "HumanDecision": T.HumanDecision(request_id="r1",
                                         answer=T.HumanAnswer.ALLOW,
                                         by="ops", channel="tty",
                                         note="looks fine",
                                         at="2026-11-13T10:05:00+00:00"),
        "HumanKind": T.HumanKind.ESCALATION,
        "HumanRequest": T.HumanRequest(id="r1", kind=T.HumanKind.APPROVAL,
                                       gate="scoped", channel="token",
                                       summary="approve scope",
                                       at="2026-11-13T10:00:00+00:00",
                                       payload={"gate": "scoped",
                                                "surface_digest": HEX_A,
                                                "checks": [{"id": "policy",
                                                            "status": "pass"}]},
                                       token_digest="0" * 64),
        "ProducerKind": T.ProducerKind.VERIFIER,
        "ProducerRecord": producer,
        "RecordKind": T.RecordKind.WAIVER,
        "RepairSpec": T.RepairSpec(max_attempts=2),
        "Severity": T.Severity.WARN,
        "Spend": T.Spend(usd=0.42, tokens=1200, model_calls=3, tool_calls=7,
                         first_started_at="2026-11-13T10:00:00+00:00",
                         iterations=4, elapsed_s=12.5),
        "Surface": T.Surface(files={"notes/a.md": HEX_A, "sources/b.md": HEX_B}),
        "SurfaceSpec": T.SurfaceSpec(include=("**/*.md",), exclude=()),
        "Verdict": T.Verdict.DENY,
        "VerifierSpec": T.VerifierSpec(id="review", rubric="rubric.md", n=3,
                                       model="reviewer"),
        "VerifierVerdict": T.VerifierVerdict(id="review", run=0, verdict="fail",
                                             findings=(finding,), digest=HEX_A,
                                             artifact_sha256=HEX_B),
    }


def _assert_plain(tc, value, where="root"):
    """to_dict() must return only JSON-plain values."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            tc.assertIsInstance(key, str, where)
            _assert_plain(tc, item, f"{where}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_plain(tc, item, f"{where}[{index}]")
        return
    tc.fail(f"non-plain value {value!r} ({type(value).__name__}) at {where}")


class TypesContract(unittest.TestCase):

    def setUp(self):
        self.samples = _samples()

    def test_every_public_type_has_a_sample(self):
        self.assertEqual(set(self.samples), set(TYPE_NAMES))

    def test_types_module_exports_exactly_the_listed_types(self):
        defined = set()
        for name, obj in vars(T).items():
            if name.startswith("_"):
                continue
            if getattr(obj, "__module__", None) != T.__name__:
                continue
            if isinstance(obj, type) and (issubclass(obj, enum.Enum)
                                          or dataclasses.is_dataclass(obj)):
                defined.add(name)
        self.assertEqual(defined, set(TYPE_NAMES))

    def test_every_type_is_frozen_dataclass_or_enum(self):
        for name in TYPE_NAMES:
            cls = getattr(T, name)
            if issubclass(cls, enum.Enum):
                continue
            with self.subTest(type=name):
                self.assertTrue(dataclasses.is_dataclass(cls))
                self.assertTrue(cls.__dataclass_params__.frozen)

    def test_round_trip_through_plain_dict_is_lossless(self):
        for name, obj in self.samples.items():
            if isinstance(obj, enum.Enum):
                continue
            with self.subTest(type=name):
                plain = obj.to_dict()
                _assert_plain(self, plain)
                self.assertEqual(type(obj).from_dict(plain), obj)
                via_json = json.loads(json.dumps(plain))
                self.assertEqual(type(obj).from_dict(via_json), obj)

    def test_enums_serialize_as_their_string_value(self):
        finding = self.samples["Finding"]
        self.assertEqual(finding.to_dict()["severity"], "fatal")
        result = self.samples["CheckResult"]
        self.assertEqual(result.to_dict()["status"], "fatal")
        self.assertEqual(self.samples["CheckStatus"].value, "did_not_run")
        self.assertEqual(T.CheckStatus("did_not_run"), T.CheckStatus.DID_NOT_RUN)
        self.assertEqual({s.value for s in T.CheckStatus},
                         {"pass", "fatal", "warn", "did_not_run", "skipped"})
        self.assertEqual({s.value for s in T.Verdict}, {"allow", "deny", "ask"})
        self.assertEqual({s.value for s in T.Severity}, {"fatal", "warn"})
        self.assertEqual({s.value for s in T.HumanKind},
                         {"work", "approval", "ask", "escalation"})
        self.assertEqual({s.value for s in T.HumanAnswer},
                         {"attempt", "approve", "reject", "allow", "refuse",
                          "retry", "abandon", "waive"})

    def test_decision_is_a_verdict_with_reasons_and_findings(self):
        decision = self.samples["Decision"]
        plain = decision.to_dict()
        self.assertEqual(plain["verdict"], "ask")
        self.assertEqual(set(plain), {"verdict", "rule", "reason", "remedy", "findings"})
        rebuilt = T.Decision.from_dict(plain)
        self.assertIsInstance(rebuilt.verdict, T.Verdict)
        self.assertIsInstance(rebuilt.findings[0], T.Finding)
        self.assertEqual(rebuilt, decision)

    def test_human_answers_follow_the_section_1_12_table(self):
        table = {kind: {answer.value for answer in answers}
                 for kind, answers in T.HUMAN_ANSWERS.items()}
        self.assertEqual(table, {
            "work": {"attempt"},
            "approval": {"approve", "reject"},
            "ask": {"allow", "refuse"},
            "escalation": {"retry", "abandon", "waive"},
        })
        covered = {a for answers in T.HUMAN_ANSWERS.values() for a in answers}
        self.assertEqual(covered, set(T.HumanAnswer))

    def test_human_request_kind_and_channel_round_trip(self):
        request = self.samples["HumanRequest"]
        plain = request.to_dict()
        self.assertEqual(plain["kind"], "approval")
        self.assertEqual(plain["channel"], "token")
        self.assertEqual(plain["token_digest"], "0" * 64)
        self.assertNotIn("token", plain)
        self.assertIsInstance(T.HumanRequest.from_dict(plain).kind, T.HumanKind)

    def test_human_request_token_is_optional(self):
        plain = self.samples["HumanRequest"].to_dict()
        del plain["token_digest"]
        request = T.HumanRequest.from_dict(plain)
        self.assertIsNone(request.token_digest)
        self.assertEqual(request.to_dict()["token_digest"], None)

    def test_human_decision_answer_is_a_human_answer(self):
        decision = T.HumanDecision.from_dict(self.samples["HumanDecision"].to_dict())
        self.assertIsInstance(decision.answer, T.HumanAnswer)
        self.assertIsNone(T.HumanDecision.from_dict(
            dict(self.samples["HumanDecision"].to_dict(), note=None)).note)

    def test_unknown_field_is_rejected(self):
        for name, obj in self.samples.items():
            if isinstance(obj, enum.Enum) or name == "Surface":
                continue  # Surface's dict keys are paths, not fields.
            with self.subTest(type=name):
                plain = obj.to_dict()
                plain["unexpected_field"] = 1
                with self.assertRaises(ValueError):
                    type(obj).from_dict(plain)

    def test_unknown_field_inside_a_nested_type_is_rejected(self):
        plain = self.samples["CheckResult"].to_dict()
        plain["findings"][0]["surprise"] = "x"
        with self.assertRaises(ValueError):
            T.CheckResult.from_dict(plain)

    def test_unknown_enum_value_is_rejected(self):
        plain = self.samples["CheckResult"].to_dict()
        plain["status"] = "bogus"
        with self.assertRaises(ValueError):
            T.CheckResult.from_dict(plain)

    def test_unknown_human_answer_is_rejected(self):
        plain = self.samples["HumanDecision"].to_dict()
        plain["answer"] = "approved"
        with self.assertRaises(ValueError):
            T.HumanDecision.from_dict(plain)

    def test_types_are_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.samples["Finding"].message = "changed"

    def test_nested_types_are_rebuilt_as_their_classes(self):
        record = T.EvidenceRecord.from_dict(self.samples["EvidenceRecord"].to_dict())
        self.assertIsInstance(record.actor, T.Actor)
        self.assertIsInstance(record.producers[0], T.ProducerRecord)
        self.assertIsInstance(record.kind, T.RecordKind)
        result = T.CheckResult.from_dict(self.samples["CheckResult"].to_dict())
        self.assertIsInstance(result.findings[0], T.Finding)
        self.assertIsInstance(result.findings[0].severity, T.Severity)
        gate = T.GateSpec.from_dict(self.samples["GateSpec"].to_dict())
        self.assertIsInstance(gate.repair, T.RepairSpec)
        self.assertIsInstance(gate.verifiers[0], T.VerifierSpec)

    def test_surface_is_its_path_to_digest_map(self):
        surface = self.samples["Surface"]
        self.assertEqual(surface.to_dict(), {"notes/a.md": HEX_A, "sources/b.md": HEX_B})
        self.assertEqual(T.Surface.from_dict(surface.to_dict()), surface)
        other = T.Surface(files={"notes/a.md": HEX_B})
        self.assertNotEqual(surface.digest(), other.digest())
        self.assertRegex(surface.digest(), r"^[0-9a-f]{64}$")


class YamlShapedInputs(unittest.TestCase):
    """Shapes the section 1.7 YAML and the section 1.9 check output really use."""

    def test_minimal_gate_from_yaml_gets_defaults(self):
        gate = T.GateSpec.from_dict({"id": "scoped",
                                     "approval": {"required": True, "channel": "tty"}})
        self.assertEqual(gate.checks, ())
        self.assertEqual(gate.verifiers, ())
        self.assertIsNone(gate.surface)
        self.assertIsNone(gate.repair)
        self.assertFalse(gate.seals_unit)

    def test_verified_gate_from_yaml_has_nested_types(self):
        gate = T.GateSpec.from_dict({
            "id": "verified",
            "checks": ["policy", "claims-have-sources", "style"],
            "verifiers": [{"id": "review", "rubric": "harness/adapter/review_rubric.md",
                           "n": 3, "model": "reviewer"}],
            "surface": {"include": ["**/*.md", "sources/**"], "exclude": ["build/**"]},
            "repair": {"max_attempts": 2},
        })
        self.assertEqual(gate.repair, T.RepairSpec(max_attempts=2))
        self.assertEqual(gate.surface.include, ("**/*.md", "sources/**"))

    def test_check_output_finding_without_remedy_is_valid(self):
        finding = T.Finding.from_dict({"rule": "style", "severity": "warn",
                                       "path": "a.md", "line": 2,
                                       "message": "long line"})
        self.assertIsNone(finding.remedy)

    def test_check_spec_without_triggers_is_gate_only(self):
        spec = T.CheckSpec.from_dict({"id": "style", "command": "python3 s.py",
                                      "severity": "warn"})
        self.assertEqual(spec.on, ())
        self.assertIsInstance(spec.severity, T.Severity)

    def test_budget_and_spend_defaults_are_unbounded_and_zero(self):
        budget = T.Budget()
        plain = budget.to_dict()
        self.assertIs(plain.pop("allow_unbounded"), False)
        self.assertTrue(all(value is None for value in plain.values()))
        spend = T.Spend()
        self.assertEqual(spend.to_dict(), {"usd": 0.0, "tokens": 0, "model_calls": 0,
                                           "tool_calls": 0, "first_started_at": None,
                                           "iterations": 0, "elapsed_s": 0.0})


class CanonicalJson(unittest.TestCase):

    def test_key_order_does_not_change_the_bytes(self):
        one = {"b": 1, "a": {"d": 2, "c": [3, {"y": 1, "x": 2}]}}
        two = {"a": {"c": [3, {"x": 2, "y": 1}], "d": 2}, "b": 1}
        self.assertEqual(T.canonical_json(one), T.canonical_json(two))
        self.assertEqual(T.canonical_json(one),
                         '{"a":{"c":[3,{"x":2,"y":1}],"d":2},"b":1}')

    def test_dataclasses_and_key_order_agree(self):
        record = _samples()["EvidenceRecord"]
        plain = record.to_dict()
        reordered = dict(reversed(list(plain.items())))
        self.assertEqual(T.canonical_json(record), T.canonical_json(plain))
        self.assertEqual(T.canonical_json(record), T.canonical_json(reordered))

    def test_human_request_payload_is_byte_stable(self):
        request = _samples()["HumanRequest"]
        again = T.HumanRequest.from_dict(
            dict(reversed(list(request.to_dict().items()))))
        self.assertEqual(T.canonical_json(request), T.canonical_json(again))

    def test_non_ascii_is_kept_as_is(self):
        self.assertEqual(T.canonical_json({"k": "é"}), '{"k":"é"}')


class EvidenceDigest(unittest.TestCase):

    def setUp(self):
        self.record = _samples()["EvidenceRecord"]
        self.base = self.record.compute_digest()

    def test_digest_is_sha256_hex(self):
        self.assertRegex(self.base, r"^[0-9a-f]{64}$")
        self.assertEqual(self.base, self.record.compute_digest())

    def test_digest_changes_when_any_covered_field_changes(self):
        changes = {
            "v": 2,
            "seq": 1,
            "kind": T.RecordKind.APPROVAL,
            "gate": "other",
            "at": "2026-11-14T10:00:00+00:00",
            "surface_digest": HEX_B,
            "producers": (),
            "config_digest": HEX_A,
            "actor": T.Actor(kind=T.ActorKind.HUMAN, id="ops", channel="tty"),
            "prev": HEX_B,
        }
        covered = {f.name for f in dataclasses.fields(T.EvidenceRecord)} - {"digest", "mac"}
        self.assertEqual(set(changes), covered)
        for name, value in changes.items():
            with self.subTest(field=name):
                changed = dataclasses.replace(self.record, **{name: value})
                self.assertNotEqual(changed.compute_digest(), self.base)

    def test_digest_covers_nested_producer_fields(self):
        producers = list(self.record.producers)
        producers[0] = dataclasses.replace(producers[0], digest=HEX_B)
        changed = dataclasses.replace(self.record, producers=tuple(producers))
        self.assertNotEqual(changed.compute_digest(), self.base)

    def test_digest_and_mac_are_not_covered(self):
        changed = dataclasses.replace(self.record, digest=HEX_B, mac="m" * 64)
        self.assertEqual(changed.compute_digest(), self.base)


class LazyApi(unittest.TestCase):

    def test_importing_api_loads_no_other_kernel_module(self):
        code = ("import sys, json; import core.kernel.api; "
                "print(json.dumps(sorted(m for m in sys.modules "
                "if m == 'core' or m.startswith('core.kernel'))))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                             capture_output=True, text=True, check=True).stdout
        loaded = json.loads(out)
        self.assertEqual(loaded, ["core", "core.kernel", "core.kernel.api"])

    def test_every_type_name_resolves_through_api(self):
        for name in TYPE_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(api, name), getattr(T, name))

    def test_unknown_name_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            getattr(api, "no_such_public_name")

    def test_mapped_names_are_routed_to_their_module(self):
        for module, names in API_MAP.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(api._LOOKUP.get(name), module)
        for name in TYPE_NAMES:
            self.assertEqual(api._LOOKUP.get(name), "types")

    def test_dir_lists_every_mapped_name(self):
        listed = set(dir(api))
        for names in API_MAP.values():
            for name in names:
                self.assertIn(name, listed)
        for name in TYPE_NAMES:
            self.assertIn(name, listed)


if __name__ == "__main__":
    unittest.main()
