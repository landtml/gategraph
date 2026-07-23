"""Unit tests for the core library modules: config, paths, manifest, policy."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/tests
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # `import core`

from fixtures import ACCT_TOKEN, default_adapter, mkroot, write_adapter  # noqa: E402
from core import config, manifest, paths, policy  # noqa: E402


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_adapter_loads(self):
        cfg = config.load(self.root)
        self.assertEqual(cfg.project, "fixtureproj")
        self.assertEqual(cfg.unit_dir, "units")
        self.assertEqual(cfg.gates, ["composed", "checked", "verified"])
        self.assertEqual(cfg.hashed_gates, ["verified"])
        self.assertEqual(cfg.tool_set_gates, ["verified"])
        self.assertIn("composed", cfg.gate_commands)
        self.assertEqual(len(cfg.deny), 2)
        self.assertEqual(cfg.escalation["fail_streak_to_escalate"], 3)
        self.assertFalse(cfg.loop["enabled"])

    def test_missing_gate_command_rejected(self):
        adapter = default_adapter()
        del adapter["workflow"]["gate_commands"]["checked"]
        write_adapter(self.root, adapter)
        with self.assertRaisesRegex(config.ConfigError, "checked"):
            config.load(self.root)

    def test_hashed_gate_not_in_gates_rejected(self):
        adapter = default_adapter()
        adapter["workflow"]["hashed_gates"] = ["nonexistent"]
        write_adapter(self.root, adapter)
        with self.assertRaisesRegex(config.ConfigError, "nonexistent"):
            config.load(self.root)

    def test_bad_deny_regex_rejected(self):
        adapter = default_adapter()
        adapter["policies"]["deny"][0]["regex"] = "([unclosed"
        write_adapter(self.root, adapter)
        with self.assertRaises(config.ConfigError):
            config.load(self.root)

    def test_check_with_yaml_boolean_on_key_rejected(self):
        # Bare `on:` is a YAML 1.1 boolean literal: the key loads as True, the
        # check's globs vanish, and the check silently never runs. Config must
        # fail loudly and name the fix (quote it: 'on':). Found in the
        # a retrofit: a hand-written adapter check never fired.
        adapter = default_adapter()
        adapter["checks"] = [{"id": "x", "cmd": "true", True: ["units/**"]}]
        write_adapter(self.root, adapter)
        with self.assertRaisesRegex(config.ConfigError, "quote"):
            config.load(self.root)

    def test_check_missing_on_rejected(self):
        # A check without 'on' globs can never match a file, a silently dead
        # check is worse than a missing one.
        adapter = default_adapter()
        adapter["checks"] = [{"id": "x", "cmd": "true"}]
        write_adapter(self.root, adapter)
        with self.assertRaisesRegex(config.ConfigError, "on"):
            config.load(self.root)

    def test_check_missing_cmd_rejected(self):
        adapter = default_adapter()
        adapter["checks"] = [{"id": "x", "on": ["units/**"]}]
        write_adapter(self.root, adapter)
        with self.assertRaisesRegex(config.ConfigError, "cmd"):
            config.load(self.root)

    def test_docs_lint_bad_type_rejected(self):
        adapter = default_adapter()
        adapter["docs_lint"] = {"extra_globs": "profile/**/*.md"}  # not a list
        write_adapter(self.root, adapter)
        with self.assertRaisesRegex(config.ConfigError, "docs_lint"):
            config.load(self.root)

    def test_wrong_schema_version_rejected(self):
        write_adapter(self.root, default_adapter(fh_schema=99))
        with self.assertRaisesRegex(config.ConfigError, "fh_schema"):
            config.load(self.root)

    def test_missing_adapter_file_rejected(self):
        (self.root / "harness/adapter/adapter.yaml").unlink()
        with self.assertRaises(config.ConfigError):
            config.load(self.root)


class TestPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))
        self.cfg = config.load(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_find_repo_root_walks_to_marker(self):
        nested = self.root / "units" / "deep"
        nested.mkdir(parents=True)
        self.assertEqual(paths.find_repo_root(nested), self.root)

    def test_active_roundtrip(self):
        self.assertIsNone(paths.read_active(self.root, self.cfg))
        paths.write_active(self.root, self.cfg, "u1", "S1")
        active = paths.read_active(self.root, self.cfg)
        self.assertEqual(active["slug"], "u1")
        self.assertEqual(active["session_id"], "S1")
        paths.clear_active(self.root, self.cfg)
        self.assertIsNone(paths.read_active(self.root, self.cfg))

    def test_markers(self):
        self.assertFalse(paths.marker_exists(self.root, "S1"))
        paths.touch_marker(self.root, "S1")
        self.assertTrue(paths.marker_exists(self.root, "S1"))
        paths.touch_marker(self.root, None)  # no session id -> no-op, no crash


class TestManifest(unittest.TestCase):
    GATES = ["composed", "checked", "verified"]
    HASHED = ["verified"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.unit = Path(self._tmp.name) / "u1"
        self.unit.mkdir()
        self.mpath = self.unit / "manifest.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def _new(self):
        return manifest.Manifest.new("u1", self.unit, self.GATES, self.HASHED)

    def test_roundtrip(self):
        m = self._new()
        m.save(self.mpath)
        m2 = manifest.Manifest.load(self.mpath, self.GATES, self.HASHED)
        self.assertEqual(m2.slug, "u1")
        self.assertEqual(m2.status, "in_progress")
        self.assertEqual(m2.next_gate(), "composed")
        self.assertFalse(m2.complete)

    def test_gate_order_enforced(self):
        m = self._new()
        with self.assertRaises(manifest.ManifestError):
            m.set_gate("checked")  # composed not passed yet
        m.set_gate("composed")
        m.set_gate("checked")
        self.assertEqual(m.next_gate(), "verified")

    def test_setting_earlier_gate_drops_later(self):
        m = self._new()
        m.set_gate("composed")
        m.set_gate("checked")
        m.set_gate("composed")  # re-earned -> checked must be re-earned too
        self.assertFalse(m.gate_passed("checked"))
        self.assertEqual(m.next_gate(), "checked")

    def test_hashed_staleness_reopens_gate(self):
        note = self.unit / "note.md"
        note.write_text("v1\n")
        m = self._new()
        m.set_gate("composed")
        m.set_gate("checked")
        m.set_gate("verified", sha256={"note.md": manifest.sha256_file(note)})
        self.assertTrue(m.complete)
        note.write_text("v2, edited after verification\n")
        self.assertFalse(m.gate_passed("verified"))
        self.assertFalse(m.complete)
        self.assertEqual(m.next_gate(), "verified")

    def test_hashed_gate_reopens_on_added_file(self):
        # F36: a file ADDED after the gate was earned is unexamined
        # evidence, it must reopen the gate exactly like a modification.
        note = self.unit / "note.md"
        note.write_text("v1\n")
        m = self._new()
        m.set_gate("composed")
        m.set_gate("checked")
        m.set_gate("verified", sha256={"note.md": manifest.sha256_file(note)})
        self.assertTrue(m.complete)
        (self.unit / "appendix.md").write_text("added after verification\n")
        self.assertFalse(m.gate_passed("verified"))
        self.assertFalse(m.complete)
        self.assertEqual(m.next_gate(), "verified")

    def test_declared_evidence_file_does_not_reopen_mid_flow(self):
        # A later gate's DECLARED evidence artifact (reviewer.evidence_file,
        # e.g. review.json) appearing MID-FLOW is bookkeeping, not content.
        # it must not reopen earlier gates; any other addition still does.
        note = self.unit / "note.md"
        note.write_text("v1\n")
        m = manifest.Manifest.new(
            "u1", self.unit, self.GATES, hashed_gates=["checked"],
            exclude=manifest.UNIT_EXCLUDE | {"review.json"})
        m.set_gate("composed")
        m.set_gate("checked", sha256={"note.md": manifest.sha256_file(note)})
        (self.unit / "review.json").write_text("{}\n")
        self.assertTrue(m.gate_passed("checked"),
                        "declared evidence file must not reopen a mid gate")
        (self.unit / "extra.md").write_text("still an unexamined addition\n")
        self.assertFalse(m.gate_passed("checked"))

    def test_final_gate_is_sealed_even_for_evidence_file(self):
        # Once the FINAL gate is earned the unit is sealed: the mid-flow
        # evidence-file exemption no longer applies, a file named like the
        # evidence artifact added post-completion reopens like any addition.
        note = self.unit / "note.md"
        note.write_text("v1\n")
        m = manifest.Manifest.new(
            "u1", self.unit, self.GATES, self.HASHED,
            exclude=manifest.UNIT_EXCLUDE | {"review.json"})
        m.set_gate("composed")
        m.set_gate("checked")
        m.set_gate("verified", sha256={"note.md": manifest.sha256_file(note)})
        self.assertTrue(m.complete)
        (self.unit / "review.json").write_text("smuggled after completion\n")
        self.assertFalse(m.gate_passed("verified"))
        self.assertFalse(m.complete)

    def test_unknown_gate_rejected(self):
        with self.assertRaises(manifest.ManifestError):
            self._new().set_gate("bogus")

    def test_corrupt_manifest_raises(self):
        self.mpath.write_text("{ not: [valid yaml\n")
        with self.assertRaises(manifest.ManifestError):
            manifest.Manifest.load(self.mpath, self.GATES, self.HASHED)

    def test_escalate(self):
        m = self._new()
        m.escalate("stuck: three failures on checked; needs a human decision")
        self.assertEqual(m.status, "escalated")
        self.assertIn("stuck", m.data["escalation"]["note"])


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))
        self.cfg = config.load(self.root)
        self.policies = policy.compile_policies(self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_any_scope_matches_everywhere(self):
        findings = policy.scan_text("id: " + ACCT_TOKEN + "\n", self.policies,
                                    "docs/random.md")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], "acct")
        self.assertEqual(findings[0][1], "fatal")

    def test_scoped_policy_only_fires_in_scope(self):
        text = "ADVISORY-MARKER present\n"
        inside = policy.scan_text(text, self.policies, "units/u1/note.md")
        outside = policy.scan_text(text, self.policies, "docs/other.md")
        self.assertEqual([f[0] for f in inside], ["todo-advisory"])
        self.assertEqual(inside[0][1], "warn")
        self.assertEqual(outside, [])

    def test_clean_text_no_findings(self):
        self.assertEqual(
            policy.scan_text("a clean line\n", self.policies, "units/u1/note.md"), [])

    def test_ask_reason_for_matching_path(self):
        self.assertIsNotNone(policy.ask_reason(self.cfg, "templates/base.tex"))
        self.assertIsNone(policy.ask_reason(self.cfg, "units/u1/note.md"))

    def test_protected_path_match(self):
        self.assertTrue(policy.is_protected(self.cfg, "units/u1/manifest.yaml"))
        self.assertTrue(policy.is_protected(self.cfg, "units/.active"))
        self.assertFalse(policy.is_protected(self.cfg, "units/u1/note.md"))


class TestPolicyNormalizedWholeText(unittest.TestCase):
    """F14 + Part 1 item 4: scanning must be NFKC-normalized and must catch
    matches that span a line break (the acct regex's own `\\s*` comment claims
    newline tolerance the line-based scanner never had)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))
        self.policies = policy.compile_policies(config.load(self.root))
        # Token halves assembled at runtime (house rule: no ACCT-shaped
        # literal at rest, even in tests).
        self.head, self.tail = ACCT_TOKEN[:6], ACCT_TOKEN[6:]

    def tearDown(self):
        self._tmp.cleanup()

    def _cpr(self, text):
        return [f for f in policy.scan_text(text, self.policies, "docs/x.md")
                if f[0] == "acct"]

    def test_token_split_across_lines_fires(self):
        found = self._cpr(f"id: {self.head}\n{self.tail}\n")
        self.assertEqual(len(found), 1, "cross-line match must fire (F14)")

    def test_cross_line_match_reports_first_line(self):
        found = self._cpr(f"a\nb\nid: {self.head}\n{self.tail}\n")
        self.assertEqual(found[0][2], 3, "finding must anchor to the match's first line")

    def test_zero_width_smuggle_fires(self):
        found = self._cpr(f"id: {self.head}\u200b{self.tail}\n")
        self.assertEqual(len(found), 1, "zero-width chars must be stripped before scan")

    def test_nfkc_homoglyph_fires(self):
        sup = str.maketrans("0123456789", "⁰¹²³⁴"
                                           "⁵⁶⁷⁸⁹")
        found = self._cpr(f"id: {ACCT_TOKEN.translate(sup)}\n")
        self.assertEqual(len(found), 1, "NFKC must fold superscript digits to ASCII")

    def test_single_line_token_yields_exactly_one_finding(self):
        # Pin: the whole-text pass must dedupe against the per-line pass.
        found = self._cpr(f"id: {ACCT_TOKEN}\n")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][2], 1)

    def test_whole_text_pass_respects_scope(self):
        # Pin: todo-advisory is scoped units/**, a whole-text pass must not
        # leak it into out-of-scope paths.
        found = policy.scan_text("ADVISORY-MARKER\n", self.policies, "docs/other.md")
        self.assertEqual(found, [])

    def test_clean_text_stays_clean(self):
        self.assertEqual(self._cpr("nothing to see\nhere either\n"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
