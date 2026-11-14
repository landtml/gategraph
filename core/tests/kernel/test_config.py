"""Tests for the configuration loader (brief K2).

Runs with the system python3: the kernel is standard library and PyYAML only.
Covers schema 2 (adapter.v2.schema.yaml with its named rules, and pack.yaml),
the schema-1 translator (config_v1.py), and config_digest.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import copy
import functools
import re
import tempfile
import unittest
from pathlib import Path

import yaml

import fixtures_v2 as fx
from core.kernel import config_v1
from core.kernel.config import Config, ConfigError, config_digest, load, parse_document
from core.kernel.evidence import make_record
from core.kernel.surface import effective_gates, hash_surface, load_surface, stale_gates, store_surface
from core.kernel.types import Approval, CheckSpec, GateSpec, RecordKind, Severity
from core.kernel.types import RepairSpec, SurfaceSpec, VerifierSpec

REPO = _bootstrap.REPO
# Schema-1 eval pack (the adapter only), copied from eval/pack at the B5a deletion (D6).
EVAL_PACK_V1 = REPO / "core" / "tests" / "kernel" / "fixtures" / "eval_pack_v1"
EVAL_ADAPTER = EVAL_PACK_V1 / "harness" / "adapter" / "adapter.yaml"
SEED_ADAPTER = REPO / "seed" / "adapter.yaml"
# The legacy (schema-1) seed as it stood at tag legacy-0.4, frozen here so the translator
# test keeps running on the real legacy file after seed/adapter.yaml moved to schema 2.
LEGACY_SEED_ADAPTER = fx.CORE / "tests" / "kernel" / "fixtures" / "seed_adapter_v1.yaml"
FIVE_GATE = fx.EXAMPLES / "adapters" / "five_gate.yaml"


def _load_yaml(path):
    """A document as the loader reads it (YAML 1.2 booleans, so ``on`` stays a string)."""
    return parse_document(Path(path).read_text(encoding="utf-8"))


def _names_key(problem, key):
    """True when ``key`` is a segment of the problem's path (text before ': ')."""
    path = problem.split(": ", 1)[0]
    return key in [s for s in re.split(r"[./\[\]\s]+", path) if s]


def _write_adapter(root, doc):
    path = Path(root) / "harness" / "adapter" / "adapter.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _set(key, value):
    def mutate(doc):
        doc[key] = value
    return mutate


def _gate_set(index, key, value):
    def mutate(doc):
        doc["gates"][index][key] = value
    return mutate


def _gate_pop(index, key):
    def mutate(doc):
        doc["gates"][index].pop(key)
    return mutate


def _check_set(index, key, value):
    def mutate(doc):
        doc["checks"][index][key] = value
    return mutate


# The fifteen broken variants of the five-gate example (the same list as the
# C2 contract test): (label, mutation, key the problem must name).
BROKEN_ADAPTERS = [
    ("duplicate gate id", _gate_set(1, "id", "scope_confirmed"), "id"),
    ("needs cycle", _gate_set(0, "needs", ["packaged"]), "needs"),
    ("unknown severity", _check_set(0, "severity", "Fatal"), "severity"),
    ("surface glob escapes the unit", _gate_set(2, "surface", {"include": ["../outside/**"]}), "surface"),
    ("gate with neither checks, verifiers nor approval", _gate_pop(2, "checks"), "checks"),
    ("non-string gate id", _gate_set(3, "id", 7), "id"),
    ("legacy hashed_gates list", _set("hashed_gates", ["lint_passed"]), "hashed_gates"),
    ("unknown gate key", _gate_set(0, "tool_set", True), "tool_set"),
    ("absolute surface glob", _gate_set(2, "surface", {"include": ["/etc/**"]}), "surface"),
    ("verifier without n", _gate_set(2, "verifiers", [{"rubric": "rubric.md", "model": "reviewer"}]), "n"),
    ("repair max_attempts not an integer", _gate_set(2, "repair", {"max_attempts": "two"}), "max_attempts"),
    ("needs an unknown gate", _gate_set(1, "needs", ["ghost"]), "needs"),
    ("schema version 1", _set("schema", 1), "schema"),
    ("approval channel outside the enumeration", _gate_set(0, "approval", {"required": True, "channel": "email"}), "channel"),
    ("check id is not a slug", _check_set(0, "id", "Bad Id"), "id"),
]

