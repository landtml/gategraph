"""Self-lint tiers, ported from the legacy core/tests/test_harness_lint.py (X1 B5b).

Each seeded rot must fire its check in the right tier: GATING fails the build, WARNING
reports, ADVISORY is a worklist and never gates. A clean project must pass the gating tier.
The legacy names are kept so a failing test points at the old one.

Kept as deleted (not ported), because the kernel config or selflint no longer has the
behaviour: docs_lint globs (schema 2 drops docs_lint), instance_token_patterns (dropped),
loop-enabled-without-oracle (loop retired), the rubric file check, the rule-coverage checks
and the core-drift check (no kernel check implements them), and the annotated-rule skip of
honor-rules-audit (it went with rule-coverage). Coverage gap, see the report.

Run alone with: python3 core/tests/kernel/test_selflint_tiers.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from core import brand
from core.kernel import config, selflint
from fixtures_v2 import make_project

GEN_OPEN = f"<!-- {brand.GEN_MARKER} gate-table -->"
GEN_CLOSE = f"<!-- /{brand.GEN_MARKER} -->"


def _write_settings(root):
    """A stamped .claude/settings.json: the four shims, with the Bash relint wired."""
    shims = ("pretool_guard.py", "posttool_lint.py", "stop_gate.py", "session_start.py")
    commands = [f"python3 \"$CLAUDE_PROJECT_DIR/harness/core/hosts/shims/{s}\"" for s in shims]
    commands.append("python3 \"$CLAUDE_PROJECT_DIR/harness/core/hosts/shims/posttool_lint.py\" "
                    "--bash-relint")
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"hooks": [{"command": c} for c in commands]}}), encoding="utf-8")


def clean_root(tmp):
    root = make_project(Path(tmp) / "p", "minimal")
    (root / "harness" / "adapter" / "checks").mkdir(parents=True, exist_ok=True)
    (root / "harness" / "adapter" / "checks" / "lint.py").write_text("# check\n")
    (root / "CLAUDE.md").write_text(
        "# fixture\n\n<!-- rule:no-acct -->\n- Never write a account number to disk.\n")
    (root / "MEMORY.md").write_text("# MEMORY: router\n")
    (root / "HANDOFF.md").write_text(
        "# HANDOFF\n\n## Now\n\nfresh fixture\n\n## Next\n\nrun make check\n")
    (root / "Makefile").write_text("check:\n\tpython3 harness/core/harness_lint.py --tier gating\n")
    _write_settings(root)
    return root


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = clean_root(self._tmp.name)
        self.cfg = config.load(self.root)

    def run_tier(self, tier, fix=False):
        return selflint.run_lint(self.root, self.cfg, tier=tier, fix=fix)

    def assert_fires(self, check_id, mutate, needle=None, tier="gating"):
        mutate()
        findings = [f for f in self.run_tier(tier) if f.check == check_id]
        self.assertTrue(findings, f"{check_id} did not fire in the {tier} tier")
        self.assertTrue(all(f.tier == tier for f in findings))
        if needle:
            self.assertTrue(any(needle in f.message for f in findings),
                            f"{needle!r} not in {[f.message for f in findings]}")


class TestCleanFixture(Base):
    def test_gating_tier_passes_clean(self):
        self.assertEqual(self.run_tier("gating"), [])

    def test_all_tiers_pass_clean(self):
        self.assertEqual(self.run_tier("all"), [])


class TestGatingTier(Base):
    def test_doc_cmd_drift_missing_script(self):
        self.assert_fires("doc-cmd-drift", lambda: (
            (self.root / "CLAUDE.md").write_text(
                "run `python3 harness/core/ghost_tool.py` daily\n")),
            "ghost_tool")

    def test_doc_cmd_drift_missing_make_target(self):
        self.assert_fires("doc-cmd-drift", lambda: (
            (self.root / "CLAUDE.md").write_text("always run `make bogus-target`\n")),
            "bogus-target")

    def test_doc_cmd_drift_ignores_make_in_prose(self):
        # "make a decision" is English, not a command: only `make <target>` in a code span
        # or a fenced block counts (found on a retrofit: prose lit up 6 false findings).
        doc = self.root / "CLAUDE.md"
        doc.write_text(doc.read_text()
                       + "\nWe make a plan, make it work, and make sense of state.\n"
                         "Do not make one exception.\n")
        self.assertEqual([f for f in self.run_tier("gating") if f.check == "doc-cmd-drift"], [])

    def test_doc_cmd_drift_fenced_make_still_fires(self):
        self.assert_fires("doc-cmd-drift", lambda: (
            (self.root / "CLAUDE.md").write_text(
                "build it:\n\n```bash\nmake fenced-ghost\n```\n")),
            "fenced-ghost")

    def test_orphan_refs_broken_md_link(self):
        self.assert_fires("orphan-refs", lambda: (
            (self.root / "CLAUDE.md").write_text("see [the guide](docs/nope.md)\n")),
            "docs/nope.md")

    def test_orphan_refs_broken_wikilink(self):
        self.assert_fires("orphan-refs", lambda: (
            (self.root / "MEMORY.md").write_text("see [[ghost-doc]]\n")),
            "ghost-doc")

    def test_dup_canon_tampered_gen_block(self):
        def mutate():
            (self.root / "docs").mkdir(exist_ok=True)
            (self.root / "docs" / "workflow.md").write_text(
                f"{GEN_OPEN}\n| 1 | tampered |\n{GEN_CLOSE}\n")
        self.assert_fires("dup-canon", mutate, "gate-table")

    def test_dup_canon_check_command_duplicated_outside_block(self):
        # The command is read from the adapter, so the test follows the fixture's placeholders.
        command = self.cfg.check("lint").command
        self.assert_fires("dup-canon", lambda: (
            (self.root / "CLAUDE.md").write_text(f"next step: `{command}`\n")),
            "outside")

    def test_adapter_complete_invalid_schema(self):
        def mutate():
            adapter = yaml.safe_load((self.root / "harness/adapter/adapter.yaml").read_text())
            adapter["schema"] = 3
            (self.root / "harness/adapter/adapter.yaml").write_text(yaml.safe_dump(adapter))
        mutate()
        findings = selflint.run_lint(self.root, tier="gating")  # cfg loaded by selflint
        self.assertTrue(any(f.check == "adapter-complete" and f.tier == "gating"
                            for f in findings), [f.message for f in findings])

    def test_instance_leak_slug_in_framework_file(self):
        def mutate():
            (self.root / "notes" / "acme-widget-2027").mkdir(parents=True)
            (self.root / "notes" / "acme-widget-2027" / "manifest.yaml").write_text("x: 1\n")
            (self.root / "docs").mkdir(exist_ok=True)
            (self.root / "docs" / "notes.md").write_text("notes on acme-widget-2027 go here\n")
        self.assert_fires("instance-leak", mutate, "acme-widget-2027")

    def test_committed_state_detected(self):
        def mutate():
            subprocess.run(["git", "init", "-q", str(self.root)], check=True)
            (self.root / brand.STATE_DIRNAME).mkdir(exist_ok=True)
            (self.root / brand.STATE_DIRNAME / "touched-LEAK").touch()
            git = ["git", "-C", str(self.root)]
            subprocess.run([*git, "add", "-f", f"{brand.STATE_DIRNAME}/touched-LEAK"],
                           check=True, capture_output=True)
            subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
                            "-qm", "oops"], check=True, capture_output=True)
        self.assert_fires("committed-state", mutate, "touched-LEAK")

    def test_hook_registration_missing_stop_is_adapter_complete(self):
        # The three hook-registration tests live in core/tests/hosts/test_registration.py;
        # this one proves selflint's gating tier calls check_registration.
        (self.root / ".claude" / "settings.json").write_text('{"hooks": {}}')
        findings = self.run_tier("gating")
        self.assertTrue(any(f.check == "adapter-complete" and "stop_gate.py" in f.message
                            for f in findings), [f.message for f in findings])


class TestWarningTier(Base):
    def test_stale_state_warns_and_fix_deletes(self):
        old = self.root / "stale.pid"
        old.touch()
        past = time.time() - 30 * 86400
        os.utime(old, (past, past))
        found = [f for f in self.run_tier("warning") if f.check == "stale-state"]
        self.assertTrue(found, "a 30-day-old pid file must be stale")
        self.assertTrue(old.exists(), "a warning never deletes by itself")
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_tier("warning", fix=True)
        self.assertFalse(old.exists(), "--fix must delete stale state")


class TestAdvisoryTier(Base):
    def test_honor_rules_audit_flags_unannotated_rule(self):
        (self.root / "CLAUDE.md").write_text(
            "You must never edit files under templates/ by hand.\n")
        found = [f for f in self.run_tier("advisory") if f.check == "honor-rules-audit"]
        self.assertTrue(found)
        self.assertTrue(all(f.tier == "advisory" for f in found))

    def test_advisory_never_gates(self):
        (self.root / "CLAUDE.md").write_text(
            "You must never edit files under templates/ by hand.\n")
        self.assertEqual(
            [f for f in self.run_tier("gating") if f.tier == "advisory"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
