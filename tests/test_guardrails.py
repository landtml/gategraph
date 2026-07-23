"""Guard rails: the pre-push
hook script and its installer target.

Hermetic: the hook is exercised inside a scratch git repo with a stub
`make` on PATH, so no real suite runs here."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "pre-push"


class TestPrePushHook(unittest.TestCase):
    def test_script_exists_executable_and_parses(self):
        self.assertTrue(HOOK.is_file(), "scripts/pre-push missing")
        self.assertTrue(HOOK.stat().st_mode & stat.S_IXUSR,
                        "scripts/pre-push must be executable")
        subprocess.run(["sh", "-n", str(HOOK)], check=True)

    def _run_in_scratch(self, make_exit: int) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            bindir = root / "bin"
            bindir.mkdir()
            stub = bindir / "make"
            stub.write_text("#!/bin/sh\necho \"stub-make $@\" >&2\n"
                            f"exit {make_exit}\n")
            stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
            return subprocess.run(["sh", str(HOOK)], cwd=root, env=env,
                                  capture_output=True, text=True)

    def test_green_make_passes(self):
        proc = self._run_in_scratch(0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("test-core eval", proc.stderr,
                      "the hook must run the fast lane (make test-core eval)")

    def test_red_make_blocks_the_push(self):
        proc = self._run_in_scratch(1)
        self.assertNotEqual(proc.returncode, 0,
                            "a red fast lane must block the push")

    def test_installer_target_exists(self):
        text = (REPO / "Makefile").read_text(encoding="utf-8")
        self.assertIn("install-hooks:", text)
        self.assertIn(".git/hooks/pre-push", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
