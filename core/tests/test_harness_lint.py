"""harness_lint tests: each seeded rot must fire its check in the right tier
(GATING fails the build; WARNING reports; ADVISORY only produces a worklist).
A clean fixture must pass the gating tier, no false positives by design."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402
from fixtures import age_file, default_adapter, mkroot, run_cli, write_adapter  # noqa: E402
from core import handoff  # noqa: E402


def lint(root, *args):
    return run_cli("harness_lint.py", *args, root=root)


def findings_by_check(stdout: str) -> dict:
    """Parse `id\ttier\tmessage` machine lines into {check_id: [messages]}."""
    out = {}
    for line in stdout.splitlines():
        if "\t" in line:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                out.setdefault(parts[0], []).append(parts[2])
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()


class TestCleanFixture(Base):
    def test_gating_tier_passes_clean(self):
        r = lint(self.root, "--tier", "gating")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_all_tiers_pass_clean(self):
        r = lint(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestGatingTier(Base):
    def assert_fires(self, check_id, mutate, needle=None):
        mutate()
        r = lint(self.root, "--tier", "gating", "--machine")
        self.assertEqual(r.returncode, 2, f"{check_id} did not gate:\n"
                                          f"{r.stdout}\n{r.stderr}")
        found = findings_by_check(r.stdout)
        self.assertIn(check_id, found, f"expected {check_id} in {found}")
        if needle:
            self.assertTrue(any(needle in m for m in found[check_id]),
                            f"{needle!r} not in {found[check_id]}")

    def test_doc_cmd_drift_missing_script(self):
        self.assert_fires("doc-cmd-drift", lambda: (
            (self.root / "CLAUDE.md").write_text(
                "run `python3 harness/core/ghost_tool.py` daily\n")),
            "ghost_tool")

    def test_doc_cmd_drift_missing_make_target(self):
        self.assert_fires("doc-cmd-drift", lambda: (
            (self.root / "CLAUDE.md").write_text("always run `make bogus-target`\n")),
            "bogus-target")

    def test_doc_cmd_drift_unknown_gate_name(self):
        self.assert_fires("doc-cmd-drift", lambda: (
            (self.root / "CLAUDE.md").write_text(
                "then `python3 harness/core/gate.py approve nonexistent_gate "
                "--unit {slug}`\n")),
            "nonexistent_gate")

    def test_doc_cmd_drift_ignores_make_in_prose(self):
        # "make a decision", "make it work", "make sense" are English, not
        # commands: only `make <target>` inside a code span or fenced block is
        # a command reference. Found on a retrofit, where craft
        # prose lit up 6 false gating findings.
        doc = self.root / "CLAUDE.md"
        doc.write_text(doc.read_text()
                       + "\nWe make a plan, make it work, and make sense of "
                         "state.\nDo not make one exception.\n")
        r = lint(self.root, "--tier", "gating", "--machine")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_doc_cmd_drift_fenced_make_still_fires(self):
        self.assert_fires("doc-cmd-drift", lambda: (
            (self.root / "CLAUDE.md").write_text(
                "build it:\n\n```bash\nmake fenced-ghost\n```\n")),
            "fenced-ghost")

    def test_docs_lint_extra_globs_resolve_wikilinks(self):
        # A repo may keep living docs outside the built-in glob set (e.g.
        # docs/notes/*.md). docs_lint.extra_globs pulls them into the doc
        # set so wikilinks to them resolve.
        adapter = default_adapter()
        adapter["docs_lint"] = {"extra_globs": ["notes/**/*.md"]}
        write_adapter(self.root, adapter)
        (self.root / "notes" / "knowledge").mkdir(parents=True)
        (self.root / "notes" / "knowledge" / "deep-thing.md").write_text("# x\n")
        (self.root / "MEMORY.md").write_text("see [[deep-thing]]\n")
        r = lint(self.root, "--tier", "gating", "--machine")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_docs_lint_exclude_globs_skip_historical_docs(self):
        # Planning artifacts (superseded specs/plans) may reference commands
        # that were never built; docs_lint.exclude_globs keeps them out of the
        # living-doc scan.
        adapter = default_adapter()
        adapter["docs_lint"] = {"exclude_globs": ["docs/plans/**"]}
        write_adapter(self.root, adapter)
        (self.root / "docs" / "plans").mkdir(parents=True)
        (self.root / "docs" / "plans" / "old-plan.md").write_text(
            "run `make ghost-target` and see [[no-such-doc]]\n")
        r = lint(self.root, "--tier", "gating", "--machine")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_orphan_refs_broken_md_link(self):
        self.assert_fires("orphan-refs", lambda: (
            (self.root / "CLAUDE.md").write_text("see [the guide](docs/nope.md)\n")),
            "docs/nope.md")

    def test_orphan_refs_broken_wikilink(self):
        self.assert_fires("orphan-refs", lambda: (
            (self.root / "MEMORY.md").write_text("see [[ghost-doc]]\n")),
            "ghost-doc")

    def test_orphan_refs_missing_rubric(self):
        self.assert_fires("orphan-refs", lambda: (
            (self.root / "harness/adapter/review_rubric.md").unlink()),
            "review_rubric")

    def test_orphan_refs_loop_enabled_without_oracle(self):
        def mutate():
            adapter = default_adapter()
            adapter["loop"]["enabled"] = True
            write_adapter(self.root, adapter)
        self.assert_fires("orphan-refs", mutate, "oracle")

    def test_dup_canon_tampered_gen_block(self):
        wf = self.root / ".claude/skills/fixtureproj/workflow.md"

        def mutate():
            wf.write_text(wf.read_text().replace("| 1 |", "| one |"))
        self.assert_fires("dup-canon", mutate, "gate-table")

    def test_dup_canon_gate_command_duplicated_outside_block(self):
        self.assert_fires("dup-canon", lambda: (
            (self.root / "CLAUDE.md").write_text(
                "next step: `python3 harness/core/gate.py approve composed "
                "--unit {slug}`\n")),
            "outside")

    def test_adapter_complete_missing_hook_registration(self):
        def mutate():
            settings = json.loads((self.root / ".claude/settings.json").read_text())
            del settings["hooks"]["Stop"]
            (self.root / ".claude/settings.json").write_text(json.dumps(settings))
        self.assert_fires("adapter-complete", mutate, "stop_gate")

    def test_adapter_complete_empty_handoff(self):
        self.assert_fires("adapter-complete", lambda: (
            (self.root / "HANDOFF.md").write_text("")),
            "HANDOFF")

    def test_adapter_complete_invalid_schema(self):
        def mutate():
            adapter = default_adapter()
            del adapter["workflow"]["gate_commands"]["checked"]
            write_adapter(self.root, adapter)
        self.assert_fires("adapter-complete", mutate)

    def test_instance_leak_slug_in_framework_file(self):
        def mutate():
            (self.root / "units" / "acme-widget-2027").mkdir()
            (self.root / ".claude/skills/fixtureproj/workflow.md").write_text(
                "notes on acme-widget-2027 go here\n")
        self.assert_fires("instance-leak", mutate, "acme-widget-2027")

    def test_instance_leak_token_pattern(self):
        def mutate():
            adapter = default_adapter(instance_token_patterns=[r"ACME-\d{4}"])
            write_adapter(self.root, adapter)
            (self.root / ".claude/agents/reviewer.md").write_text(
                "context: the ACME-9999 engagement\n")
        self.assert_fires("instance-leak", mutate, "ACME-9999")

    def test_committed_state_detected(self):
        def mutate():
            subprocess.run(["git", "init", "-q", str(self.root)], check=True)
            marker = self.root / "harness/state/touched-LEAK"
            marker.touch()
            subprocess.run(["git", "-C", str(self.root), "add", "-f",
                            "harness/state/touched-LEAK"], check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t",
                            "-c", "user.email=t@t", "commit", "-qm", "oops"],
                           check=True, capture_output=True)
        self.assert_fires("committed-state", mutate, "touched-LEAK")

    def test_rule_coverage_uncovered_rule(self):
        self.assert_fires("rule-coverage", lambda: (
            (self.root / "CLAUDE.md").write_text(
                "<!-- rule:no-secrets -->\n- Never write secrets to disk.\n")),
            "no-secrets")

    def test_rule_coverage_orphaned_enforces(self):
        def mutate():
            adapter = default_adapter()
            adapter["checks"] = [{"id": "x", "cmd": "true", "on": ["units/**"],
                                  "severity": "fatal", "enforces": "phantom-rule"}]
            write_adapter(self.root, adapter)
        self.assert_fires("rule-coverage", mutate, "phantom-rule")

    def test_rule_coverage_clean_when_linked(self):
        # the fixture deny policy enforces no-acct; annotate the rule -> clean
        (self.root / "CLAUDE.md").write_text(
            "<!-- rule:no-acct -->\n- Never write a account number to disk.\n")
        r = lint(self.root, "--tier", "gating")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestWarningTier(Base):
    def warnings(self):
        r = lint(self.root, "--tier", "warning", "--machine")
        return r, findings_by_check(r.stdout)

    def test_stale_state_warns_and_fix_deletes(self):
        old = self.root / "harness/state/touched-ANCIENT"
        old.touch()
        age_file(old, 30 * 86400)
        r, found = self.warnings()
        self.assertEqual(r.returncode, 1)
        self.assertIn("stale-state", found)
        r = lint(self.root, "--tier", "warning", "--fix")
        self.assertFalse(old.exists(), "--fix must delete stale state")

    def test_core_drift_warns_on_hash_mismatch(self):
        core_file = self.root / "harness/core/somefile.py"
        core_file.parent.mkdir(parents=True, exist_ok=True)
        core_file.write_text("edited = True\n")
        fh_dir = self.root / "harness/.fh"
        fh_dir.mkdir(parents=True, exist_ok=True)
        (fh_dir / "MANIFEST.json").write_text(json.dumps(
            {"version": "0.0.1",
             "files": {"harness/core/somefile.py": "0" * 64}}))
        r, found = self.warnings()
        self.assertEqual(r.returncode, 1)
        self.assertIn("core-drift", found)
        self.assertTrue(any("somefile" in m for m in found["core-drift"]))

    def test_handoff_decay_warns_on_two_stubs(self):
        from core import config
        cfg = config.load(self.root)
        handoff.write_fallback_stub(self.root, cfg)
        r, found = self.warnings()
        self.assertNotIn("handoff-decay", found, "one stub is not decay yet")
        handoff.write_fallback_stub(self.root, cfg)
        r, found = self.warnings()
        self.assertIn("handoff-decay", found)


class TestAdvisoryTier(Base):
    def test_honor_rules_audit_flags_unannotated_rule(self):
        (self.root / "CLAUDE.md").write_text(
            "You must never edit files under templates/ by hand.\n")
        r = lint(self.root, "--tier", "advisory", "--machine")
        self.assertEqual(r.returncode, 0, "advisory never gates")
        found = findings_by_check(r.stdout)
        self.assertIn("honor-rules-audit", found)

    def test_annotated_rules_not_in_advisory_worklist(self):
        (self.root / "CLAUDE.md").write_text(
            "<!-- rule:no-acct -->\n- You must never write a account number under "
            "units/ at all.\n")
        r = lint(self.root, "--tier", "advisory", "--machine")
        found = findings_by_check(r.stdout)
        self.assertNotIn("honor-rules-audit", found,
                         "annotated rules are rule-coverage's task")


if __name__ == "__main__":
    unittest.main(verbosity=2)