# Placeholder values that turn seed/adapter.yaml (a stamped-at-install template)
# into a schema-1 document. Only the placeholders are filled; nothing else changes.
SEED_VALUES = {
    "{{FH_PROJECT}}": "seed-example",
    "{{FH_ARTIFACT_NOUN}}": "report",
    "{{FH_UNIT_DIR}}": "reports",
    "{{FH_GATES}}": "[composed, lint_passed]",
    "{{FH_HASHED_GATES}}": "[lint_passed]",
    "{{FH_TOOL_SET_GATES}}": "[composed, lint_passed]",
    "{{FH_GATE_COMMANDS}}": (
        '    composed: "python3 harness/adapter/checks/composed.py --unit {slug}"\n'
        '    lint_passed: "python3 harness/core/lint.py --unit {slug} --set-gate lint_passed"'
    ),
    "{{FH_POLICIES_BLOCK}}": "  deny: []\n  ask: []",
    "{{FH_LOOP_ENABLED}}": "false",
}


class ExamplesLoadTests(unittest.TestCase):

    def test_example_check_commands_use_only_unit_and_pack_placeholders(self):
        # Item 6: the runner substitutes {unit} and {pack} in a check command. An example that
        # names any other placeholder (the retired {slug}, or {files}) would run with it unexpanded.
        allowed = {"unit", "pack"}
        seen, bad = 0, []
        for path in sorted(fx.EXAMPLES.rglob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            items = list(doc.get("checks") or []) if isinstance(doc, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                command = item.get("command") or item.get("cmd") or ""
                seen += 1
                for name in re.findall(r"\{([^{}]*)\}", str(command)):
                    if name not in allowed:
                        bad.append(f"{path.relative_to(fx.EXAMPLES)}: {item.get('id')}: {{{name}}}")
        self.assertGreater(seen, 0, "no example check commands were found")
        self.assertEqual(bad, [], "example check commands with placeholders outside {unit}/{pack}")

    def test_examples_load(self):
        for name in fx.ADAPTERS:
            with self.subTest(adapter=name), tempfile.TemporaryDirectory() as tmp:
                root = fx.make_project(tmp, adapter=name)
                cfg = load(root)
                self.assertIsInstance(cfg, Config)
                self.assertEqual(cfg.schema, 2)
                self.assertTrue(cfg.gates)
                for gate in cfg.gates:
                    self.assertIsInstance(gate, GateSpec)

    def test_five_gate_maps_to_gate_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load(fx.make_project(tmp, adapter="five_gate"))
        ids = tuple(gate.id for gate in cfg.gates)
        self.assertEqual(ids, ("scope_confirmed", "composed", "lint_passed", "pdf_verified", "packaged"))
        first = cfg.gates[0]
        self.assertEqual(first.approval, Approval(required=True, channel="tty"))
        self.assertEqual(first.checks, ())
        # Default needs: the previous gate in the YAML.
        self.assertEqual(cfg.gate("lint_passed").needs, ("composed",))
        self.assertEqual(cfg.gate("scope_confirmed").needs, ())
        self.assertEqual([g.id for g in cfg.gates if g.seals_unit], ["packaged"])
        # schema-2 key ``command`` maps to CheckSpec.command. The lint gate runs the built-in
        # policy check, never lint.py (which refuses --unit).
        policy = cfg.check("policy")
        self.assertIsInstance(policy, CheckSpec)
        self.assertEqual(policy.command, "python3 harness/adapter/checks/policy_scan.py --unit {unit}")
        self.assertEqual(policy.severity, Severity.FATAL)
        self.assertEqual(cfg.gate("lint_passed").checks, ("policy",))
        self.assertNotIn("lint.py", " ".join(c.command for c in cfg.checks))

    def test_research_maps_verifiers_surface_repair_and_pack_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load(fx.make_project(tmp, adapter="research"))
        self.assertEqual(cfg.packs, ("document", "research"))
        verified = cfg.gate("verified")
        self.assertEqual(
            verified.verifiers,
            (VerifierSpec(id="verified.v1", rubric="harness/adapter/review_rubric.md", n=3, model="reviewer"),),
        )
        self.assertEqual(verified.surface, SurfaceSpec(include=("**/*.md", "sources/**"), exclude=("build/**",)))
        self.assertEqual(verified.repair, RepairSpec(max_attempts=2))
        self.assertEqual(cfg.gate("packaged").needs, ("verified",))
        # Pack check: ``cmd`` maps to ``command``; {pack} resolves to packs/<name>.
        claims = cfg.check("claims-have-sources")
        self.assertEqual(claims.command, "python3 packs/research/checks/claims.py {unit}")
        self.assertEqual(claims.on, ("**/*.md",))
        self.assertEqual(cfg.check("render").command, "python3 packs/document/checks/render.py {unit}")
        self.assertEqual(cfg.gate("packaged").checks, ("render", "bundle"))

    def test_verifier_ids_are_numbered_by_position(self):
        doc = _load_yaml(FIVE_GATE)
        doc["gates"][2]["verifiers"] = [
            {"rubric": "a.md", "n": 1, "model": "reviewer"},
            {"rubric": "b.md", "n": 2, "model": "reviewer"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="five_gate")
            _write_adapter(root, doc)
            cfg = load(root)
        self.assertEqual([v.id for v in cfg.gate("lint_passed").verifiers],
                         ["lint_passed.v1", "lint_passed.v2"])


class SchemaRejectionTests(unittest.TestCase):

    def test_examples_are_valid_and_fifteen_broken_variants_name_the_key(self):
        five_gate = _load_yaml(FIVE_GATE)
        self.assertEqual(len(BROKEN_ADAPTERS), 15)
        for label, mutate, key in BROKEN_ADAPTERS:
            with self.subTest(variant=label), tempfile.TemporaryDirectory() as tmp:
                root = fx.make_project(tmp, adapter="five_gate")
                doc = copy.deepcopy(five_gate)
                mutate(doc)
                _write_adapter(root, doc)
                with self.assertRaises(ConfigError) as ctx:
                    load(root)
                problems = ctx.exception.problems
                self.assertTrue(problems, "variant was accepted")
                self.assertTrue(
                    any(_names_key(p, key) for p in problems),
                    f"no problem names {key!r}: {problems}",
                )

    def test_B12_unknown_severity_is_an_error(self):
        # Adapter check severity: a misspelt value must not default to warn.
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="five_gate")
            doc = _load_yaml(FIVE_GATE)
            doc["checks"][0]["severity"] = "Fatal"
            _write_adapter(root, doc)
            with self.assertRaises(ConfigError) as ctx:
                load(root)
        self.assertTrue(any(_names_key(p, "severity") and "Fatal" in p for p in ctx.exception.problems))
        # Schema-1 check severity, the same rule through the translator.
        with tempfile.TemporaryDirectory() as tmp:
            raw = _load_yaml(EVAL_ADAPTER)
            raw["checks"] = [{"id": "x", "cmd": "python3 x.py", "on": ["**/*.md"], "severity": "Fatal"}]
            root = Path(tmp)
            _write_adapter(root, raw)
            with self.assertRaises(ConfigError) as ctx:
                load(root)
        self.assertTrue(any(_names_key(p, "severity") for p in ctx.exception.problems))

    def test_B40_duplicate_gate_rejected(self):
        doc = _load_yaml(FIVE_GATE)
        doc["gates"][3]["id"] = "composed"
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="five_gate")
            _write_adapter(root, doc)
            with self.assertRaises(ConfigError) as ctx:
                load(root)
        self.assertIn("gates[3].id: duplicate gate id 'composed'", ctx.exception.problems)

    def test_B40_duplicate_gate_rejected_in_schema_1(self):
        raw = _load_yaml(EVAL_ADAPTER)
        raw["workflow"]["gates"] = ["scope_confirmed", "composed", "composed", "pdf_verified", "packaged"]
        with tempfile.TemporaryDirectory() as tmp:
            _write_adapter(tmp, raw)
            with self.assertRaises(ConfigError) as ctx:
                load(tmp)
        self.assertTrue(any(_names_key(p, "gates") and "composed" in p for p in ctx.exception.problems))

    def test_B04_v1_hashed_not_toolset_rejected(self):
        raw = _load_yaml(EVAL_ADAPTER)
        raw["workflow"]["tool_set_gates"] = [g for g in raw["workflow"]["tool_set_gates"] if g != "packaged"]
        self.assertIn("packaged", raw["workflow"]["hashed_gates"])
        with tempfile.TemporaryDirectory() as tmp:
            _write_adapter(tmp, raw)
            with self.assertRaises(ConfigError) as ctx:
                load(tmp)
        self.assertTrue(any(_names_key(p, "hashed_gates") and "packaged" in p
                            for p in ctx.exception.problems), ctx.exception.problems)

    def test_broken_pack_names_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="research")
            pack = root / "packs" / "research" / "pack.yaml"
            doc = _load_yaml(pack)
            doc["readers"][0].pop("reader")
            pack.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load(root)
        self.assertTrue(any(_names_key(p, "reader") for p in ctx.exception.problems), ctx.exception.problems)

    def test_unknown_check_reference_names_the_key(self):
        doc = _load_yaml(FIVE_GATE)
        doc["gates"][2]["checks"] = ["ghost"]
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="five_gate")
            _write_adapter(root, doc)
            with self.assertRaises(ConfigError) as ctx:
                load(root)
        self.assertTrue(any(p.startswith("gates[2].checks: unknown check 'ghost'")
                            for p in ctx.exception.problems), ctx.exception.problems)

    def test_missing_pack_names_the_key(self):
        doc = _load_yaml(fx.EXAMPLES / "adapters" / "research.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="research")
            doc["packs"] = ["document", "absent"]
            _write_adapter(root, doc)
            with self.assertRaises(ConfigError) as ctx:
                load(root)
        self.assertTrue(any(_names_key(p, "packs") and "absent" in p for p in ctx.exception.problems))

    def test_verifier_n_below_one_names_the_key(self):
        doc = _load_yaml(fx.EXAMPLES / "adapters" / "research.yaml")
        doc["gates"][2]["verifiers"][0]["n"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="research")
            _write_adapter(root, doc)
            with self.assertRaises(ConfigError) as ctx:
                load(root)
        self.assertIn("gates[2].verifiers[0].n: must be at least 1, got 0", ctx.exception.problems)

    def test_load_raises_only_config_error(self):
        cases = {
            "missing file": None,
            "not yaml": "gates: [\n",
            "root is a list": "- one\n- two\n",
            "no schema key": "project: x\n",
        }
        for label, text in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                if text is not None:
                    path = Path(tmp) / "harness" / "adapter" / "adapter.yaml"
                    path.parent.mkdir(parents=True)
                    path.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load(tmp)


class SchemaOneTests(unittest.TestCase):

    def test_v1_adapter_translates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_adapter(root, _load_yaml(EVAL_ADAPTER))
            cfg = load(root)
        five = load_five_gate_config()
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.schema, 1)
        self.assertEqual(type(cfg), type(five))
        self.assertEqual([g.id for g in cfg.gates], [g.id for g in five.gates])
        self.assertTrue(all(isinstance(g, GateSpec) for g in cfg.gates))
        self.assertEqual(cfg.gates[0].approval, five.gates[0].approval)
        self.assertEqual([g.id for g in cfg.gates if g.seals_unit], ["packaged"])
        # The gate commands of the eval pack are the five-gate example's commands,
        # with the schema-1 ``--set-gate`` flag removed (only the seal node sets a gate).
        for gate in five.gates[1:]:
            if gate.id == "lint_passed":
                continue  # the legacy tool-set gate: see test_lint_set_gate_becomes_policy_and_adapter_checks
            (check_id,) = gate.checks
            (v1_id,) = cfg.gate(gate.id).checks
            self.assertEqual(cfg.check(v1_id).command, five.check(check_id).command, gate.id)

    def test_lint_set_gate_becomes_policy_and_adapter_checks(self):
        # Item 9: `lint.py --unit S --set-gate G` is retired (it refuses). The gate runs the
        # built-in policy scan (the seed's `policy` check) and the adapter's own checks, and
        # never the lint command.
        raw = _load_yaml(EVAL_ADAPTER)
        raw["checks"] = [{"id": "v1-a", "cmd": "python3 harness/adapter/checks/a.py",
                          "on": ["dossiers/**"]}]
        cfg = config_v1.translate(raw, EVAL_PACK_V1)
        gate = cfg.gate("lint_passed")
        self.assertEqual(gate.checks, ("policy", "v1-a"))
        self.assertEqual(cfg.check("policy").command,
                         "python3 harness/adapter/checks/policy_scan.py --unit {unit}")
        self.assertEqual(cfg.check("policy").severity, Severity.FATAL)
        self.assertEqual([c.id for c in cfg.checks].count("policy"), 1)
        self.assertNotIn("lint.py", " ".join(c.command for c in cfg.checks))
        self.assertTrue(gate.surface.include, "a hashed lint gate keeps its surface")

    def test_lint_set_gate_refuses_a_clash_with_the_policy_check_id(self):
        raw = _load_yaml(EVAL_ADAPTER)
        raw["checks"] = [{"id": "policy", "cmd": "python3 x.py", "on": ["**"]}]
        with self.assertRaises(ConfigError) as caught:
            config_v1.translate(raw, EVAL_PACK_V1)
        self.assertTrue(any("reserved" in p for p in caught.exception.problems))

    def test_v1_seed_template_translates_once_its_placeholders_are_filled(self):
        text = LEGACY_SEED_ADAPTER.read_text(encoding="utf-8")
        for placeholder, value in SEED_VALUES.items():
            text = text.replace(placeholder, value)
        self.assertNotIn("{{", text)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness" / "adapter" / "adapter.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(text, encoding="utf-8")
            cfg = load(tmp)
        self.assertEqual(cfg.project, "seed-example")
        self.assertEqual([g.id for g in cfg.gates], ["composed", "lint_passed"])

    def test_v1_translator_entry_point(self):
        raw = _load_yaml(EVAL_ADAPTER)
        cfg = config_v1.translate(raw, EVAL_PACK_V1)
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.gate("scope_confirmed").approval, Approval(required=True, channel="tty"))


