"""Contract tests for core/tools/spec.py (brief C4, plan section 1.10).

Standard library only. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import ast
import subprocess
import unittest

from core.kernel.types import Action, ActionKind, Spend
from core.tools import spec as S

REPO = _bootstrap.REPO
SPEC_PATH = REPO / "core" / "tools" / "spec.py"


def _noop_run(args, ctx):
    return S.ToolResult(content="ok")


def _spec(effects, kind, **extra):
    """A ToolSpec whose to_action always returns an Action of the given kind."""
    return S.ToolSpec(
        name="probe",
        description="test tool",
        args_schema={"type": "object"},
        effects=frozenset(effects),
        to_action=lambda args: Action(kind=kind, tool="probe", path="a.txt"),
        run=_noop_run,
        idempotent=True,
        **extra,
    )


class EffectDeclarationTests(unittest.TestCase):
    def test_undeclared_effect_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            _spec({"delete"}, ActionKind.WRITE)

    def test_undeclared_effect_mixed_with_valid_one_is_rejected(self):
        with self.assertRaises(ValueError):
            _spec({"read", "teleport"}, ActionKind.READ)

    def test_empty_effects_are_rejected(self):
        with self.assertRaises(ValueError):
            _spec(set(), ActionKind.READ)

    def test_valid_effects_are_accepted(self):
        s = _spec({"read", "write"}, ActionKind.WRITE)
        self.assertEqual(s.effects, frozenset({"read", "write"}))

    def test_needs_approval_defaults_to_false(self):
        self.assertFalse(_spec({"read"}, ActionKind.READ).needs_approval)


class ActionConsistencyTests(unittest.TestCase):
    def test_action_kind_matching_effects_is_returned(self):
        s = _spec({"write"}, ActionKind.EDIT)
        action = s.action_for({})
        self.assertIsInstance(action, Action)
        self.assertEqual(action.kind, ActionKind.EDIT)

    def test_action_kind_not_covered_by_effects_is_rejected(self):
        s = _spec({"read"}, ActionKind.WRITE)
        with self.assertRaises(ValueError):
            s.action_for({})

    def test_exec_action_requires_exec_effect(self):
        s = _spec({"write", "net"}, ActionKind.EXEC)
        with self.assertRaises(ValueError):
            s.action_for({})

    def test_net_action_requires_net_effect(self):
        s = _spec({"read"}, ActionKind.NET)
        with self.assertRaises(ValueError):
            s.action_for({})

    def test_to_action_returning_non_action_is_rejected(self):
        s = S.ToolSpec(
            name="probe", description="d", args_schema={},
            effects=frozenset({"read"}),
            to_action=lambda args: {"kind": "read"},
            run=_noop_run, idempotent=True,
        )
        with self.assertRaises(TypeError):
            s.action_for({})


class ToolResultTests(unittest.TestCase):
    def test_absolute_touched_path_is_rejected(self):
        with self.assertRaises(ValueError):
            S.ToolResult(content="x", touched=("/etc/passwd",))

    def test_parent_escape_is_rejected(self):
        with self.assertRaises(ValueError):
            S.ToolResult(content="x", touched=("../outside.txt",))

    def test_parent_escape_inside_path_is_rejected(self):
        with self.assertRaises(ValueError):
            S.ToolResult(content="x", touched=("unit/../../outside.txt",))

    def test_windows_drive_path_is_rejected(self):
        with self.assertRaises(ValueError):
            S.ToolResult(content="x", touched=("C:/outside.txt",))

    def test_empty_touched_path_is_rejected(self):
        with self.assertRaises(ValueError):
            S.ToolResult(content="x", touched=("",))

    def test_relative_touched_paths_are_accepted(self):
        r = S.ToolResult(content="x", touched=("unit/a.txt", "scratch/b/c.md"))
        self.assertEqual(r.touched, ("unit/a.txt", "scratch/b/c.md"))

    def test_result_defaults(self):
        r = S.ToolResult(content="x")
        self.assertEqual(r.touched, ())
        self.assertEqual(r.artifacts, ())
        self.assertEqual(r.cost, Spend())


class IdempotencyKeyTests(unittest.TestCase):
    def test_key_is_sha256_hex(self):
        key = S.idempotency_key("thread-1", "call-1")
        self.assertEqual(len(key), 64)
        int(key, 16)  # raises if not hex

    def test_key_is_deterministic(self):
        self.assertEqual(S.idempotency_key("t", "c"), S.idempotency_key("t", "c"))

    def test_key_depends_on_both_inputs(self):
        base = S.idempotency_key("t", "c")
        self.assertNotEqual(base, S.idempotency_key("t2", "c"))
        self.assertNotEqual(base, S.idempotency_key("t", "c2"))

    def test_key_is_unambiguous_across_the_separator(self):
        self.assertNotEqual(S.idempotency_key("ab", "c"), S.idempotency_key("a", "bc"))


class JournalProtocolTests(unittest.TestCase):
    class DictJournal:
        def __init__(self):
            self._store = {}

        def seen(self, key):
            return self._store.get(key)

        def record(self, key, result):
            self._store[key] = result

    def test_protocol_is_runtime_checkable(self):
        self.assertTrue(isinstance(self.DictJournal(), S.EffectsJournal))

    def test_object_without_methods_is_not_a_journal(self):
        self.assertFalse(isinstance(object(), S.EffectsJournal))

    def test_seen_returns_recorded_result(self):
        j = self.DictJournal()
        key = S.idempotency_key("t", "c")
        self.assertIsNone(j.seen(key))
        result = S.ToolResult(content="done")
        j.record(key, result)
        self.assertIs(j.seen(key), result)


class ToolContextTests(unittest.TestCase):
    def test_context_carries_the_declared_fields(self):
        from core.kernel.types import Budget
        ctx = S.ToolContext(
            root=REPO, unit_dir=REPO / "unit", scratch_dir=REPO / "scratch",
            writable=("unit", "scratch"), blind=("rubrics",),
            budget=Budget(), journal=self.DictJournal(),
        )
        self.assertEqual(ctx.writable, ("unit", "scratch"))
        self.assertEqual(ctx.blind, ("rubrics",))

    class DictJournal:
        def seen(self, key):
            return None

        def record(self, key, result):
            pass


class RuleTextTests(unittest.TestCase):
    """Section 1.12: no tool body calls interrupt(). The docstrings must say so."""

    def test_module_docstring_states_no_interrupt_rule(self):
        self.assertIn("interrupt()", S.__doc__)

    def test_toolspec_docstring_states_no_interrupt_rule(self):
        self.assertIn("interrupt()", S.ToolSpec.__doc__)


class ImportBoundaryTests(unittest.TestCase):
    def test_spec_module_has_no_langchain_or_langgraph_import(self):
        tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        bad = [n for n in names if n.split(".")[0] in ("langchain", "langgraph")]
        self.assertEqual(bad, [])

    def test_spec_imports_under_system_python_without_langchain(self):
        code = (
            "import sys; sys.path.insert(0, %r); "
            "import core.tools.spec; "
            "assert not any(m.startswith(('langchain', 'langgraph')) for m in sys.modules)"
        ) % str(REPO)
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
