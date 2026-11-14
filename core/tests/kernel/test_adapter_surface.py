"""The adapter-config surface stays honest for schema 2 (ported from the legacy test_schema_surface.py;
X1 B2, M2 step 3).

- The seed renders to a schema-2 adapter that the kernel loader accepts. The loader rejects every
  key the schema does not declare, so no dead key can ship without failing this test.
- The seed carries no schema-1 or dead names: fh_schema, workflow, hashed_gates, tool_set_gates,
  gate_commands, escalation, disagreement_threshold, error_prob_threshold, terminal_states,
  confidence_signal, aggregate. Schema 2 has no escalation block at all.
- The live search knob graduate_target is not an adapter key. It is the search CLI flag
  --graduate-target, and the search graph reads it.
- The kernel Config exposes no attribute that nothing reads.

The seed checks are template-only: a stamped project has no seed/ directory and they skip there.
Standard library and PyYAML only; runs with system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import dataclasses
import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml

from core.kernel import config
from fixtures_v2 import make_project

REPO = _bootstrap.REPO
SEED = REPO / "seed"
HAS_SEED = (SEED / "adapter.yaml").is_file()
SEED_ADAPTER = (SEED / "adapter.yaml").read_text(encoding="utf-8") if HAS_SEED else ""
SCHEMA_FILE = REPO / "core" / "kernel" / "schema" / "adapter.v2.schema.yaml"
template_only = unittest.skipUnless(HAS_SEED, "template repo only (no seed/ in stamps)")

# Names that belong to schema 1, or that nothing reads. None may appear as a key in the seed.
DEAD_KEYS = ("fh_schema", "workflow", "hashed_gates", "tool_set_gates", "gate_commands",
             "escalation", "disagreement_threshold", "error_prob_threshold", "terminal_states",
             "confidence_signal", "aggregate", "graduate_target", "policies", "reviewer",
             "state_reap", "framework_dirs", "artifact_noun", "loop")
# Attributes of the kernel Config that nothing reads.
DEAD_ATTRIBUTES = ("terminal_states", "confidence_signal", "aggregate", "escalation",
                   "hashed_gates", "tool_set_gates", "gate_commands")


def _load_gategraph():
    """The template's stamper, loaded by path (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("gategraph_under_test", REPO / "gategraph.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _keys(node):
    """Every mapping key anywhere in a parsed YAML document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(item)


@template_only
class TestSeedAdapter(unittest.TestCase):
    def rendered(self):
        """The seed rendered the way init renders it, for a fixed set of answers."""
        gb = _load_gategraph()
        answers = gb.normalize_answers({"project": "demo", "artifact_noun": "note",
                                        "unit_dir": "notes"}, Path("demo"))
        return gb.render(SEED_ADAPTER, answers)

    def test_seed_renders_to_a_schema2_adapter_the_loader_accepts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "harness" / "adapter").mkdir(parents=True)
            (root / "harness" / "adapter" / "adapter.yaml").write_text(self.rendered(),
                                                                       encoding="utf-8")
            cfg = config.load(root)
            self.assertEqual(cfg.schema, 2)
            self.assertEqual({g.id for g in cfg.gates}, {"composed", "checked", "verified"})

    def test_seed_keys_are_declared_by_the_schema(self):
        doc = yaml.safe_load(self.rendered())
        schema = yaml.safe_load(SCHEMA_FILE.read_text(encoding="utf-8"))
        top = set(schema["properties"])
        undeclared = set(doc) - top
        self.assertFalse(undeclared, f"seed ships keys the schema does not declare: {undeclared}")
        gate_keys = set(schema["definitions"]["gate"]["properties"])
        for gate in doc["gates"]:
            self.assertFalse(set(gate) - gate_keys, f"gate {gate.get('id')!r} has undeclared keys")

    def test_seed_carries_no_schema1_or_dead_keys(self):
        present = set(_keys(yaml.safe_load(self.rendered()))) & set(DEAD_KEYS)
        self.assertFalse(present, f"seed ships keys nothing reads or schema 2 dropped: {present}")

    def test_seed_has_no_schema1_name_in_its_text(self):
        # The raw seed (before rendering) must not spell the old escalation name either.
        self.assertNotIn("error_prob_threshold", SEED_ADAPTER)
        self.assertNotIn("disagreement_threshold", SEED_ADAPTER)

    def test_live_search_knob_is_a_cli_flag_the_search_graph_reads(self):
        cli_src = (REPO / "core" / "cli" / "search.py").read_text(encoding="utf-8")
        graph_src = (REPO / "core" / "graph" / "search.py").read_text(encoding="utf-8")
        self.assertIn('"--graduate-target"', cli_src)
        self.assertIn('cfg.get("graduate_target")', graph_src)


class TestKernelConfigDropsDeadAttributes(unittest.TestCase):
    def test_config_exposes_no_dead_attribute(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config.load(make_project(Path(td), "minimal"))
            for name in DEAD_ATTRIBUTES:
                self.assertFalse(hasattr(cfg, name),
                                 f"config exposes {name!r}, which nothing reads")
            fields = {f.name for f in dataclasses.fields(config.Config)}
            self.assertFalse(fields & set(DEAD_ATTRIBUTES),
                             "Config declares a field nothing reads")


if __name__ == "__main__":
    unittest.main(verbosity=2)