class LegacySurfaceTests(unittest.TestCase):
    """The translator keeps the legacy staleness rules (core/manifest.py 129 to 174):
    unhashed and approval gates have an empty surface, hashed gates exempt the
    declared reviewer evidence mid-flow, and the final (sealing) gate exempts nothing."""

    def _eval_gates(self):
        cfg = config_v1.translate(_load_yaml(EVAL_ADAPTER), EVAL_PACK_V1)
        return {gate.id: gate for gate in cfg.gates}

    def test_unhashed_and_approval_gates_get_empty_surface(self):
        gates = self._eval_gates()
        for gate_id in ("scope_confirmed", "composed"):
            with self.subTest(gate=gate_id):
                self.assertEqual(gates[gate_id].surface, SurfaceSpec(include=()))

    def test_hashed_midflow_gates_exclude_review_json(self):
        gates = self._eval_gates()
        for gate_id in ("lint_passed", "pdf_verified"):
            with self.subTest(gate=gate_id):
                self.assertEqual(gates[gate_id].surface,
                                 SurfaceSpec(include=("**",), exclude=("review.json",)))

    def test_final_hashed_gate_does_not_exempt_review_json(self):
        gates = self._eval_gates()
        self.assertTrue(gates["packaged"].seals_unit)
        self.assertEqual(gates["packaged"].surface, SurfaceSpec(include=("**",), exclude=()))


