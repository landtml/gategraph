"""G8: the gate command layer (plan sections 1.8, 1.12, 1.15; bugs B02, B32, B39).

Every test runs core/cli/gate.py in a subprocess against a temporary project. Offline only:
the checks are the shell builtins ``true`` and ``false``, there is no model and no network.
Terminal-only paths (channel tty) use a pseudo-terminal for the child's standard input.

Run standalone: ``.venv/bin/python core/tests/graph/test_cli_gate.py``.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import hashlib
import json
import os
import pty
import re
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from core import brand
from core.graph import runner
from core.kernel import config, locks

REPO = pathlib.Path(__file__).resolve().parents[3]
CLI = REPO / "core" / "cli" / "gate.py"
UNIT = "u1"
THREAD = "demo/u1"
TOKEN_LINE = "approval token (shown once, not stored): "
STATE = brand.STATE_DIRNAME

ADAPTER = """\
schema: 2
project: demo
unit_noun: report
unit_dir: units
checks:
  - {id: c1, command: "%(c1)s", severity: fatal}
  - {id: c2, command: "true", severity: fatal}
gates:
  - id: g1
    checks: [c1]
    repair: {max_attempts: %(attempts)s}
  - id: g2
    needs: [g1]
    checks: [c2]
    approval: {required: true, channel: %(channel)s}
"""


FAIL_CHECK = """\
import json, sys
print(json.dumps({"rule": "demo.no_todo", "severity": "fatal", "path": "units/u1/note.md",
                  "line": 3, "message": "TODO left in the note"}))
sys.exit(1)
"""


COUNT_CHECK = """\
import pathlib, sys
with pathlib.Path(sys.argv[1]).open("a", encoding="utf-8") as fh:
    fh.write("run\\n")
