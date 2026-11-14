"""MCP tools (plan section 1.10, brief A4, Q8).

The tests use only the local stdio echo server in this directory. No network.
Runs with .venv/bin/python (it needs langchain[mcp]).
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import os
import socket
import tempfile
import unittest
from unittest import mock

from core.kernel.types import Action, ActionKind, Budget
from core.tools import spec as S
from core.tools import mcp

SERVER = str(_bootstrap.REPO / "core" / "tests" / "tools" / "mcp_echo_server.py")
ECHO_CFG = {
    "command": sys.executable,
    "args": [SERVER],
    "timeout_s": 60,
    "tools": {"echo": {"effects": ["read"], "idempotent": True}},
}


def _cfg(server=None, **extra):
    return {"servers": {"echo": dict(server or ECHO_CFG), **extra}}


class _Journal:
    def __init__(self):
        self.store = {}

    def seen(self, key):
        return self.store.get(key)

    def record(self, key, result):
        self.store[key] = result


class _Attempts:
    """Records every socket connect attempt made in this process."""

    def __init__(self):
        self.attempts = []


class McpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve()
        self.ctx = S.ToolContext(
            root=self.root, unit_dir=self.root, scratch_dir=self.root,
            writable=(), blind=(), budget=Budget(), journal=_Journal(),
        )
        self.advisories = []

    def load(self, cfg):
        return mcp.load_mcp_tools(cfg, on_advisory=self.advisories.append)

    def by_name(self, specs):
        return {s.name: s for s in specs}

    # -- listing and wrapping ------------------------------------------------

    def test_tools_are_listed_and_wrapped_as_tool_specs(self):
        specs = self.load(_cfg())
        self.assertEqual(self.advisories, [])
        names = sorted(s.name for s in specs)
        self.assertEqual(names, ["mcp__echo__echo", "mcp__echo__fail",
                                 "mcp__echo__report_env"])
        for s in specs:
            self.assertIsInstance(s, S.ToolSpec)
        echo = self.by_name(specs)["mcp__echo__echo"]
        self.assertEqual(echo.effects, frozenset({"read"}))
        self.assertFalse(echo.needs_approval)
        self.assertTrue(echo.idempotent)
        self.assertIn("text", echo.args_schema.get("properties", {}))
        self.assertEqual(echo.description, "Echo the text back.")

    def test_declared_tool_action_matches_its_effect(self):
        echo = self.by_name(self.load(_cfg()))["mcp__echo__echo"]
        action = echo.action_for({"text": "hi"})
        self.assertIsInstance(action, Action)
        self.assertEqual(action.kind, ActionKind.READ)
        self.assertEqual(action.tool, "mcp__echo__echo")

    def test_call_runs_the_server_tool(self):
        echo = self.by_name(self.load(_cfg()))["mcp__echo__echo"]
        result = echo.run({"text": "hi"}, self.ctx)
        self.assertIsInstance(result, S.ToolResult)
        self.assertEqual(result.content, "echo:hi")
        self.assertEqual(result.touched, ())

    def test_server_reported_error_raises_in_run(self):
        fail = self.by_name(self.load(_cfg()))["mcp__echo__fail"]
        with self.assertRaises(mcp.McpToolError) as caught:
            fail.run({"text": "x"}, self.ctx)
        self.assertIn("refused x", str(caught.exception))

    # -- undeclared effects --------------------------------------------------

    def test_undeclared_tool_gets_write_exec_net_and_needs_approval(self):
        env = self.by_name(self.load(_cfg()))["mcp__echo__report_env"]
        self.assertEqual(env.effects, frozenset({"write", "exec", "net"}))
        self.assertTrue(env.needs_approval)
        self.assertFalse(env.idempotent)
        # The guard sees an action whose kind is one of the declared effects.
        action = env.action_for({"name": "X"})
        self.assertIn(action.kind, (ActionKind.EXEC, ActionKind.NET, ActionKind.WRITE))

    def test_declared_tool_with_no_effects_key_is_treated_as_undeclared(self):
        server = dict(ECHO_CFG, tools={"echo": {"idempotent": True}})
        echo = self.by_name(self.load(_cfg(server)))["mcp__echo__echo"]
        self.assertEqual(echo.effects, frozenset({"write", "exec", "net"}))
        self.assertTrue(echo.needs_approval)

    def test_empty_effects_list_is_treated_as_undeclared(self):
        server = dict(ECHO_CFG, tools={"echo": {"effects": []}})
        echo = self.by_name(self.load(_cfg(server)))["mcp__echo__echo"]
        self.assertEqual(echo.effects, frozenset({"write", "exec", "net"}))
        self.assertTrue(echo.needs_approval)

    def test_unknown_effect_in_config_is_an_advisory_and_the_tool_is_skipped(self):
        server = dict(ECHO_CFG, tools={"echo": {"effects": ["fly"]}})
        names = [s.name for s in self.load(_cfg(server))]
        self.assertNotIn("mcp__echo__echo", names)
        self.assertTrue(any(a.id == "mcp:echo:echo" for a in self.advisories))

    # -- failure to start ----------------------------------------------------

    def test_server_that_fails_to_start_gives_advisory_and_no_tools(self):
        broken = {"command": sys.executable,
                  "args": ["-c", "import sys; sys.exit(3)"], "timeout_s": 60}
        specs = self.load({"servers": {"broken": broken}})
        self.assertEqual(specs, [])
        self.assertEqual(len(self.advisories), 1)
        adv = self.advisories[0]
        self.assertEqual(adv.id, "mcp:broken")
        self.assertFalse(adv.required)
        self.assertFalse(adv.ok)
        self.assertIn("broken", adv.detail)

    def test_missing_command_gives_advisory_not_exception(self):
        cfg = {"servers": {"ghost": {"command": "/nonexistent/no-such-mcp-binary",
                                     "args": [], "timeout_s": 10}}}
        self.assertEqual(self.load(cfg), [])
        self.assertEqual([a.id for a in self.advisories], ["mcp:ghost"])

    def test_empty_or_missing_config_gives_no_tools_and_no_advisory(self):
        self.assertEqual(self.load(None), [])
        self.assertEqual(self.load({}), [])
        self.assertEqual(self.load({"servers": {}}), [])
        self.assertEqual(self.advisories, [])

    def test_importing_the_module_does_not_start_anything(self):
        import subprocess
        code = (
            "import sys; sys.modules['langchain.mcp'] = None; "
            "sys.modules['fastmcp'] = None; "
            "sys.path.insert(0, %r); import core.tools.mcp" % str(_bootstrap.REPO)
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    # -- update check and banner ---------------------------------------------

    def test_quiet_settings_are_set_before_the_server_starts(self):
        seen = {}
        real = mcp._make_transport

        def spy(server):
            seen["check"] = os.environ.get("FASTMCP_CHECK_FOR_UPDATES")
            seen["banner"] = os.environ.get("FASTMCP_SHOW_SERVER_BANNER")
            return real(server)

        with mock.patch.object(mcp, "_make_transport", side_effect=spy):
            self.load(_cfg())
        self.assertEqual(seen, {"check": "off", "banner": "false"})

    def test_server_process_sees_the_quiet_settings(self):
        # The echo server sets nothing itself, so "off" can only come from us.
        env = self.by_name(self.load(_cfg()))["mcp__echo__report_env"]
        out = env.run({"name": "FASTMCP_CHECK_FOR_UPDATES"}, self.ctx)
        self.assertEqual(out.content, "off")
        out = env.run({"name": "FASTMCP_SHOW_SERVER_BANNER"}, self.ctx)
        self.assertEqual(out.content, "false")

    # -- no network ----------------------------------------------------------

    def test_no_network_connection_is_attempted_in_process(self):
        refusing = _Attempts()
        real_connect = socket.socket.connect

        def guard(sock, address):
            refusing.attempts.append(address)
            if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
                raise AssertionError(f"network connect attempted: {address!r}")
            return real_connect(sock, address)

        with mock.patch.object(socket.socket, "connect", guard):
            specs = self.load(_cfg())
            self.by_name(specs)["mcp__echo__echo"].run({"text": "a"}, self.ctx)
        for address in refusing.attempts:
            self.assertIsInstance(address, (str, tuple))
            if isinstance(address, tuple):
                self.assertIn(address[0], ("127.0.0.1", "::1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