class LegacyStalenessEndToEndTests(unittest.TestCase):

    def test_edit_after_pdf_verified_matches_golden(self):
        # Golden scenario stale-edit-reopens-downstream-chain (tests/golden/scenarios.yaml,
        # snapshot tests/golden/expected/stale-edit-reopens-downstream-chain.json): the legacy
        # engine earns every gate through packaged (pdf_verified included), then edits main.tex.
        # Snapshot after the edit: effective [scope_confirmed, composed],
        # stale [lint_passed, pdf_verified, packaged]. No scenario ends at pdf_verified
        # itself, so this is the nearest one that edits after pdf_verified passes.
        cfg = config_v1.translate(_load_yaml(EVAL_ADAPTER), EVAL_PACK_V1)
        all_gates = [gate.id for gate in cfg.gates]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            unit = Path(tmp) / "dossiers" / "u1"
            unit.mkdir(parents=True)
            (unit / "main.tex").write_text("a\n", encoding="utf-8")
            chain = []
            surfaces = functools.partial(load_surface, state)

            for gate_id in all_gates:
                digest = store_surface(state, hash_surface(unit, gate_id, cfg.gates))
                kind = RecordKind.APPROVAL if cfg.gate(gate_id).approval else RecordKind.GATE
                chain.append(make_record(
                    chain, kind=kind, gate=gate_id, at=f"2026-01-01T00:00:{len(chain):02d}+00:00",
                    surface_digest=digest, producers=(), config_digest="c" * 64,
                    actor={"kind": "tool", "id": "test-tool", "channel": None}))

            self.assertEqual(effective_gates(chain, cfg, unit, surfaces), all_gates)
            self.assertEqual(stale_gates(chain, cfg, unit, surfaces), [])

            (unit / "main.tex").write_text("b\n", encoding="utf-8")
            self.assertEqual(effective_gates(chain, cfg, unit, surfaces), ["scope_confirmed", "composed"])
            self.assertEqual(stale_gates(chain, cfg, unit, surfaces),
                             ["lint_passed", "pdf_verified", "packaged"])


