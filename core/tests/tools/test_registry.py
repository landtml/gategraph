"""Toolbox registry and harness tools (plan section 1.10, brief A1, fixes B36).

Standard library only. Runs with .venv/bin/python and with the system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import ast
import json
import os
import tempfile
import unittest

from core.kernel.types import Action, ActionKind, Budget
from core.tools import spec as S
from core.tools import harness_tools
from core.packs.ops.tool import apply_spec
from core.tools.exec import ExecPolicy
from core.tools.registry import ToolRefused, Toolbox, build_specs, default_specs

HARNESS_SOURCE = _bootstrap.REPO / "core" / "tools" / "harness_tools.py"


class _Journal:
    def __init__(self):
        self.store = {}

    def seen(self, key):
        return self.store.get(key)

    def record(self, key, result):
        self.store[key] = result


def _tree(root):
    """Every file under root, as {relative posix path: bytes}."""
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


class ToolboxTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve()
        self.unit = self.root / "units" / "a"
        self.scratch = self.root / "scratch"
        self.unit.mkdir(parents=True)
        self.scratch.mkdir()
        (self.unit / "notes.txt").write_text("alpha\n", encoding="utf-8")
        self.journal = _Journal()
        self.ctx = S.ToolContext(
            root=self.root, unit_dir=self.unit, scratch_dir=self.scratch,
            writable=("units/a/**", "scratch/**"), blind=(),
            budget=Budget(), journal=self.journal,
        )

    def make_spec(self, name="probe", effects=("read",), kind=ActionKind.READ,
                  idempotent=True, needs_approval=False, run=None):
        calls = []

        def default_run(args, ctx):
            calls.append(args)
            return S.ToolResult(content=f"ran {len(calls)}")

        spec = S.ToolSpec(
            name=name, description="probe tool",
            args_schema={"type": "object", "properties": {}, "required": []},
            effects=frozenset(effects),
            to_action=lambda args: Action(kind=kind, tool=name),
            run=run or default_run,
            idempotent=idempotent, needs_approval=needs_approval,
        )
        return spec, calls

    def call(self, toolbox, name, args=None, call_id="call-1", **kw):
        return toolbox.call(name, args or {}, thread_id="thread-1",
                            tool_call_id=call_id, **kw)


class TestToolboxBasics(ToolboxTestCase):
    def test_get_names_and_unknown_tool(self):
        spec, _ = self.make_spec(name="beta")
        other, _ = self.make_spec(name="alpha")
        tb = Toolbox([spec, other], self.ctx)
        self.assertEqual(tb.names(), ("alpha", "beta"))
        self.assertIs(tb.get("beta"), spec)
        with self.assertRaises(ToolRefused):
            tb.get("missing")
        with self.assertRaises(ToolRefused):
            self.call(tb, "missing")

    def test_duplicate_names_are_rejected(self):
        first, _ = self.make_spec(name="same")
        second, _ = self.make_spec(name="same")
        with self.assertRaises(ValueError):
            Toolbox([first, second], self.ctx)

    def test_missing_required_argument_is_refused_before_run(self):
        spec = S.ToolSpec(
            name="needs_path", description="", args_schema={
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"]},
            effects=frozenset({"read"}),
            to_action=lambda args: Action(kind=ActionKind.READ, tool="needs_path"),
            run=lambda args, ctx: S.ToolResult(content="ran"),
            idempotent=True,
        )
        tb = Toolbox([spec], self.ctx)
        with self.assertRaises(ToolRefused):
            self.call(tb, "needs_path", {})

    def test_returns_tool_result_from_run(self):
        spec, calls = self.make_spec()
        tb = Toolbox([spec], self.ctx)
        result = self.call(tb, "probe")
        self.assertIsInstance(result, S.ToolResult)
        self.assertEqual(result.content, "ran 1")
        self.assertEqual(len(calls), 1)


class TestEffects(ToolboxTestCase):
    def test_B36_tool_without_declared_effects_is_refused(self):
        # A tool that declares no effects cannot even be constructed.
        with self.assertRaises(ValueError):
            S.ToolSpec(
                name="silent", description="", args_schema={"type": "object"},
                effects=frozenset(),
                to_action=lambda args: Action(kind=ActionKind.READ, tool="silent"),
                run=lambda args, ctx: S.ToolResult(content="ran"),
                idempotent=True,
            )
        # A tool that declares only read but whose action is a write is refused
        # by the toolbox before its body runs.
        ran = []

        def run(args, ctx):
            ran.append(args)
            return S.ToolResult(content="ran")

        spec, _ = self.make_spec(name="sneaky", effects=("read",),
                                 kind=ActionKind.WRITE, run=run)
        tb = Toolbox([spec], self.ctx)
        with self.assertRaises(ToolRefused):
            self.call(tb, "sneaky")
        self.assertEqual(ran, [])

    def test_needs_approval_is_refused_without_approval(self):
        spec, calls = self.make_spec(name="risky", needs_approval=True)
        tb = Toolbox([spec], self.ctx)
        with self.assertRaises(ToolRefused):
            self.call(tb, "risky")
        self.assertEqual(calls, [])
        result = self.call(tb, "risky", call_id="call-2", approved=True)
        self.assertEqual(result.content, "ran 1")


class TestJournal(ToolboxTestCase):
    def test_non_idempotent_tool_returns_recorded_result_on_repeat(self):
        spec, calls = self.make_spec(name="apply", effects=("write",),
                                     kind=ActionKind.WRITE, idempotent=False)
        tb = Toolbox([spec], self.ctx)
        first = self.call(tb, "apply", call_id="same")
        second = self.call(tb, "apply", call_id="same")
        self.assertEqual(len(calls), 1, "a repeated key must not run the tool again")
        self.assertEqual(first, second)

    def test_idempotent_tool_is_repeated(self):
        spec, calls = self.make_spec(name="look", idempotent=True)
        tb = Toolbox([spec], self.ctx)
        self.call(tb, "look", call_id="same")
        self.call(tb, "look", call_id="same")
        self.assertEqual(len(calls), 2)


class TestHarnessTools(ToolboxTestCase):
    HARNESS_ARGS = {
        "gate_status": {},
        "request_gate": {"gate": "G1"},
        "note_decision": {"text": "chose option A"},
        "commit_fact": {"key": "k", "value": "v"},
        "escalate": {"reason": "stuck"},
    }

    def test_request_gate_marks_work_step_finished_and_changes_nothing(self):
        (self.unit / "notes.txt").write_text("alpha\n", encoding="utf-8")
        before = _tree(self.root)
        tb = Toolbox(harness_tools.specs(), self.ctx)
        result = self.call(tb, "request_gate", {"gate": "G1"})
        self.assertEqual(result.touched, ())
        payload = json.loads(result.content)
        self.assertEqual(payload["work_step"], "finished")
        self.assertEqual(payload["gate"], "G1")
        self.assertEqual(_tree(self.root), before)

    def test_no_harness_tool_can_set_a_gate(self):
        # Static: the module does not reach the surface store or the evidence chain.
        tree = ast.parse(HARNESS_SOURCE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for module in imported:
            self.assertNotIn("surface", module)
            self.assertNotIn("evidence", module)
            self.assertNotIn("core.graph", module)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertFalse(node.name.startswith("set_"), node.name)
                self.assertNotIn("set_gate", node.name)
        # Dynamic: every harness tool leaves the workspace unchanged and
        # reports no touched path.
        before = _tree(self.root)
        tb = Toolbox(harness_tools.specs(), self.ctx)
        for name, args in self.HARNESS_ARGS.items():
            result = self.call(tb, name, args, call_id=f"id-{name}")
            self.assertEqual(result.touched, (), name)
            payload = json.loads(result.content)
            self.assertNotIn("gate_set", payload, name)
            self.assertNotIn("verdict", payload, name)
        self.assertEqual(_tree(self.root), before)

    def test_harness_tools_are_exactly_the_five_named_ones(self):
        names = {spec.name for spec in harness_tools.specs()}
        self.assertEqual(names, {"gate_status", "request_gate", "note_decision",
                                 "commit_fact", "escalate"})


class TestDefaultSpecs(ToolboxTestCase):
    def test_default_specs_hold_six_file_tools_and_five_harness_tools(self):
        names = {spec.name for spec in default_specs()}
        self.assertEqual(names, {"read_file", "list_dir", "glob", "grep",
                                 "write_file", "edit_file",
                                 "gate_status", "request_gate", "note_decision",
                                 "commit_fact", "escalate"})


GRANTED_BUILTINS = {"read_file", "list_dir", "glob", "grep", "write_file", "edit_file",
                    "run_command", "fetch_url", "web_search", "query_table",
                    "render_document", "extract_text", "gate_status", "request_gate",
                    "note_decision", "commit_fact", "escalate"}


def _full_config():
    return {
        "exec_policy": ExecPolicy(allowed={"python3": sys.executable}),
        "net_config": {"allowed_hosts": {"docs.example.test"},
                       "search_backend": lambda query, limit: []},
        "doc_config": {"render_command": ["pandoc", "{source}", "-o", "{output}"]},
    }


class TestBuildSpecs(unittest.TestCase):
    def test_build_specs_returns_every_granted_builtin(self):
        specs, gaps = build_specs(GRANTED_BUILTINS, **_full_config())
        self.assertEqual({spec.name for spec in specs}, GRANTED_BUILTINS)
        self.assertEqual(gaps, [])

    def test_missing_exec_policy_is_a_named_gap(self):
        specs, gaps = build_specs({"read_file", "run_command"}, exec_policy=None)
        self.assertEqual({spec.name for spec in specs}, {"read_file"})
        self.assertEqual([name for name, _ in gaps], ["run_command"])
        self.assertIn("allow-list", gaps[0][1])

    def test_pack_tool_is_registered(self):
        specs, gaps = build_specs({"apply_plan"}, pack_tools={"apply_plan": apply_spec})
        self.assertEqual(gaps, [])
        self.assertEqual([spec.name for spec in specs], ["apply_plan"])
        self.assertTrue(specs[0].needs_approval)

    def test_web_search_without_backend_and_unknown_names_are_named_gaps(self):
        specs, gaps = build_specs({"web_search", "profile_table", "apply_plan"},
                                  net_config={"allowed_hosts": {"docs.example.test"}})
        self.assertEqual(specs, [])
        self.assertEqual([name for name, _ in gaps],
                         ["apply_plan", "profile_table", "web_search"])
        self.assertTrue(all(reason for _, reason in gaps))


if __name__ == "__main__":
    unittest.main()
