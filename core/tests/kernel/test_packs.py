"""Tests for the pack loader (brief D1; plan section 1.13).

Runs with the system python3: standard library and PyYAML only. Projects are
built from the example adapters and packs by fixtures_v2.make_project.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import dataclasses
import tempfile
import unittest
from pathlib import Path

import yaml

import fixtures_v2 as fx
from core.kernel import config
from core.kernel.config import ConfigError
from core.kernel.packs import PackError, load_packs, merge_settings
from core.kernel.types import CheckSpec, Severity


class _ProjectTest(unittest.TestCase):
    def setUp(self):
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        self.base = Path(base.name)
        self.count = 0

    def _project(self, adapter="research", extra_checks=(), pack_edit=None):
        """A project root from an example adapter, optionally edited before loading."""
        self.count += 1
        root = self.base / f"p{self.count}"
        fx.make_project(root, adapter)
        if extra_checks:
            path = root / "harness" / "adapter" / "adapter.yaml"
            doc = config.parse_document(path.read_text(encoding="utf-8"))
            doc["checks"] = doc.get("checks", []) + list(extra_checks)
            path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        if pack_edit is not None:
            path = root / "packs" / "research" / "pack.yaml"
            doc = config.parse_document(path.read_text(encoding="utf-8"))
            pack_edit(doc)
            path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return root


class ExamplePacksTest(_ProjectTest):
    def test_both_example_packs_load(self):
        cfg = config.load(self._project("research"))
        packs = load_packs(cfg)
        self.assertEqual([p.name for p in packs], ["document", "research"])

        research = packs[1]
        self.assertEqual(research.version, 1)
        self.assertEqual(
            [(r.reader, r.match) for r in research.readers],
            [("pdf_text", ("**/*.pdf",)), ("text", ("**/*.md", "**/*.txt"))],
        )
        self.assertEqual(
            research.tools,
            ("read_file", "glob", "grep", "write_file", "edit_file", "fetch_url", "web_search"),
        )
        self.assertEqual(
            [c.id for c in research.checks], ["claims-have-sources", "sources-resolve"]
        )
        self.assertEqual(research.gate_presets,
                         {"default": ("scoped", "sourced", "composed", "verified")})
        self.assertEqual([(a.id, a.cmd) for a in research.advisory],
                         [("pdftotext", "pdftotext -v")])

        document = packs[0]
        self.assertEqual([c.id for c in document.checks], ["render"])
        self.assertEqual([(r.reader, r.match) for r in document.readers],
                         [("text", ("**/*.tex", "**/*.md"))])

    def test_pack_checks_are_the_configuration_checks(self):
        cfg = config.load(self._project("research"))
        for pack in load_packs(cfg):
            for spec in pack.checks:
                self.assertEqual(spec, cfg.check(spec.id))
                self.assertEqual(spec.severity, Severity.FATAL)

    def test_readers_are_registered_in_pack_order(self):
        cfg = config.load(self._project("research"))
        calls = []
        load_packs(cfg, register_reader=lambda reader: calls.append((reader.pack, reader.reader)))
        self.assertEqual(calls, [("document", "text"), ("research", "pdf_text"),
                                 ("research", "text")])

    def test_no_packs_gives_no_packs(self):
        cfg = config.load(self._project("minimal"))
        self.assertEqual(load_packs(cfg), [])


class CollisionTest(_ProjectTest):
    def test_pack_check_colliding_with_adapter_check_names_both(self):
        cfg = config.load(self._project("research"))
        adapter_check = CheckSpec(id="render", command="python3 harness/adapter/checks/r.py",
                                  severity=Severity.FATAL)
        clashing = dataclasses.replace(cfg, checks=cfg.checks + (adapter_check,))
        with self.assertRaises(PackError) as ctx:
            load_packs(clashing)
        text = "\n".join(ctx.exception.problems)
        self.assertIn("render", text)
        self.assertIn("packs/document/pack.yaml", text)
        self.assertIn("adapter", text)

    def test_config_load_rejects_the_same_collision(self):
        clash = {"id": "render", "command": "true", "severity": "fatal"}
        with self.assertRaises(ConfigError) as ctx:
            config.load(self._project("research", extra_checks=[clash]))
        self.assertIn("render", "\n".join(ctx.exception.problems))


class SettingsMergeTest(unittest.TestCase):
    def test_adapter_settings_override_pack_settings_key_by_key(self):
        pack = {"timeout_s": 30, "on": ["**/*.md"], "severity": "warn"}
        adapter = {"timeout_s": 60}
        merged = merge_settings(pack, adapter)
        self.assertEqual(merged, {"timeout_s": 60, "on": ["**/*.md"], "severity": "warn"})
        self.assertEqual(pack, {"timeout_s": 30, "on": ["**/*.md"], "severity": "warn"})
        self.assertEqual(adapter, {"timeout_s": 60})


class ToolCheckTest(_ProjectTest):
    def test_pack_naming_an_unknown_tool_is_an_error_at_load(self):
        def add_unknown(doc):
            doc["tools"] = doc["tools"] + ["frobnicate"]

        cfg = config.load(self._project("research", pack_edit=add_unknown))
        with self.assertRaises(PackError) as ctx:
            load_packs(cfg)
        text = "\n".join(ctx.exception.problems)
        self.assertIn("frobnicate", text)
        self.assertIn("packs/research/pack.yaml", text)

    def test_known_tools_can_be_given_by_the_toolbox(self):
        cfg = config.load(self._project("research"))
        with self.assertRaises(PackError) as ctx:
            load_packs(cfg, known_tools={"read_file"})
        self.assertIn("glob", "\n".join(ctx.exception.problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
