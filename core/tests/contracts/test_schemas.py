"""Contract tests for the adapter schema 2 and the pack schema (brief C2).

Standard library and PyYAML only. The validator in this file is the reference
interpreter for the schema language: K2 (kernel/config.py) re-implements the
same language in about 150 lines. Runs with the system python3 and with
.venv/bin/python.

Schema language: type (object, array, string, integer, number, boolean),
required, properties, items, enum, pattern (a full match), additional: false
(no keys beyond properties). Cross-field rules are listed by name under the
top-level ``rules`` key; each has a message template that the validator fills
with the named fields.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import copy
import json
import re
import tempfile
import unittest

import yaml

import fixtures_v2 as fx

REPO = _bootstrap.REPO
SCHEMA_DIR = REPO / "core" / "kernel" / "schema"
ADAPTER_SCHEMA = SCHEMA_DIR / "adapter.v2.schema.yaml"
PACK_SCHEMA = SCHEMA_DIR / "pack.schema.yaml"
EXAMPLES = SCHEMA_DIR / "examples"
EVAL_ADAPTER = REPO / "core" / "tests" / "kernel" / "fixtures" / "eval_pack_v1" / "harness" / "adapter" / "adapter.yaml"  # schema-1 copy (D6)

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


# --------------------------------------------------------------------------
# Reference validator (the language described in the module docstring)
# --------------------------------------------------------------------------

def _join(path, key):
    return f"{path}.{key}" if path else str(key)


def _check_node(value, schema, path, errors):
    kind = schema.get("type")
    if kind is not None:
        ok = isinstance(value, _TYPES[kind])
        if kind in ("integer", "number") and isinstance(value, bool):
            ok = False
        if not ok:
            errors.append(f"{path or '<root>'}: expected {kind}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}, got {value!r}")
    if "pattern" in schema and isinstance(value, str):
        if not re.fullmatch(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{_join(path, key)}: required key is missing")
        if schema.get("additional") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{_join(path, key)}: unknown key")
        for key, sub in props.items():
            if key in value:
                _check_node(value[key], sub, _join(path, key), errors)
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check_node(item, schema["items"], f"{path}[{index}]", errors)


def _escapes(glob):
    """A glob escapes the unit when it is absolute or has a '..' segment."""
    return glob.startswith("/") or ".." in glob.split("/")


def _run_rules(data, rules, errors):
    templates = {rule["name"]: rule["message"] for rule in rules}

    def emit(name, **params):
        errors.append(templates[name].format(**params))

    if not isinstance(data, dict):
        return
    checks = data.get("checks", [])
    if isinstance(checks, list):
        seen = set()
        for index, check in enumerate(checks):
            cid = check.get("id") if isinstance(check, dict) else None
            if isinstance(cid, str):
                if cid in seen:
                    emit("duplicate-check-id", index=index, id=cid)
                seen.add(cid)

    gates = data.get("gates")
    if not isinstance(gates, list):
        return
    if not gates:
        emit("gates-required")
        return
    gates = [gate if isinstance(gate, dict) else {} for gate in gates]
    ids = [gate["id"] if isinstance(gate.get("id"), str) else None for gate in gates]
    known = {gid for gid in ids if gid is not None}

    seen = set()
    for index, gid in enumerate(ids):
        if gid is None:
            continue
        if gid in seen:
            emit("duplicate-gate-id", index=index, id=gid)
        seen.add(gid)

    # Effective needs: the explicit list, or the previous gate in the YAML.
    graph = {}
    for index, gate in enumerate(gates):
        gid = ids[index]
        if gid is None:
            continue
        if "needs" in gate:
            needs = gate["needs"] if isinstance(gate["needs"], list) else []
        else:
            needs = [ids[index - 1]] if index > 0 and ids[index - 1] else []
        needs = [need for need in needs if isinstance(need, str)]
        for need in needs:
            if need not in known:
                emit("needs-unknown-gate", index=index, need=need)
        graph[gid] = [need for need in needs if need in known]

    def reaches_itself(start):
        stack = list(graph.get(start, []))
        visited = set()
        while stack:
            node = stack.pop()
            if node == start:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(graph.get(node, []))
        return False

    for index, gid in enumerate(ids):
        if gid is not None and reaches_itself(gid):
            emit("needs-cycle", index=index, id=gid)

    for index, gate in enumerate(gates):
        has_work = bool(gate.get("checks")) or bool(gate.get("verifiers")) or "approval" in gate
        if not has_work:
            emit("gate-without-work", index=index, id=ids[index] or "?")

    for index, gate in enumerate(gates):
        surface = gate.get("surface") if isinstance(gate.get("surface"), dict) else {}
        fields = (
            ("surface.include", surface.get("include")),
            ("surface.exclude", surface.get("exclude")),
            ("produces", gate.get("produces")),
        )
        for key, globs in fields:
            if not isinstance(globs, list):
                continue
            for glob in globs:
                if isinstance(glob, str) and _escapes(glob):
                    emit("surface-escapes-unit", index=index, key=key, glob=glob)


def validate(data, schema):
    """Return a list of error messages; an empty list means valid."""
    errors = []
    _check_node(data, schema, "", errors)
    _run_rules(data, schema.get("rules", []), errors)
    return errors


def load_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def names_key(message, key):
    """True when ``key`` is a segment of the message's path (text before ': ')."""
    path = message.split(": ", 1)[0]
    return key in [segment for segment in re.split(r"[.\[\]]+", path) if segment]


