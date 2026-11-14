#!/usr/bin/env python3
"""Session discipline (H2): the stop rule, the session start context, the handoff.

Covers B07 (one automatic stub per session), B32 (one arming helper), B47 (atomic
handoff writes, whole-line headings) and B48 (session start archives the previous
working note). Runnable on its own: python3 core/tests/hosts/test_session.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401,E402

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from core import brand
from core.hosts import bridge, session
from core.kernel import memory, paths
from core.kernel.types import GateSpec, Verdict

REPO = Path(_bootstrap.REPO)
SHIMS = REPO / "core" / "hosts" / "shims"
GATES = (GateSpec(id="build"), GateSpec(id="review", needs=("build",)))
CFG = SimpleNamespace(root=None, schema=2, project="proj", unit_dir="units",
                      packs=(), checks=(), gates=GATES)
LEGACY_AUTO = next(m for m in brand.LEGACY_GEN_MARKERS if m.endswith("auto-handoff"))


def stop_reply(root, sid, cfg=CFG):
    event = bridge.HOSTS["claude_code"].parse(json.dumps(
        {"session_id": sid, "hook_event_name": "Stop", "stop_hook_active": False}))
    return session.stop_reply(root, event, cfg=cfg)


def start_reply(root, cfg=CFG):
    event = bridge.HOSTS["claude_code"].parse(json.dumps(
        {"session_id": "boot-1", "hook_event_name": "SessionStart", "source": "startup"}))
    return session.start_reply(root, event, cfg=cfg)


def write_unit(root, status, effective, slug="u1"):
    """An active unit: .active names it, manifest.yaml is a schema-2 projection."""
    unit = root / "units" / slug
    unit.mkdir(parents=True, exist_ok=True)
    (root / "units" / ".active").write_text(
        yaml.safe_dump({"slug": slug, "session_id": None}), encoding="utf-8")
    manifest = {"schema": 2, "unit": slug, "project": "proj", "thread_id": "t-1",
                "status": status, "config_digest": "0" * 8, "checkpoint_id": None,
                "head": None, "gates": {}, "effective": list(effective), "stale": []}
    (unit / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


class SessionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write_handoff(self, text, age_s=0.0):
        h = self.root / session.HANDOFF_NAME
        h.write_text(text, encoding="utf-8")
        if age_s:
            past = time.time() - age_s
            os.utime(h, (past, past))
        return h


class TestOneStubPerSession(SessionCase):
    def test_B07_one_stub_per_session(self):
        # A unit that stays in progress keeps the stop blocked, so every stop reaches
        # the stub path once the block cap is reached.
        write_unit(self.root, "in_progress", [])
        self.write_handoff("## Now\nworking\n\n## Next\nnext\n", age_s=600)
        session.arm(self.root, "s-1")
        replies = [stop_reply(self.root, "s-1") for _ in range(10)]
        self.assertEqual([r.block for r in replies], [True] * 3 + [False] * 7)
        text = (self.root / session.HANDOFF_NAME).read_text(encoding="utf-8")
        self.assertEqual(text.count("## Auto-handoff"), 1)
        self.assertEqual(text.count(session.AUTO_TAG), 1)
        self.assertIn("## Now\nworking", text)

    def test_block_renders_as_stop_decision(self):
        write_unit(self.root, "in_progress", [])
        session.arm(self.root, "s-2")
        reply = stop_reply(self.root, "s-2")
        self.assertTrue(reply.block)
        text, code = bridge.HOSTS["claude_code"].render(reply, "stop")
        self.assertEqual(code, 0)
        body = json.loads(text)
        self.assertEqual(body["decision"], "block")
        self.assertIn("'build'", body["reason"])

    def test_B07_stub_replaces_previous_auto_stub(self):
        legacy = (f"\n<!-- {LEGACY_AUTO} -->\n## Auto-handoff 2020-01-01T00:00:00\n\n"
                  "### git status\n\n  (clean)\n")
        self.write_handoff("## Now\nworking\n\n## Next\nnext\n" + legacy)
        session.write_fallback_stub(self.root, CFG, now=datetime(2026, 11, 13, 9, 0, 0))
        text = (self.root / session.HANDOFF_NAME).read_text(encoding="utf-8")
        self.assertEqual(text.count("## Auto-handoff"), 1)
        self.assertNotIn("2020-01-01", text)
        self.assertNotIn(LEGACY_AUTO, text)
        self.assertIn("2026-11-13T09:00:00", text)
        self.assertIn("## Now\nworking", text)
        self.assertIn("## Next\nnext", text)

        session.write_fallback_stub(self.root, CFG, now=datetime(2026, 11, 14, 8, 0, 0))
        text = (self.root / session.HANDOFF_NAME).read_text(encoding="utf-8")
        self.assertEqual(text.count("## Auto-handoff"), 1)
        self.assertEqual(text.count(session.AUTO_TAG), 1)
        self.assertNotIn("2026-11-13T09:00:00", text)
        self.assertIn("2026-11-14T08:00:00", text)
        self.assertEqual(session.trailing_stub_count(self.root), 1)


class TestHandoffWrites(SessionCase):
    def test_B47_handoff_written_atomically(self):
        self.write_handoff("## Now\nold\n\n## Next\nold\n")
        with mock.patch.object(os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                session.write_handoff(self.root, "## Now\nnew\n\n## Next\nnew\n")
        text = (self.root / session.HANDOFF_NAME).read_text(encoding="utf-8")
        self.assertEqual(text, "## Now\nold\n\n## Next\nold\n")
        self.assertEqual(list(self.root.glob(".tmp-*")), [])

        session.write_handoff(self.root, "## Now\nnew\n\n## Next\nnew\n")
        self.assertIn("new", (self.root / session.HANDOFF_NAME).read_text(encoding="utf-8"))

    def test_B47_heading_must_match_whole_line(self):
        self.write_handoff("## Nowhere\nx\n\n## Next\ny\n")
        problems = session.validate_handoff(self.root)
        self.assertTrue(any("'## Now'" in p for p in problems), problems)
        self.write_handoff("## Now  \nx\n\n## Next\ny\n")
        self.assertEqual(session.validate_handoff(self.root), [])


class TestSessionStart(SessionCase):
    def test_B48_session_start_archives_previous_working_note(self):
        memory.write_now(self.root, "note A: finish the parser")
        reply = start_reply(self.root)
        self.assertIn("note A: finish the parser", reply.context or "")
        self.assertEqual(memory.read_now(self.root).strip(), "")
        archived = list((paths.state_dir(self.root) / session.NOW_ARCHIVE).glob("*.md"))
        self.assertEqual(len(archived), 1)
        self.assertIn("note A: finish the parser", archived[0].read_text(encoding="utf-8"))

        quiet = start_reply(self.root)
        self.assertFalse(quiet.context)

    def test_active_unit_status_in_context(self):
        write_unit(self.root, "in_progress", ["build"])
        reply = start_reply(self.root)
        self.assertIn("u1", reply.context or "")
        self.assertIn("'review'", reply.context or "")


class TestStopReasonCommand(SessionCase):
    def test_stop_reason_names_next_gate_command(self):
        # A stamped schema-1 project: the adapter's gate_commands table and a legacy manifest,
        # as the stamped gate.py writes it (no schema key, a gates mapping, no effective list).
        from test_bridge import make_root
        make_root(self.root)
        unit = self.root / "units" / "n1"
        unit.mkdir(parents=True)
        (self.root / "units" / ".active").write_text(
            yaml.safe_dump({"slug": "n1", "session_id": "S"}), encoding="utf-8")
        (unit / "manifest.yaml").write_text(yaml.safe_dump(
            {"slug": "n1", "title": "", "created": "2026-11-13T09:00:00",
             "status": "in_progress", "meta": {}, "gates": {}}), encoding="utf-8")
        session.arm(self.root, "S")
        self.write_handoff("## Now\nx\n\n## Next\ny\n")  # fresh: only the gate blocks

        reply = stop_reply(self.root, "S", cfg=None)
        self.assertTrue(reply.block)
        self.assertIn("python3 harness/core/gate.py approve composed --unit n1", reply.reason)

        # With HANDOFF.md missing, the gate reason comes first and the stale reason follows
        # it, joined by the legacy " ALSO: " separator.
        (self.root / session.HANDOFF_NAME).unlink()
        reply = stop_reply(self.root, "S", cfg=None)
        gate_part, _, stale_part = reply.reason.partition(" ALSO: ")
        self.assertIn("approve composed --unit n1", gate_part)
        self.assertTrue(stale_part.startswith("HANDOFF.md is stale"), reply.reason)

        # A schema-2 unit with no command table to read falls back to the gate attempt.
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        write_unit(other, "in_progress", [], slug="u1")
        session.arm(other, "S2")
        (other / session.HANDOFF_NAME).write_text("## Now\nx\n\n## Next\ny\n", encoding="utf-8")
        reply = stop_reply(other, "S2")
        self.assertIn("gate attempt build --unit u1", reply.reason)


class TestStopRule(SessionCase):
    def test_no_tracked_work_is_never_blocked(self):
        write_unit(self.root, "in_progress", [])
        self.write_handoff("## Now\nx\n", age_s=0)
        reply = stop_reply(self.root, "never-armed")
        self.assertFalse(reply.block)
        self.assertEqual(reply.reason, "")
        self.assertFalse(paths.state_dir(self.root).joinpath("stopcount-never-armed").exists())

    def test_invalid_session_id_is_never_blocked(self):
        write_unit(self.root, "in_progress", [])
        self.assertFalse(stop_reply(self.root, "../escape").block)
        self.assertFalse(stop_reply(self.root, None).block)

    def test_unit_status_is_read_from_projection(self):
        cases = [
            ("in_progress", [], True, "'build'"),
            ("in_progress", ["build"], True, "'review'"),
            ("in_progress", ["build", "review"], False, None),
            ("paused", [], False, None),        # waiting at an interrupt
            ("escalated", [], False, None),
            ("abandoned", [], False, None),
            ("complete", ["build", "review"], False, None),
        ]
        for status, effective, blocks, named in cases:
            with self.subTest(status=status, effective=effective):
                root = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, root, True)
                write_unit(root, status, effective)
                session.arm(root, "s-proj")
                self.write_handoff_at(root, "## Now\nx\n\n## Next\ny\n")
                reply = stop_reply(root, "s-proj")
                self.assertEqual(reply.block, blocks)
                if named:
                    self.assertIn(named, reply.reason)

    def write_handoff_at(self, root, text):
        (root / session.HANDOFF_NAME).write_text(text, encoding="utf-8")

    def test_stale_handoff_blocks_until_consolidated(self):
        self.write_handoff("## Now\nold\n\n## Next\nold\n", age_s=600)
        session.arm(self.root, "s-h")
        reply = stop_reply(self.root, "s-h")
        self.assertTrue(reply.block)
        self.assertIn("HANDOFF.md is stale", reply.reason)
        self.write_handoff("## Now\nnew\n\n## Next\nnew\n")
        self.assertFalse(stop_reply(self.root, "s-h").block)

    def test_B32_arm_helper(self):
        self.assertTrue(session.arm(self.root, "s-1"))
        self.assertTrue(paths.marker_path(self.root, "s-1").exists())
        self.assertTrue(session.is_armed(self.root, "s-1"))
        self.assertFalse(session.arm(self.root, "../escape"))
        self.assertFalse(session.arm(self.root, None))
        self.assertEqual(sorted(p.name for p in self.root.rglob("*escape*")), [])


class TestBoundary(unittest.TestCase):
    def test_stop_rule_never_imports_graph_layer(self):
        files = [REPO / "core" / "hosts" / "session.py", *sorted(SHIMS.glob("*.py"))]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for banned in ("core.graph", "langgraph", "langchain"):
                self.assertNotIn(banned, text, f"{path.name} mentions {banned}")
        code = ("import sys; import core.hosts.session; "
                "print([m for m in sys.modules if m.startswith(('core.graph', 'langgraph', 'langchain'))])")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_shims_fail_open(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        with tempfile.TemporaryDirectory() as empty:
            for shim in ("stop_gate.py", "session_start.py"):
                for stdin in ("not json", "", json.dumps({"hook_event_name": "Stop"})):
                    with self.subTest(shim=shim, stdin=stdin):
                        out = subprocess.run([sys.executable, str(SHIMS / shim)], input=stdin,
                                             cwd=empty, env=env, capture_output=True,
                                             text=True, timeout=60)
                        self.assertEqual(out.returncode, 0, out.stderr)
                        self.assertEqual(out.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
