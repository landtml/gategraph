"""Guard: decide(action, cfg) is the one policy decision (plan section 1.9; brief K6).

Covers the B09 and B10 fixes, protected, ask and blind paths, the fail-closed
rule when the configuration cannot be loaded, the built-in state-dir rule, and
the denial message.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import fixtures_v2 as fx
from core import brand
from core.kernel import guard
from core.kernel.config import load
from core.kernel.types import Action, ActionKind, Decision, Verdict


class FakeCfg:
    """The policy fields decide reads: deny, ask, protected_paths, blind, root."""

    def __init__(self, deny=(), ask=(), protected_paths=(), blind=(), root=None):
        self.deny = list(deny)
        self.ask = list(ask)
        self.protected_paths = list(protected_paths)
        self.blind = list(blind)
        self.root = root


def write(path, content="x"):
    return Action(kind=ActionKind.WRITE, tool="write_file", path=path, content=content)


def edit(path, content="x"):
    return Action(kind=ActionKind.EDIT, tool="edit_file", path=path, content=content)


def execute(command, path=None):
    return Action(kind=ActionKind.EXEC, tool="run_command", command=command, path=path)


def read(path):
    return Action(kind=ActionKind.READ, tool="read_file", path=path)


PIN_POLICY = {"id": "pin", "regex": r"PIN-\d{4}", "severity": "fatal", "summary": "a pin"}


class TestWarnAndFatalSeverity(unittest.TestCase):
    def test_B09_warn_policy_does_not_deny(self):
        cfg = FakeCfg(deny=[{"id": "soft", "regex": r"maybe", "severity": "warn",
                             "summary": "soft note"}])
        d = guard.decide(write("notes/a.md", "maybe later"), cfg)
        self.assertEqual(d.verdict, Verdict.ALLOW)
        self.assertEqual([f.rule for f in d.findings], ["soft"])
        self.assertEqual(d.findings[0].severity.value, "warn")

    def test_fatal_policy_denies_and_names_the_rule(self):
        cfg = FakeCfg(deny=[PIN_POLICY])
        d = guard.decide(write("notes/a.md", "PIN-1234"), cfg)
        self.assertEqual(d.verdict, Verdict.DENY)
        self.assertEqual(d.rule, "pin")

    def test_pretool_false_policy_is_not_checked_before_write(self):
        cfg = FakeCfg(deny=[dict(PIN_POLICY, pretool=False)])
        self.assertEqual(guard.decide(write("notes/a.md", "PIN-1234"), cfg).verdict,
                         Verdict.ALLOW)

    def test_policy_scope_limits_the_write_check(self):
        cfg = FakeCfg(deny=[dict(PIN_POLICY, scope=["docs/*.md"])])
        self.assertEqual(guard.decide(write("src/a.py", "PIN-1234"), cfg).verdict,
                         Verdict.ALLOW)
        self.assertEqual(guard.decide(write("docs/a.md", "PIN-1234"), cfg).verdict,
                         Verdict.DENY)


class TestNormalizationBeforeWrite(unittest.TestCase):
    def test_B10_fullwidth_digits_denied_before_write(self):
        cfg = FakeCfg(deny=[PIN_POLICY])
        fullwidth = "PIN-１２３４"  # fullwidth digits
        d = guard.decide(write("notes/a.md", fullwidth), cfg)
        self.assertEqual(d.verdict, Verdict.DENY)
        self.assertEqual(d.rule, "pin")

    def test_zero_width_characters_do_not_hide_a_match(self):
        cfg = FakeCfg(deny=[PIN_POLICY])
        d = guard.decide(edit("notes/a.md", "PIN​-1‍234"), cfg)
        self.assertEqual(d.verdict, Verdict.DENY)


class TestProtectedPaths(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeCfg(protected_paths=["tasks/locked.md", "gates/*.json"])

    def test_protected_path_denied_for_write(self):
        d = guard.decide(write("tasks/locked.md"), self.cfg)
        self.assertEqual((d.verdict, d.rule), (Verdict.DENY, "protected-path"))

    def test_protected_path_denied_for_edit(self):
        d = guard.decide(edit("gates/g1.json"), self.cfg)
        self.assertEqual(d.verdict, Verdict.DENY)

    def test_protected_path_denied_through_dot_segments(self):
        self.assertEqual(guard.decide(write("docs/../tasks/locked.md"), self.cfg).verdict,
                         Verdict.DENY)
        self.assertEqual(guard.decide(write("./tasks/locked.md"), self.cfg).verdict,
                         Verdict.DENY)

    def test_absolute_path_inside_the_root_is_denied(self):
        cfg = FakeCfg(protected_paths=["tasks/locked.md"], root=Path("/repo"))
        self.assertEqual(guard.decide(write("/repo/tasks/locked.md"), cfg).verdict,
                         Verdict.DENY)

    def test_exec_naming_a_protected_path_is_denied(self):
        d = guard.decide(execute("sed -i 's/a/b/' tasks/locked.md"), self.cfg)
        self.assertEqual((d.verdict, d.rule), (Verdict.DENY, "protected-path"))
        d = guard.decide(execute("cp x.md gates/g1.json"), self.cfg)
        self.assertEqual(d.verdict, Verdict.DENY)

    def test_exec_not_naming_a_protected_path_is_allowed(self):
        self.assertEqual(guard.decide(execute("cat tasks/open.md"), self.cfg).verdict,
                         Verdict.ALLOW)

    def test_unprotected_write_is_allowed(self):
        self.assertEqual(guard.decide(write("tasks/open.md"), self.cfg).verdict,
                         Verdict.ALLOW)


class TestAskPaths(unittest.TestCase):
    def test_ask_path_returns_ask_with_its_reason(self):
        cfg = FakeCfg(ask=[{"paths": ["docs/**"], "reason": "docs need sign-off"}])
        d = guard.decide(write("docs/guide/intro.md"), cfg)
        self.assertEqual(d.verdict, Verdict.ASK)
        self.assertIn("docs need sign-off", d.reason)

    def test_deny_outranks_ask(self):
        cfg = FakeCfg(deny=[PIN_POLICY],
                      ask=[{"paths": ["docs/**"], "reason": "docs need sign-off"}])
        self.assertEqual(guard.decide(write("docs/a.md", "PIN-1234"), cfg).verdict,
                         Verdict.DENY)


class TestBlindPaths(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeCfg(blind=["oracle/**"])

    def test_blind_path_denied_for_read(self):
        d = guard.decide(read("oracle/gold/case1.json"), self.cfg)
        self.assertEqual((d.verdict, d.rule), (Verdict.DENY, "blind-path"))

    def test_blind_path_denied_for_exec_that_names_it(self):
        self.assertEqual(guard.decide(execute("cat oracle/gold/case1.json"), self.cfg).verdict,
                         Verdict.DENY)

    def test_other_reads_are_allowed(self):
        self.assertEqual(guard.decide(read("docs/readme.md"), self.cfg).verdict, Verdict.ALLOW)


class TestUnloadableConfiguration(unittest.TestCase):
    def test_no_configuration_denies_every_action_but_adapter_writes(self):
        self.assertEqual(guard.decide(write("src/a.py"), None).verdict, Verdict.DENY)
        self.assertEqual(guard.decide(edit("src/a.py"), None).verdict, Verdict.DENY)
        self.assertEqual(guard.decide(read("src/a.py"), None).verdict, Verdict.DENY)
        self.assertEqual(guard.decide(execute("ls"), None).verdict, Verdict.DENY)
        self.assertEqual(guard.decide(Action(kind=ActionKind.NET, tool="fetch_url",
                                             url="https://example.invalid/"), None).verdict,
                         Verdict.DENY)

    def test_writes_under_the_adapter_directory_are_allowed(self):
        self.assertEqual(guard.decide(write("harness/adapter/adapter.yaml"), None).verdict,
                         Verdict.ALLOW)
        self.assertEqual(guard.decide(edit("harness/adapter/adapter.yaml"), None).verdict,
                         Verdict.ALLOW)

    def test_adapter_directory_is_not_reached_through_dot_dot(self):
        self.assertEqual(guard.decide(write("harness/adapter/../../src/a.py"), None).verdict,
                         Verdict.DENY)

    def test_exec_and_read_in_the_adapter_directory_are_denied(self):
        self.assertEqual(guard.decide(read("harness/adapter/adapter.yaml"), None).verdict,
                         Verdict.DENY)
        self.assertEqual(guard.decide(execute("cat harness/adapter/adapter.yaml"), None).verdict,
                         Verdict.DENY)

    def test_configuration_without_policy_fields_is_unloadable(self):
        real_shape = SimpleNamespace(root=Path("/repo"), project="p", gates=())
        self.assertEqual(guard.decide(write("src/a.py"), real_shape).verdict, Verdict.DENY)
        self.assertEqual(guard.decide(write("harness/adapter/adapter.yaml"), real_shape).verdict,
                         Verdict.ALLOW)

    def test_uncompilable_policy_is_unloadable(self):
        cfg = FakeCfg(deny=[{"id": "bad", "regex": "(", "severity": "fatal", "summary": "s"}])
        self.assertEqual(guard.decide(write("src/a.py", "x"), cfg).verdict, Verdict.DENY)

    def test_unknown_severity_is_unloadable(self):
        cfg = FakeCfg(deny=[{"id": "bad", "regex": "x", "severity": "error", "summary": "s"}])
        self.assertEqual(guard.decide(write("src/a.py", "x"), cfg).verdict, Verdict.DENY)


class TestStateDirectoryRule(unittest.TestCase):
    def names(self):
        return [brand.STATE_DIRNAME, *brand.LEGACY_STATE_DIRNAMES]

    def test_writes_edits_and_exec_naming_the_state_directory_are_denied(self):
        cfg = FakeCfg()  # the adapter protects nothing
        for name in self.names():
            with self.subTest(name=name):
                d = guard.decide(write(f"{name}/state.db"), cfg)
                self.assertEqual((d.verdict, d.rule), (Verdict.DENY, "state-dir"))
                self.assertEqual(guard.decide(edit(f"sub/{name}/x.json"), cfg).rule, "state-dir")
                self.assertEqual(guard.decide(execute(f"rm -rf {name}"), cfg).rule, "state-dir")

    def test_state_directory_rule_holds_when_the_configuration_is_unloadable(self):
        d = guard.decide(write(f"{brand.STATE_DIRNAME}/state.db"), None)
        self.assertEqual((d.verdict, d.rule), (Verdict.DENY, "state-dir"))

    def test_adapter_ask_cannot_open_the_state_directory(self):
        cfg = FakeCfg(ask=[{"paths": ["**"], "reason": "anything"}])
        d = guard.decide(write(f"{brand.STATE_DIRNAME}/state.db"), cfg)
        self.assertEqual((d.verdict, d.rule), (Verdict.DENY, "state-dir"))

    def test_state_directory_is_named_through_case_fullwidth_quotes_and_zero_width(self):
        cfg = FakeCfg()
        name = brand.STATE_DIRNAME
        self.assertEqual(guard.decide(write(name.upper() + "/x"), cfg).rule, "state-dir")
        self.assertEqual(guard.decide(write("．" + name[1:] + "/x"), cfg).rule, "state-dir")
        self.assertEqual(guard.decide(execute(f'cat ".{name[1:]}"'), cfg).rule, "state-dir")
        self.assertEqual(guard.decide(execute(f"ls .{name[1:3]}​{name[3:]}"), cfg).rule,
                         "state-dir")

    def test_names_that_only_contain_the_state_directory_name_are_not_denied(self):
        cfg = FakeCfg()
        self.assertEqual(guard.decide(write(f"docs/notes-{brand.NAME}.md"), cfg).verdict,
                         Verdict.ALLOW)
        for name in self.names():
            with self.subTest(name=name):
                self.assertEqual(guard.decide(write(f"docs/x{name}y.md"), cfg).verdict,
                                 Verdict.ALLOW)

    def test_reading_the_state_directory_is_not_denied_by_this_rule(self):
        self.assertEqual(guard.decide(read(f"{brand.STATE_DIRNAME}/state.db"), FakeCfg()).verdict,
                         Verdict.ALLOW)


class TestRealLoadedConfiguration(unittest.TestCase):
    """The guard run on a Config produced by config.load, not on a fake."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = fx.make_project(tmp.name, adapter="five_gate")
        self.cfg = load(self.root)
        self.unit = fx.unit_path(self.root).relative_to(self.root).as_posix()

    def test_guard_with_real_loaded_config(self):
        ordinary = guard.decide(write(f"{self.unit}/sketch.md", "A plain sentence."), self.cfg)
        self.assertEqual(ordinary.verdict, Verdict.ALLOW)
        leaked = guard.decide(write(f"{self.unit}/notes.md", "ACCT 010203-1234"), self.cfg)
        self.assertEqual((leaked.verdict, leaked.rule), (Verdict.DENY, "acct"))
        protected = guard.decide(write(f"dossiers/{self.unit.split('/')[-1]}/manifest.yaml"),
                                 self.cfg)
        self.assertEqual((protected.verdict, protected.rule), (Verdict.DENY, "protected-path"))

    def test_adapter_without_policy_block_allows_an_ordinary_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load(fx.make_project(tmp, adapter="minimal"))
            d = guard.decide(write("notes/unit-001/sketch.md", "A plain sentence."), cfg)
        self.assertEqual(d.verdict, Verdict.ALLOW)


class TestDenialMessage(unittest.TestCase):
    def test_denial_message_has_rule_reason_remedy_and_policy_sentence(self):
        cfg = FakeCfg(protected_paths=["tasks/locked.md"])
        d = guard.decide(write("tasks/locked.md"), cfg)
        msg = guard.denial_message(d)
        self.assertIn("protected-path", msg)
        self.assertIn(d.reason, msg)
        self.assertIn(d.remedy, msg)
        self.assertIn("policy outranks the task", msg.lower())

    def test_denial_message_for_a_built_decision(self):
        d = Decision(verdict=Verdict.DENY, rule="state-dir", reason="names the state directory",
                     remedy="Use the harness commands.", findings=())
        msg = guard.denial_message(d)
        for part in ("state-dir", "names the state directory", "Use the harness commands.",
                     guard.POLICY_SENTENCE):
            self.assertIn(part, msg)


if __name__ == "__main__":
    unittest.main()