# --------------------------------------------------------------------------
# Broken variants of the five-gate adapter (exactly fifteen)
# --------------------------------------------------------------------------

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


BROKEN_ADAPTERS = [
    # (label, mutation, key the message must name)
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


class AdapterSchemaTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.adapter_schema = load_yaml(ADAPTER_SCHEMA)
        cls.pack_schema = load_yaml(PACK_SCHEMA)
        cls.five_gate = load_yaml(EXAMPLES / "adapters" / "five_gate.yaml")

    def test_examples_validate(self):
        for name in ("minimal", "five_gate", "research"):
            with self.subTest(adapter=name):
                data = load_yaml(EXAMPLES / "adapters" / f"{name}.yaml")
                self.assertEqual(validate(data, self.adapter_schema), [])
        for name in ("document", "research"):
            with self.subTest(pack=name):
                data = load_yaml(EXAMPLES / "packs" / name / "pack.yaml")
                self.assertEqual(validate(data, self.pack_schema), [])

    def test_fifteen_broken_adapters_are_rejected_naming_the_key(self):
        self.assertEqual(len(BROKEN_ADAPTERS), 15)
        self.assertEqual(validate(self.five_gate, self.adapter_schema), [])
        for label, mutate, key in BROKEN_ADAPTERS:
            with self.subTest(variant=label):
                doc = copy.deepcopy(self.five_gate)
                mutate(doc)
                errors = validate(doc, self.adapter_schema)
                self.assertTrue(errors, "variant was accepted")
                self.assertTrue(
                    any(names_key(message, key) for message in errors),
                    f"no message names {key!r}: {errors}",
                )

    def test_broken_packs_are_rejected_naming_the_key(self):
        base = load_yaml(EXAMPLES / "packs" / "research" / "pack.yaml")
        missing_reader = copy.deepcopy(base)
        missing_reader["readers"][0].pop("reader")
        string_version = copy.deepcopy(base)
        string_version["version"] = "1"
        for label, doc, key in (
            ("reader entry without reader", missing_reader, "reader"),
            ("version as a string", string_version, "version"),
        ):
            with self.subTest(variant=label):
                errors = validate(doc, self.pack_schema)
                self.assertTrue(any(names_key(m, key) for m in errors), errors)

    def test_five_gate_chain_matches_eval_adapter(self):
        eval_gates = load_yaml(EVAL_ADAPTER)["workflow"]["gates"]
        gates = self.five_gate["gates"]
        ids = [gate["id"] for gate in gates]
        self.assertEqual(ids, eval_gates)
        self.assertEqual(ids, ["scope_confirmed", "composed", "lint_passed", "pdf_verified", "packaged"])
        # Linear chain: each gate needs the previous one (the default, so no key).
        for gate in gates:
            self.assertNotIn("needs", gate)
        # The first gate is the human approval; the others run checks.
        self.assertEqual(gates[0]["approval"], {"required": True, "channel": "tty"})
        for gate in gates[1:]:
            self.assertNotIn("approval", gate)
            self.assertTrue(gate["checks"])
        # Only the last gate seals the unit.
        self.assertEqual([gate["id"] for gate in gates if gate.get("seals_unit")], ["packaged"])

    def test_fixtures_build_projects_and_unit_files_round_trip(self):
        for name in ("minimal", "five_gate", "research"):
            with self.subTest(adapter=name), tempfile.TemporaryDirectory() as tmp:
                root = fx.make_project(tmp, adapter=name)
                adapter_file = root / "harness" / "adapter" / "adapter.yaml"
                self.assertTrue(adapter_file.is_file())
                data = load_yaml(adapter_file)
                self.assertEqual(validate(data, self.adapter_schema), [])
                for pack in data.get("packs", []):
                    self.assertTrue((root / "packs" / pack / "pack.yaml").is_file())
                unit = fx.unit_path(root)
                self.assertTrue(unit.is_dir())
                self.assertEqual(list(unit.iterdir()), [])
                self.assertIsNone(fx.read_manifest(unit))
                self.assertEqual(fx.read_evidence(unit), [])
                fx.write_unit_file(unit, "manifest.yaml", "gates: []\n")
                fx.write_unit_file(unit, "evidence.jsonl", json.dumps({"seq": 1}) + "\n")
                self.assertEqual(fx.read_manifest(unit), {"gates": []})
                self.assertEqual(fx.read_evidence(unit), [{"seq": 1}])


if __name__ == "__main__":
    unittest.main()
