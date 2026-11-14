"""Host bridge (brief H1): replay of the recorded events against expected_legacy/,
a second host registered in the test, and the fail-open / fail-closed rules.

expected_legacy/*.json were recorded by running each recorded event through the
legacy hook that settings.json registers for it, with CLAUDE_PROJECT_DIR set to a
fixture root built on expected_legacy/fixture_adapter.yaml. The event's
/work/sample-repo paths were replaced by that root; "<ROOT>" in the saved text
stands for it. The legacy hooks are deleted at cutover; these files are the truth.

Run alone with: python3 core/tests/hosts/test_bridge.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import inspect
import json
import os
import subprocess
import tempfile
import unittest

import yaml

from core.hosts import bridge, claude_code, generic
from core.hosts.dispatch import handle  # noqa: F401  (the kernel decision, used directly)
from core.kernel import config

HERE = pathlib.Path(__file__).resolve().parent
EVENTS = HERE / "events"
EXPECTED = HERE / "expected_legacy"
REPO = _bootstrap.REPO
SHIMS = REPO / "core" / "hosts" / "shims"
SAMPLE_ROOT = "/work/sample-repo"
KIND = {"PreToolUse": "pre_tool", "PostToolUse": "post_tool",
        "Stop": "stop", "SessionStart": "session_start"}
SHIM_FOR = {"pre_tool": SHIMS / "pretool_guard.py", "post_tool": SHIMS / "posttool_lint.py"}
ACCT = "010190" + "-1234"  # synthetic, assembled at run time


def make_root(tmp, deny=None, ask=None):
    """A project root on the recorded fixture adapter (schema 1, as the legacy replies used)."""
    adapter = yaml.safe_load((EXPECTED / "fixture_adapter.yaml").read_text(encoding="utf-8"))
    if deny is not None:
        adapter["policies"]["deny"] = deny
    if ask is not None:
        adapter["policies"]["ask"] = ask
    root = pathlib.Path(tmp)
    (root / "harness" / "adapter").mkdir(parents=True)
    (root / "harness" / "adapter" / "adapter.yaml").write_text(
        yaml.safe_dump(adapter, sort_keys=False), encoding="utf-8")
    (root / "units").mkdir()
    return root


def replay_text(name, root):
    return (EVENTS / name).read_text(encoding="utf-8").replace(SAMPLE_ROOT, str(root))


def intentional_differences():
    data = yaml.safe_load((HERE / "intentional_differences.yaml").read_text(encoding="utf-8"))
    return data["differences"]


class FlatHost:
    """The 20-line second host the test registers: a flat-field payload, same kernel."""
    name = "flat"
    KINDS = {"before": "pre_tool", "after": "post_tool", "end": "stop", "begin": "session_start"}

    def parse(self, stdin_text):
        data = json.loads(stdin_text)
        kind = self.KINDS[data["phase"]]
        tool = data.get("tool") if kind in ("pre_tool", "post_tool") else None
        action = generic.tool_action(tool, data.get("args") or {}) if tool else None
        return bridge.HostEvent(kind=kind, session=data.get("sid"), tool=tool,
                                action=action, raw=data)

    def render(self, reply, kind):
        return json.dumps(generic.decision_fields(reply)), (2 if reply.block else 0)


class TestReplay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_root(self._tmp.name)

    def replay(self, name):
        text = replay_text(name, self.root)
        kind = KIND[json.loads(text)["hook_event_name"]]
        return claude_code.run(kind, text, root=self.root)

    def test_every_recorded_event_has_a_legacy_reply(self):
        events = sorted(p.name for p in EVENTS.glob("*.json"))
        self.assertTrue(events)
        self.assertEqual(events, sorted(p.name for p in EXPECTED.glob("*.json")
                                        if p.name != "fixture_adapter.yaml"))

    def test_every_recorded_event_matches_legacy(self):
        exempt = {d["event"] for d in intentional_differences() if d.get("event")}
        mismatches = []
        for path in sorted(EVENTS.glob("*.json")):
            legacy = json.loads((EXPECTED / path.name).read_text(encoding="utf-8"))
            got_out, got_err, got_code = self.replay(path.name)
            want = (legacy["stdout"].replace("<ROOT>", str(self.root)),
                    legacy["stderr"].replace("<ROOT>", str(self.root)), legacy["exit"])
            if (got_out, got_err, got_code) != want and path.name not in exempt:
                mismatches.append((path.name, (got_out, got_err, got_code), want))
        self.assertEqual(mismatches, [])

    def test_shims_reproduce_legacy_for_pre_and_post_events(self):
        for path in sorted(EVENTS.glob("*.json")):
            text = replay_text(path.name, self.root)
            kind = KIND[json.loads(text)["hook_event_name"]]
            if kind not in SHIM_FOR:
                continue
            legacy = json.loads((EXPECTED / path.name).read_text(encoding="utf-8"))
            proc = subprocess.run([sys.executable, str(SHIM_FOR[kind])], input=text,
                                  capture_output=True, text=True,
                                  env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)})
            self.assertEqual(proc.stdout, legacy["stdout"].replace("<ROOT>", str(self.root)),
                             path.name)
            self.assertEqual(proc.returncode, legacy["exit"], path.name)

    def test_reply_carries_nested_and_flat_decision_keys(self):
        text = json.dumps({"session_id": "S1", "hook_event_name": "PreToolUse",
                           "tool_name": "Write",
                           "tool_input": {"file_path": str(self.root / "units/unit-001/manifest.yaml"),
                                          "content": "x\n"}})
        out, _, code = claude_code.run("pre_tool", text, root=self.root)
        self.assertEqual(code, 0)
        data = json.loads(out)
        nested = data["hookSpecificOutput"]
        self.assertEqual(nested["permissionDecision"], "deny")
        self.assertEqual(data["permissionDecision"], "deny")
        self.assertEqual(data["permissionDecisionReason"], nested["permissionDecisionReason"])


class TestSecondHost(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_root(self._tmp.name)
        bridge.HOSTS["flat"] = FlatHost()
        self.addCleanup(bridge.HOSTS.pop, "flat", None)

    def test_translator_is_twenty_lines_or_fewer(self):
        self.assertLessEqual(len(inspect.getsource(FlatHost).splitlines()), 20)

    def test_registered_host_reaches_the_same_kernel_decisions(self):
        host = bridge.HOSTS["flat"]
        cfg = config.load(self.root)
        cases = [
            ({"file_path": str(self.root / "notes/todo.md"), "content": "hello\n"}, "allow"),
            ({"file_path": str(self.root / "units/unit-001/manifest.yaml"), "content": "x\n"}, "deny"),
        ]
        for tool_input, want in cases:
            payload = {"phase": "before", "sid": "S1", "tool": "Write", "args": tool_input}
            event = host.parse(json.dumps(payload))
            reply = handle(event, self.root, cfg)
            text, code = host.render(reply, event.kind)
            self.assertEqual(json.loads(text)["decision"], want)
            self.assertEqual(code, 0)
            # the claude_code host reaches the same verdict for the same action
            native = claude_code.run("pre_tool", json.dumps({
                "session_id": "S1", "hook_event_name": "PreToolUse", "tool_name": "Write",
                "tool_input": tool_input}), root=self.root)
            verdict = "allow" if native[0] == "" else json.loads(native[0])["hookSpecificOutput"]["permissionDecision"]
            self.assertEqual(verdict, want)

    def test_registry_holds_both_hosts(self):
        self.assertEqual(set(bridge.HOSTS), {"claude_code", "flat"})


class TestFailOpenAndClosed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_root(self._tmp.name)

    def test_unparseable_pre_tool_fails_closed(self):
        out, _, code = claude_code.run("pre_tool", "{not json", root=self.root)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unparseable_post_tool_fails_open(self):
        out, _, code = claude_code.run("post_tool", "{not json", root=self.root)
        self.assertEqual((out, code), ("", 0))

    def test_shims_obey_the_same_rules_over_stdin(self):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)}
        pre = subprocess.run([sys.executable, str(SHIM_FOR["pre_tool"])], input="{not json",
                             capture_output=True, text=True, env=env)
        post = subprocess.run([sys.executable, str(SHIM_FOR["post_tool"])], input="{not json",
                              capture_output=True, text=True, env=env)
        self.assertEqual(pre.returncode, 0, pre.stderr)
        self.assertEqual(json.loads(pre.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(post.returncode, 0, post.stderr)

    def test_shims_are_at_most_ten_lines(self):
        for shim in SHIM_FOR.values():
            self.assertLessEqual(len(shim.read_text(encoding="utf-8").splitlines()), 10, shim)


class TestDifferencesRegister(unittest.TestCase):
    def test_each_difference_names_a_bug_and_an_existing_test(self):
        source = (HERE / "test_bridge.py").read_text(encoding="utf-8") + \
                 (HERE / "test_claude_code.py").read_text(encoding="utf-8")
        entries = intentional_differences()
        self.assertTrue(entries)
        for entry in entries:
            self.assertIn(entry["bug"], {"B05", "B09", "B10", "B31", "B35", "K6"})
            self.assertIn(f"def {entry['test']}(", source, entry["bug"])
            if entry.get("event"):
                self.assertTrue((EVENTS / entry["event"]).is_file(), entry["bug"])


if __name__ == "__main__":
    unittest.main()
