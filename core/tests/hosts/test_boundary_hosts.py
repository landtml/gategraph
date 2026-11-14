"""Boundary hardening on the host side, ported from the legacy core/tests/test_boundary.py
(X1 B5b): the pre-tool guard fails closed on a broken policy (F18), the pre-tool contract holds
under config garbage (T9), and one hand-written fact does not blank the session context (F28).

Run alone with: python3 core/tests/hosts/test_boundary_hosts.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import json
import random
import tempfile
import unittest
from pathlib import Path

import yaml

from core.hosts import claude_code, session
from core.kernel import config
from fixtures_v2 import make_project

ACCT_TOKEN = "010190" + "-1234"  # synthetic, assembled at run time


def write_event(root, path, content, sid="S1"):
    return {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "Write",
            "tool_input": {"file_path": str(root / path), "content": content}}


def mutate(obj, rng, depth=0):
    pool = [None, True, False, 0, -1, 3.14, "", "x", [], {}, [1, 2], {"a": 1}, ["mixed", {"b": 2}]]
    if isinstance(obj, dict) and obj and depth < 4:
        key = rng.choice(sorted(obj, key=str))
        action = rng.random()
        out = dict(obj)
        if action < 0.3:
            del out[key]
        elif action < 0.6:
            out[key] = rng.choice(pool)
        elif action < 0.7:
            out[True] = out.pop(key)
        else:
            out[key] = mutate(out[key], rng, depth + 1)
        return out
    if isinstance(obj, list) and obj and depth < 4:
        out = list(obj)
        i = rng.randrange(len(out))
        out[i] = rng.choice(pool) if rng.random() < 0.5 else mutate(out[i], rng, depth + 1)
        return out
    return rng.choice(pool)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")
        self.adapter_path = self.root / "harness" / "adapter" / "adapter.yaml"
        self.base = yaml.safe_load(self.adapter_path.read_text(encoding="utf-8"))

    def write_adapter(self, data):
        self.adapter_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def pre(self, event):
        return claude_code.run("pre_tool", json.dumps(event), root=self.root)

    @staticmethod
    def decision(stdout):
        return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]


class TestPreToolFailClosed(Base):
    def test_pretool_fail_closes_on_idless_policy(self):
        # End to end: an id-less deny policy must DENY the ACCT write (fail-closed), not allow it.
        data = dict(self.base)
        data["policy"] = {"deny": [{"regex": r"\d{6}\s*-?\s*\d{4}", "summary": "no account number"}]}
        self.write_adapter(data)
        out, _, code = self.pre(write_event(self.root, "doc.md", f"id: {ACCT_TOKEN}"))
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(out), "deny", f"ACCT write was ALLOWED: {out!r}")

    def test_pretool_contract_under_config_garbage(self):
        """For every adapter mutant: no traceback, valid hook JSON, and an unloadable config
        denies the ACCT write (seeded fuzz, the T9 detector)."""
        rng = random.Random(2024)
        for i in range(25):
            mutant = self.base
            for _ in range(rng.randint(1, 3)):
                mutant = mutate(mutant, rng)
            if not isinstance(mutant, dict):
                mutant = {"schema": mutant}
            self.write_adapter(mutant)
            out, err, code = self.pre(write_event(self.root, "doc.md", f"id: {ACCT_TOKEN}"))
            self.assertEqual(code, 0, f"iteration {i}")
            self.assertNotIn("Traceback", err, f"iteration {i}")
            if out.strip():
                json.loads(out)  # any output must be valid hook JSON
            try:
                config.load(self.root)
                loadable = True
            except Exception:
                loadable = False  # the loader contract has its own fuzz test
            if not loadable:
                self.assertEqual(self.decision(out), "deny",
                                 f"iteration {i}: unloadable config but the write was "
                                 f"ALLOWED (fail-open)\nmutant: {mutant!r}")


class TestSessionStartFacts(Base):
    def test_session_start_still_prints_context(self):
        p = self.root / "harness" / "state" / "facts.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump({"facts": [{"text": "visible fact"}, "bare string fact"]}))
        reply = session.start_reply(self.root, None)
        self.assertIn("visible fact", reply.context or "",
                      "session context was silently blanked by one bad entry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
