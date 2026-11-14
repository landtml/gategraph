"""Tests for core/kernel/paths.py: root discovery (B30), session ids (B45), atomic writes (B46).

Standard library only. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import os
import stat
import tempfile
import unittest
from unittest import mock

from core import brand
from core.kernel import paths
from core.kernel.paths import (RootNotFound, atomic_write, find_root, marker_path,
                               state_dir, unit_path, valid_slug)

ENV_NAMES = (brand.ENV_PREFIX + "PROJECT_DIR",
             *(p + "PROJECT_DIR" for p in brand.LEGACY_ENV_PREFIXES),
             "CLAUDE_PROJECT_DIR")


def make_root(base):
    """A directory holding the adapter file: a project root."""
    adapter = pathlib.Path(base) / "harness" / "adapter"
    adapter.mkdir(parents=True, exist_ok=True)
    (adapter / "adapter.yaml").write_text("project: example\n", encoding="utf-8")
    return pathlib.Path(base).resolve()


class EnvIsolated(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name).resolve()


class TestFindRoot(EnvIsolated):
    def test_B30_nested_marker_does_not_misroot(self):
        root = make_root(self.tmp / "project")
        nested = root / "docs" / "nested"
        nested.mkdir(parents=True)
        (nested / "CLAUDE.md").write_text("# nested\n", encoding="utf-8")
        self.assertEqual(find_root(start=nested), root)

    def test_B30_explicit_start_wins(self):
        a = make_root(self.tmp / "a")
        b = make_root(self.tmp / "b")
        os.environ[brand.ENV_PREFIX + "PROJECT_DIR"] = str(b)
        sub = a / "sub"
        sub.mkdir()
        self.assertEqual(find_root(start=sub), a)

    def test_env_used_when_no_explicit_start(self):
        make_root(self.tmp / "walk")
        b = make_root(self.tmp / "b")
        os.environ[brand.ENV_PREFIX + "PROJECT_DIR"] = str(b)
        with mock.patch.object(pathlib.Path, "cwd", return_value=self.tmp / "walk"):
            self.assertEqual(find_root(), b)

    def test_legacy_env_variable_is_read(self):
        b = make_root(self.tmp / "b")
        os.environ["CLAUDE_PROJECT_DIR"] = str(b)
        with mock.patch.object(pathlib.Path, "cwd", return_value=self.tmp):
            self.assertEqual(find_root(), b)

    def test_env_without_adapter_is_an_error(self):
        os.environ[brand.ENV_PREFIX + "PROJECT_DIR"] = str(self.tmp / "empty")
        (self.tmp / "empty").mkdir()
        with self.assertRaises(RootNotFound):
            find_root()

    def test_no_fallback_to_module_location(self):
        # With no adapter at or above the directory, the repository root
        # (where this module lives) must not be returned.
        bare = self.tmp / "bare"
        bare.mkdir()
        repo = pathlib.Path(_bootstrap.REPO).resolve()
        with mock.patch.object(pathlib.Path, "cwd", return_value=bare):
            with self.assertRaises(RootNotFound):
                find_root()
        with self.assertRaises(RootNotFound):
            find_root(start=bare)
        self.assertNotEqual(repo, bare)


class TestSessionIds(EnvIsolated):
    def test_B45_session_id_validated(self):
        root = make_root(self.tmp / "p")
        for bad in ("../escape", "a/b", "", "-rf", ".hidden", "x\n", None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    marker_path(root, bad)
        good = marker_path(root, "sess-01.a_b")
        self.assertEqual(good.parent, state_dir(root))
        self.assertEqual(good.name, "touched-sess-01.a_b")

    def test_valid_slug_rejects_trailing_newline(self):
        self.assertFalse(valid_slug("abc\n"))
        self.assertTrue(valid_slug("abc"))

    def test_state_dir_uses_brand_name(self):
        root = self.tmp / "p"
        self.assertEqual(state_dir(root), root / brand.STATE_DIRNAME)

    def test_unit_path_validates_slug(self):
        root = self.tmp / "p"
        self.assertEqual(unit_path(root, "harness/units", "u1"),
                         root / "harness" / "units" / "u1")
        with self.assertRaises(ValueError):
            unit_path(root, "harness/units", "../u1")


class TestAtomicWrite(EnvIsolated):
    def test_B46_atomic_write_keeps_mode(self):
        target = self.tmp / "keep.txt"
        target.write_text("old\n", encoding="utf-8")
        os.chmod(target, 0o640)
        atomic_write(target, "new\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o640)

    def test_B46_new_file_gets_umask_mode(self):
        target = self.tmp / "fresh.txt"
        umask = os.umask(0o022)
        os.umask(umask)
        atomic_write(target, "x")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o666 & ~umask)

    def test_no_temp_files_left_behind(self):
        atomic_write(self.tmp / "a.txt", "one")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["a.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
