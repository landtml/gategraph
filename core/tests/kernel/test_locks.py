"""Tests for core/kernel/locks.py: non-blocking exclusive locks that name their holder.

Standard library only. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import os
import subprocess
import tempfile
import unittest

from core.kernel.locks import LockHeld, file_lock

REPO = _bootstrap.REPO

CHILD = """
import sys
sys.path.insert(0, {repo!r})
from core.kernel.locks import file_lock
with file_lock({lock!r}, owner='child-session'):
    print('locked', flush=True)
    sys.stdin.read()
"""


class TestFileLock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock = pathlib.Path(tmp.name) / "state" / "gate.lock"

    def test_lock_is_exclusive_across_processes(self):
        code = CHILD.format(repo=str(REPO), lock=str(self.lock))
        child = subprocess.Popen([sys.executable, "-c", code],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(LockHeld) as cm:
                with file_lock(self.lock, owner="parent-session"):
                    self.fail("second holder acquired a held lock")
            self.assertEqual(cm.exception.holder.get("owner"), "child-session")
            self.assertIn("child-session", str(cm.exception))
        finally:
            child.stdin.close()
            child.wait(timeout=30)
            child.stdout.close()
        self.assertEqual(child.returncode, 0)
        with file_lock(self.lock, owner="parent-session"):
            pass

    def test_lock_failure_names_holder_pid(self):
        with file_lock(self.lock, owner="first"):
            with self.assertRaises(LockHeld) as cm:
                with file_lock(self.lock, owner="second"):
                    pass
        self.assertEqual(cm.exception.holder.get("owner"), "first")
        self.assertEqual(cm.exception.holder.get("pid"), os.getpid())

    def test_lock_released_on_exit(self):
        with file_lock(self.lock):
            pass
        with file_lock(self.lock, owner="again"):
            pass

    def test_lock_released_after_exception(self):
        with self.assertRaises(RuntimeError):
            with file_lock(self.lock):
                raise RuntimeError("boom")
        with file_lock(self.lock):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