def load_five_gate_config():
    with tempfile.TemporaryDirectory() as tmp:
        return load(fx.make_project(tmp, adapter="five_gate"))


class DigestTests(unittest.TestCase):

    def test_config_digest_changes_when_a_check_script_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="five_gate")
            script = root / "harness" / "adapter" / "checks" / "policy_scan.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('v1')\n", encoding="utf-8")
            first = config_digest(load(root))
            self.assertRegex(first, r"^[0-9a-f]{64}$")
            self.assertEqual(first, config_digest(load(root)), "digest is not stable")
            script.write_text("print('v2')\n", encoding="utf-8")
            second = config_digest(load(root))
            self.assertNotEqual(first, second)
            # Creating a script that a check names also changes the digest.
            (root / "harness" / "adapter" / "checks").mkdir(parents=True, exist_ok=True)
            before = config_digest(load(root))
            (root / "harness" / "adapter" / "checks" / "build.py").write_text("x\n", encoding="utf-8")
            self.assertNotEqual(before, config_digest(load(root)))

    def test_config_digest_changes_for_pack_scripts_and_adapter_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="research")
            claims = root / "packs" / "research" / "checks" / "claims.py"
            claims.parent.mkdir(parents=True)
            claims.write_text("print('a')\n", encoding="utf-8")
            first = config_digest(load(root))
            claims.write_text("print('b')\n", encoding="utf-8")
            second = config_digest(load(root))
            self.assertNotEqual(first, second)
            adapter = root / "harness" / "adapter" / "adapter.yaml"
            adapter.write_text(adapter.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
            self.assertNotEqual(second, config_digest(load(root)))


EVAL_DENY_IDS = ("acct", "tariffs", "notes-section", "scratch-section",
                 "disclosure-client", "disclosure-product", "shouting",
                 "style-emdash", "style-buzzwords", "style-cliche", "style-not-just")
EVAL_PROTECTED = ("dossiers/*/manifest.yaml", "dossiers/.active")


def _copy_adapter(root, source):
    path = Path(root) / "harness" / "adapter" / "adapter.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
    return path


class PolicyTests(unittest.TestCase):
    """The policy attributes the guard reads (deny, ask, protected_paths, blind),
    from the schema-2 ``policy:`` block and from the legacy schema-1 ``policies:``."""

    def test_five_gate_policy_block_loads_into_config(self):
        cfg = load_five_gate_config()
        self.assertEqual(tuple(p["id"] for p in cfg.deny), EVAL_DENY_IDS)
        self.assertEqual(cfg.protected_paths, EVAL_PROTECTED)
        self.assertEqual(cfg.ask, ())
        self.assertEqual(cfg.blind, ())
        pretool = {p["id"]: p.get("pretool", True) for p in cfg.deny}
        self.assertTrue(pretool["acct"])
        self.assertFalse(pretool["shouting"])

    def test_eval_pack_adapter_policies_load_through_schema_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            _copy_adapter(tmp, EVAL_ADAPTER)
            cfg = load(tmp)
        self.assertEqual(cfg.schema, 1)
        self.assertEqual(tuple(p["id"] for p in cfg.deny), EVAL_DENY_IDS)
        self.assertEqual(cfg.protected_paths, EVAL_PROTECTED)
        self.assertEqual(cfg.ask, ())
        self.assertEqual(cfg.blind, ())

    def test_five_gate_policies_equal_the_eval_pack_policies(self):
        five = load_five_gate_config()
        with tempfile.TemporaryDirectory() as tmp:
            _copy_adapter(tmp, EVAL_ADAPTER)
            legacy = load(tmp)
        self.assertEqual(five.deny, legacy.deny)
        self.assertEqual(five.ask, legacy.ask)
        self.assertEqual(five.protected_paths, legacy.protected_paths)
        self.assertEqual(five.blind, legacy.blind)

    def test_adapter_without_policy_block_loads_with_no_policies(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load(fx.make_project(tmp, adapter="minimal"))
        self.assertEqual((cfg.deny, cfg.ask, cfg.protected_paths, cfg.blind), ((), (), (), ()))

    def test_uncompilable_policy_regex_is_a_config_error_naming_the_key(self):
        doc = _load_yaml(FIVE_GATE)
        doc["policy"]["deny"][0]["regex"] = "("
        with tempfile.TemporaryDirectory() as tmp:
            _write_adapter(tmp, doc)
            with self.assertRaises(ConfigError) as caught:
                load(tmp)
        self.assertTrue(any(_names_key(p, "regex") for p in caught.exception.problems),
                        caught.exception.problems)

    def test_policy_block_is_covered_by_config_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fx.make_project(tmp, adapter="five_gate")
            first = config_digest(load(root))
            doc = _load_yaml(FIVE_GATE)
            doc.setdefault("policy", {}).setdefault("protected_paths", []).append("dossiers/extra.md")
            _write_adapter(root, doc)
            self.assertNotEqual(first, config_digest(load(root)))


class ReferenceTests(unittest.TestCase):

    def test_seed_and_eval_files_exist(self):
        self.assertTrue(EVAL_ADAPTER.is_file())
        self.assertTrue(SEED_ADAPTER.is_file())
        self.assertTrue(LEGACY_SEED_ADAPTER.is_file())

    def test_missing_adapter_is_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load(tmp)


if __name__ == "__main__":
    unittest.main()
