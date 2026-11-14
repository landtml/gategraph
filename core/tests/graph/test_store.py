"""Tests for core/graph/store.py: the checkpointer factory (plan section 1.15, step 2).

Runs with .venv/bin/python (the graph layer). Strict msgpack, SQLite at
paths.state_dir(root)/checkpoints.sqlite, in-memory on request, and the encrypted
serializer under privacy: strict.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import dataclasses
import importlib.util
import os
import tempfile
import unittest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from core.graph import store
from core.kernel import guard, paths
from core.kernel.types import Action, ActionKind, Verdict


class GuardConfig:
    """The fields guard.decide reads, with an adapter that protects nothing (as in
    core/tests/kernel/test_guard.py FakeCfg)."""

    def __init__(self, root):
        self.deny = []
        self.ask = []
        self.protected_paths = []
        self.blind = []
        self.root = root


class TestCheckpointerFactory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)

    def test_sqlite_file_is_in_state_dir(self):
        saver = store.make_checkpointer({"root": self.root})
        self.assertIsInstance(saver, SqliteSaver)
        db = paths.state_dir(self.root) / "checkpoints.sqlite"
        self.assertTrue(db.is_file(), "the SQLite file must exist after the factory returns")

    def test_checkpoint_db_lives_in_kernel_state_dir(self):
        saver = store.make_checkpointer({"root": self.root})
        self.assertIsInstance(saver, SqliteSaver)
        db = paths.state_dir(self.root) / "checkpoints.sqlite"
        self.assertTrue(db.is_file(), "the checkpoint database must be in the kernel state dir")
        action = Action(kind=ActionKind.WRITE, tool="write_file", path=str(db), content="x")
        decision = guard.decide(action, GuardConfig(root=str(self.root)))
        self.assertEqual((decision.verdict, decision.rule), (Verdict.DENY, "state-dir"))

    def test_in_memory_when_asked_writes_no_file(self):
        saver = store.make_checkpointer({"root": self.root, "in_memory": True})
        self.assertIsInstance(saver, InMemorySaver)
        self.assertFalse(paths.state_dir(self.root).exists())

    def test_missing_root_is_refused_for_sqlite(self):
        with self.assertRaises((KeyError, ValueError)):
            store.make_checkpointer({})

    def test_unknown_privacy_value_is_refused(self):
        with self.assertRaises(ValueError):
            store.make_checkpointer({"root": self.root, "privacy": "loose"})

    def test_strict_msgpack_is_set_and_serializer_is_strict(self):
        self.assertEqual(os.environ.get("LANGGRAPH_STRICT_MSGPACK"), "true")
        saver = store.make_checkpointer({"root": self.root})
        serde = saver.serde
        self.assertIsInstance(serde, JsonPlusSerializer)
        self.assertFalse(serde.pickle_fallback)
        self.assertIsNone(serde._allowed_msgpack_modules)

    def test_default_privacy_is_not_encrypted(self):
        saver = store.make_checkpointer({"root": self.root})
        self.assertNotIsInstance(saver.serde, EncryptedSerializer)

    def test_nested_unit_state_round_trips_through_saver(self):
        saver = store.make_checkpointer({"root": self.root})
        state = {"project": "example", "unit": "u1", "status": "in_progress",
                 "evidence": [{"seq": 0, "prev": None, "digest": "d" * 64,
                               "checks": [{"id": "policy", "findings": []}],
                               "meta": {"deep": [1, 2.5, None, True, {"k": []}]}}],
                 "attempts": {"verified": 2}, "fail_streak": 1,
                 "spend": {"usd": 0.0123, "tokens": 4567},
                 "work_summary": "unicode æøå ✓"}
        self.assertEqual(saver.serde.loads_typed(saver.serde.dumps_typed(state)), state)

    def test_dataclass_value_is_blocked_by_strict_serializer(self):
        @dataclasses.dataclass
        class Custom:
            a: int

        saver = store.make_checkpointer({"root": self.root})
        result = saver.serde.loads_typed(saver.serde.dumps_typed({"x": Custom(1)}))
        self.assertNotIsInstance(result.get("x"), Custom)

    def test_privacy_strict_uses_encrypted_serializer(self):
        if importlib.util.find_spec("Crypto") is None:
            # pycryptodome is not in core/requirements.lock; the factory must say so.
            with self.assertRaises(ImportError) as ctx:
                store.make_checkpointer({"root": self.root, "privacy": "strict"})
            self.assertIn("pycryptodome", str(ctx.exception).lower())
            return
        old = os.environ.get("LANGGRAPH_AES_KEY")
        os.environ["LANGGRAPH_AES_KEY"] = "k" * 32
        self.addCleanup(lambda: os.environ.pop("LANGGRAPH_AES_KEY", None) if old is None
                        else os.environ.__setitem__("LANGGRAPH_AES_KEY", old))
        saver = store.make_checkpointer({"root": self.root, "privacy": "strict"})
        self.assertIsInstance(saver.serde, EncryptedSerializer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