"""


class Result:
    def __init__(self, proc):
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    def __repr__(self):
        return f"Result(code={self.code}, out={self.out!r}, err={self.err!r})"


def make_project(tmp, *, c1="true", channel="tty", attempts=1):
    root = Path(tmp)
    (root / "harness" / "adapter").mkdir(parents=True)
    (root / "harness" / "adapter" / "adapter.yaml").write_text(
        ADAPTER % {"c1": c1, "channel": channel, "attempts": attempts}, encoding="utf-8")
    (root / "units").mkdir()
    return root


def gate(root, *args, tty=False):
    """Run the command in a subprocess. ``tty=True`` gives it a pseudo-terminal as stdin."""
    cmd = [sys.executable, str(CLI), "--root", str(root), *args]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith((brand.ENV_PREFIX, *brand.LEGACY_ENV_PREFIXES))}
    if tty:
        master, slave = pty.openpty()
        try:
            proc = subprocess.run(cmd, stdin=slave, capture_output=True, text=True,
                                  timeout=300, env=env, cwd=str(root))
        finally:
            os.close(slave)
            os.close(master)
    else:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=300, env=env, cwd=str(root))
    return Result(proc)


def seed_unit(root, unit=UNIT):
    """A real file inside g1's surface. A hashed gate over an empty unit is refused
    (seal.empty_surface), so each attempt that should pass needs one first."""
    path = Path(root) / "units" / unit / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("sketch\n", encoding="utf-8")


def unit_dir(root, unit=UNIT):
    return Path(root) / "units" / unit


def manifest(root, unit=UNIT):
    return yaml.safe_load((unit_dir(root, unit) / "manifest.yaml").read_text(encoding="utf-8"))


def evidence_lines(root, unit=UNIT):
    path = unit_dir(root, unit) / "evidence.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def ledger_rows(root):
    path = Path(root) / "harness" / "state" / "failures.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def db_bytes(root):
    path = Path(root) / STATE / "checkpoints.sqlite"
    return path.read_bytes() if path.exists() else b""


def thread_rows(root, thread):
    path = Path(root) / STATE / "checkpoints.sqlite"
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
                            (thread,)).fetchone()[0]
    finally:
        conn.close()


class GateCommandTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_project(self._tmp.name)

    def ok(self, *args, tty=False):
        result = gate(self.root, *args, tty=tty)
        self.assertEqual(result.code, 0, f"{args}: {result}")
        return result

    def refused(self, *args, tty=False, expect=()):
        before = len([r for r in ledger_rows(self.root) if r["kind"] == "gate_refusal"])
        result = gate(self.root, *args, tty=tty)
        self.assertNotEqual(result.code, 0, f"{args} should be refused: {result}")
        self.assertIn("\nnext: ", "\n" + result.err, f"{args}: no next command: {result.err!r}")
        for text in expect:
            self.assertIn(text, result.err, f"{args}: {result.err!r}")
        after = len([r for r in ledger_rows(self.root) if r["kind"] == "gate_refusal"])
        self.assertEqual(after, before + 1, f"{args}: a refusal writes one ledger row")
        return result

    def pending_of(self):
        """The open requests as the command prints them (one line each, 'kind gate=... ')."""
        return gate(self.root, "pending", "--unit", UNIT).out

    # ---- basic verbs -----------------------------------------------------------

    def test_start_creates_unit_and_rests_at_first_work_request(self):
        self.ok("start", "--unit", UNIT, "--session-id", "S1")
        m = manifest(self.root)
        self.assertEqual(m["thread_id"], THREAD)
        self.assertIsNone(m["head"])
        self.assertTrue(m["checkpoint_id"])
        self.assertEqual(evidence_lines(self.root), [])
        out = self.pending_of()
        self.assertIn("work", out)
        self.assertIn("gate=g1", out)
        self.assertIn("gate attempt g1 --unit u1", out)

    def test_status_shows_in_progress_at_a_work_request_and_the_next_command(self):
        self.ok("start", "--unit", UNIT)
        out = self.ok("status", "--unit", UNIT).out
        self.assertIn("unit u1: in_progress", out)
        self.assertNotIn("paused", out)
        self.assertIn("gate attempt g1 --unit u1", out)

    def test_pending_on_unknown_unit_names_the_start_command_and_creates_nothing(self):
        self.refused("pending", "--unit", "ghost", expect=("gate start --unit ghost",))
        self.assertFalse((self.root / "units" / "ghost").exists())
        self.assertTrue(any(row["kind"] == "gate_refusal" for row in ledger_rows(self.root)))

    def test_attempt_passes_the_gate_and_waits_at_the_next_work_request(self):
        self.ok("start", "--unit", UNIT)
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)
        m = manifest(self.root)
        self.assertEqual(m["effective"], ["g1"])
        self.assertEqual(m["status"], "in_progress")
        self.assertIn("gate=g2", self.pending_of())
        self.assertEqual(len(evidence_lines(self.root)), 1)

    def test_attempt_on_a_gate_the_unit_is_not_waiting_for_is_refused(self):
        self.ok("start", "--unit", UNIT)
        self.refused("attempt", "g2", "--unit", UNIT, expect=("gate attempt g1 --unit u1",))

    def test_attempt_under_a_held_lock_is_refused_with_the_next_command(self):
        self.ok("start", "--unit", UNIT)
        lock = runner.lock_path(Path(self.root) / STATE / "locks", THREAD)
        with locks.file_lock(lock, owner="test holder"):
            self.refused("attempt", "g1", "--unit", UNIT, expect=("gate status --unit u1",))

    def test_status_never_takes_the_lock(self):
        self.ok("start", "--unit", UNIT)
        lock = runner.lock_path(Path(self.root) / STATE / "locks", THREAD)
        with locks.file_lock(lock, owner="test holder"):
            out = self.ok("status", "--unit", UNIT).out
        self.assertIn("in_progress", out)

    # ---- approvals (section 1.12) ----------------------------------------------

    def test_approval_flow_with_tty_channel_completes_from_a_terminal(self):
        self.ok("start", "--unit", UNIT)
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)
        self.ok("attempt", "g2", "--unit", UNIT)
        self.assertIn("approval", self.pending_of())
        self.assertEqual(manifest(self.root)["status"], "paused")
        self.ok("proceed", "--unit", UNIT, "--decision", "approve", "--by", "alice",
                tty=True)
        m = manifest(self.root)
        self.assertEqual(m["status"], "complete")
        self.assertEqual(m["effective"], ["g1", "g2"])

    def test_approval_channel_tty_refuses_without_a_terminal(self):
        self.ok("start", "--unit", UNIT)
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)
        self.ok("attempt", "g2", "--unit", UNIT)
        before = self.pending_of()
        self.refused("proceed", "--unit", UNIT, "--decision", "approve", "--by", "alice",
                     expect=("terminal", "gate proceed --unit u1 --decision approve"))
        self.assertEqual(self.pending_of(), before)
        self.assertEqual(manifest(self.root)["status"], "paused")
        self.assertTrue(any("terminal" in row.get("reason", "") for row in ledger_rows(self.root)
                            if row["kind"] == "gate_refusal"))

    def test_invalid_answer_is_refused_before_proceed_and_the_thread_still_proceeds(self):
        self.ok("start", "--unit", UNIT)
        before = self.pending_of()
        self.refused("proceed", "--unit", UNIT, "--decision", "approve", "--by", "alice",
                     expect=("attempt",))
        self.assertEqual(self.pending_of(), before)  # nothing reached the graph
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)
        self.ok("attempt", "g2", "--unit", UNIT)
        self.refused("proceed", "--unit", UNIT, "--decision", "reject", "--by", "alice",
                     tty=True, expect=("note",))
        self.ok("proceed", "--unit", UNIT, "--decision", "approve", "--by", "alice",
                tty=True)
        self.assertEqual(manifest(self.root)["status"], "complete")

    def test_token_channel_checks_the_digest_and_never_stores_the_token(self):
        root = make_project(Path(self._tmp.name) / "tok", channel="token")
        self.root = root
        self.ok("start", "--unit", UNIT)
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)
        raised = self.ok("attempt", "g2", "--unit", UNIT).out
        self.assertIn(TOKEN_LINE, raised)
        token = raised.split(TOKEN_LINE, 1)[1].split()[0]
        self.refused("proceed", "--unit", UNIT, "--decision", "approve", "--by", "alice",
                     "--token", "not-the-token", expect=("token",))
        self.assertEqual(manifest(root)["status"], "paused")
        self.ok("proceed", "--unit", UNIT, "--decision", "approve", "--by", "alice",
                "--token", token)
        self.assertEqual(manifest(root)["status"], "complete")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.assertIn(digest.encode("ascii"), db_bytes(root))
        self.assertNotIn(token.encode("utf-8"), db_bytes(root))
        self.assertNotIn(token, (unit_dir(root) / "manifest.yaml").read_text(encoding="utf-8"))
        self.assertNotIn(token, (unit_dir(root) / "evidence.jsonl").read_text(encoding="utf-8"))

    # ---- B02, B39, B32 ----------------------------------------------------------

    def _escalated_and_abandoned(self):
        """A failing gate escalates on its first attempt; the human abandons it (tty)."""
        root = make_project(Path(self._tmp.name) / "fail", c1="false")
        self.root = root
        self.ok("start", "--unit", UNIT)
        escalated = gate(root, "attempt", "g1", "--unit", UNIT)
        self.assertEqual(escalated.code, 1, f"an escalating attempt exits 1: {escalated}")
        self.ok("proceed", "--unit", UNIT, "--decision", "abandon", "--by", "alice",
                "--note", "stop", tty=True)
        self.assertEqual(manifest(root)["status"], "abandoned")
        return root

    def test_B02_pause_refused_on_terminal_unit(self):
        self._escalated_and_abandoned()
        self.refused("pause", "--unit", UNIT, expect=("abandoned", "gate status --unit u1"))
        self.assertEqual(manifest(self.root)["status"], "abandoned")

    def test_pause_on_a_waiting_unit_leaves_it_paused(self):
        self.ok("start", "--unit", UNIT)
        out = self.ok("pause", "--unit", UNIT).out
        self.assertIn("gate attempt g1 --unit u1", out)
        self.assertEqual(manifest(self.root)["status"], "paused")

    def test_B39_abandon_unknown_unit_is_an_error(self):
        self.refused("abandon", "--unit", "ghost", expect=("gate start --unit ghost",))
        self.assertFalse((self.root / "units" / "ghost").exists())
        self.assertTrue(any(row["kind"] == "gate_refusal" for row in ledger_rows(self.root)))

    def test_abandon_at_a_work_request_is_refused_with_the_pause_command(self):
        self.ok("start", "--unit", UNIT)
        self.refused("abandon", "--unit", UNIT, "--by", "alice", expect=("gate pause --unit u1",))
        self.assertEqual(manifest(self.root)["status"], "in_progress")

    def test_escalate_is_refused_with_the_next_command_and_leaves_the_unit_alone(self):
        self.ok("start", "--unit", UNIT)
        self.refused("escalate", "--unit", UNIT, "--note", "stuck", expect=("gate pause --unit u1",))
        self.assertEqual(manifest(self.root)["status"], "in_progress")
        self.assertTrue(any(row["kind"] == "gate_refusal" for row in ledger_rows(self.root)))

    def test_B32_every_mutating_verb_arms_the_session(self):
        marker = self.root / STATE / "touched-S1"
        # Ok or refused, each mutating verb arms the session first (B32).
        steps = [
            ("start", "--unit", UNIT, "--session-id", "S1"),
            ("attempt", "g1", "--unit", UNIT, "--session-id", "S1"),
            ("pause", "--unit", UNIT, "--session-id", "S1"),
            ("abandon", "--unit", UNIT, "--by", "alice", "--session-id", "S1"),
            ("escalate", "--unit", UNIT, "--note", "stuck", "--session-id", "S1"),
            ("proceed", "--unit", UNIT, "--decision", "attempt", "--by", "alice",
             "--session-id", "S1"),
            ("archive", "--unit", UNIT, "--session-id", "S1"),
        ]
        for args in steps:
            if marker.exists():
                marker.unlink()
            result = gate(self.root, *args)
            self.assertTrue(marker.exists(), f"{args[0]} did not arm the session: {result}")

    # ---- proceed without a thread, projections, stop hook -------------------------

    def test_proceed_without_a_thread_says_so_and_creates_no_thread(self):
        self.ok("start", "--unit", UNIT)
        self.refused("proceed", "--unit", "ghost", "--decision", "attempt", "--by", "alice",
                     expect=("gate start --unit ghost",))
        self.assertFalse((self.root / "units" / "ghost").exists())
        self.assertEqual(thread_rows(self.root, "demo/ghost"), 0)

    def test_projections_are_written_after_every_run_including_interrupts(self):
        self.ok("start", "--unit", UNIT)
        first = manifest(self.root)
        self.assertTrue(first["checkpoint_id"])
        self.assertEqual(evidence_lines(self.root), [])
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)
        lines = evidence_lines(self.root)
        self.assertEqual([line["gate"] for line in lines], ["g1"])
        second = manifest(self.root)
        self.assertEqual(second["head"]["seq"], 0)
        self.assertNotEqual(second["checkpoint_id"], first["checkpoint_id"])
        report = self.ok("verify", "--unit", UNIT).out
        self.assertIn("checkpoint", report)
        self.assertRegex(report, r"step 3 checkpoint: pass")

    def _stop_reply(self, sid="S1"):
        from core.hosts import bridge, session
        event = bridge.HOSTS["claude_code"].parse(json.dumps(
            {"session_id": sid, "hook_event_name": "Stop", "stop_hook_active": False}))
        return session.stop_reply(self.root, event, cfg=config.load(self.root))

    def test_work_wait_is_in_progress_and_stop_blocks_with_attempt_command(self):
        self.ok("start", "--unit", UNIT, "--session-id", "S1")
        self.assertEqual(manifest(self.root)["status"], "in_progress")
        reply = self._stop_reply()
        self.assertTrue(reply.block, f"a unit waiting for work holds the stop: {reply}")
        self.assertIn("gate attempt g1 --unit u1", reply.reason)

    def test_approval_wait_is_paused_and_stop_allows(self):
        self.ok("start", "--unit", UNIT, "--session-id", "S1")
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)
        self.ok("attempt", "g2", "--unit", UNIT)
        self.assertEqual(manifest(self.root)["status"], "paused")
        # a current handoff, so nothing but the unit could hold the stop
        time.sleep(0.02)
        (self.root / "HANDOFF.md").write_text("# HANDOFF\n\n## Now\nx\n\n## Next\ny\n",
                                              encoding="utf-8")
        reply = self._stop_reply()
        self.assertFalse(reply.block, f"a unit waiting for a human does not hold the stop: {reply}")

    def test_pause_at_a_work_request_holds_until_the_next_run(self):
        self.ok("start", "--unit", UNIT, "--session-id", "S1")
        self.ok("pause", "--unit", UNIT)
        self.assertEqual(manifest(self.root)["status"], "paused")
        self.assertIn("paused", self.ok("status", "--unit", UNIT).out)
        time.sleep(0.02)  # a current handoff, so only the unit could hold the stop
        (self.root / "HANDOFF.md").write_text("# HANDOFF\n\n## Now\nx\n\n## Next\ny\n",
                                              encoding="utf-8")
        self.assertFalse(self._stop_reply().block, "a paused unit does not hold the stop")
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)  # the next run moves the checkpoint
        self.assertEqual(manifest(self.root)["status"], "in_progress")
        self.assertIn("in_progress", self.ok("status", "--unit", UNIT).out)
        reply = self._stop_reply()
        self.assertTrue(reply.block)
        self.assertIn("gate attempt g2 --unit u1", reply.reason)  # g1 passed; g2 is next

    def test_attempt_failure_prints_findings_and_exits_1(self):
        script = Path(self._tmp.name) / "fail_check.py"
        script.write_text(FAIL_CHECK, encoding="utf-8")
        root = make_project(Path(self._tmp.name) / "retry",
                            c1=f"{sys.executable} {script}", attempts=3)
        self.root = root
        self.ok("start", "--unit", UNIT)
        result = gate(root, "attempt", "g1", "--unit", UNIT)
        self.assertEqual(result.code, 1, f"a failing attempt exits 1: {result}")
        self.assertIn("demo.no_todo", result.out)
        self.assertIn("units/u1/note.md:3", result.out)
        self.assertIn("TODO left in the note", result.out)
        self.assertIn("remedy:", result.out)
        self.assertIn("next: gate attempt g1 --unit u1", result.out)
        self.assertEqual(manifest(root)["status"], "in_progress")  # the work request is re-issued

    def test_attempt_escalation_prints_why_and_the_proceed_command_and_exits_1(self):
        root = make_project(Path(self._tmp.name) / "esc", c1="false")
        self.root = root
        self.ok("start", "--unit", UNIT)
        result = gate(root, "attempt", "g1", "--unit", UNIT)
        self.assertEqual(result.code, 1, f"an escalating attempt exits 1: {result}")
        self.assertIn("escalated", result.out)
        self.assertIn("why:", result.out)
        self.assertIn("next: gate proceed --unit u1 --decision retry|abandon", result.out)

    def test_attempt_pass_exits_0(self):
        self.ok("start", "--unit", UNIT)
        seed_unit(self.root)
        result = self.ok("attempt", "g1", "--unit", UNIT)
        self.assertIn("gate g1 passed for u1", result.out)
        self.assertEqual(result.err, "")

    def test_attempt_after_edit_reopens_stale_gate(self):
        # A file edit makes g1 stale. The next attempt at g1 must re-run its checks and pass;
        # it must not refuse with "already passed" from the stored snapshot.
        counter = Path(self._tmp.name) / "g1-runs.txt"
        script = Path(self._tmp.name) / "count_check.py"
        script.write_text(COUNT_CHECK, encoding="utf-8")
        root = make_project(Path(self._tmp.name) / "reedit",
                            c1=f"{sys.executable} {script} {counter}")
        self.root = root
        self.ok("start", "--unit", UNIT)
        seed_unit(root)
        self.ok("attempt", "g1", "--unit", UNIT)
        self.assertEqual(counter.read_text(encoding="utf-8").count("run"), 1)
        (unit_dir(root) / "report.md").write_text("edited after the gate was earned\n",
                                                  encoding="utf-8")
        again = self.ok("attempt", "g1", "--unit", UNIT)
        self.assertIn("gate g1 passed for u1", again.out)
        self.assertNotIn("already passed", again.err)
        self.assertEqual(counter.read_text(encoding="utf-8").count("run"), 2)
        self.assertEqual(manifest(root)["effective"], ["g1"])

    def test_verify_fails_at_the_step_that_is_wrong(self):
        self.ok("start", "--unit", UNIT)
        seed_unit(self.root)
        self.ok("attempt", "g1", "--unit", UNIT)
        path = unit_dir(self.root) / "manifest.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["head"]["digest"] = "0" * 64
        path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        result = gate(self.root, "verify", "--unit", UNIT)
        self.assertEqual(result.code, 1)
        self.assertRegex(result.out, r"step 2 manifest head and gates: fail")

    def test_archive_exports_the_chain_then_deletes_the_thread(self):
        self._escalated_and_abandoned()
        self.ok("archive", "--unit", UNIT)
        self.assertTrue((unit_dir(self.root) / "evidence.jsonl").exists())
        self.assertEqual(thread_rows(self.root, THREAD), 0)
        self.refused("pending", "--unit", UNIT, expect=("gate start --unit u1",))

    def test_archive_refused_while_the_unit_is_open(self):
        self.ok("start", "--unit", UNIT)
        self.refused("archive", "--unit", UNIT, expect=("gate pending --unit u1",))
        self.assertGreater(thread_rows(self.root, THREAD), 0)


class TokenShapeTest(unittest.TestCase):
    """Approval tokens never start with '-', so `--token <tok>` always parses (the old
    token_urlsafe generator failed about once in 64 tries)."""

    def test_500_tokens_each_parse_after_the_token_flag(self):
        from core.cli import gate as gate_cli
        parser = gate_cli.build_parser()
        seen = set()
        for _ in range(500):
            tok = gate_cli.new_token()
            self.assertRegex(tok, r"^[0-9a-f]{36}$")
            seen.add(tok)
            args = parser.parse_args(["proceed", "--unit", UNIT, "--decision", "approve",
                                      "--token", tok])
            self.assertEqual(args.token, tok)
        self.assertEqual(len(seen), 500, "tokens must not repeat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
