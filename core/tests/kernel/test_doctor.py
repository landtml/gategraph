"""Doctor: a stale cache is ignored and said so (B52), the dependency lock is
enforced as required, tracing under privacy strict is a required failure, the
Python floor is brand.MIN_PYTHON, and the CLI (ported from the version-1 doctor
tests): POSIX-only platform check, JSON mode with its cache, and exit codes."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path

from core import brand
from core.kernel import doctor, paths
from fixtures_v2 import make_project


def _by_name(results, name):
    matches = [r for r in results if r.name == name]
    assert len(matches) == 1, [r.name for r in results]
    return matches[0]


class DoctorTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")
        self.missing_lock = Path(self._tmp.name) / "absent.lock"

    def tearDown(self):
        self._tmp.cleanup()

    def test_B52_stale_doctor_cache_ignored(self):
        now = datetime(2026, 11, 13, 12, 0, 0)
        cache = paths.state_dir(self.root) / doctor.CACHE_NAME
        cache.parent.mkdir(parents=True, exist_ok=True)
        stale = {"at": (now - timedelta(days=2)).isoformat(), "ok": True, "checks": []}
        cache.write_text(json.dumps(stale), encoding="utf-8")
        payload, note = doctor.read_cache(self.root, now=now)
        self.assertIsNone(payload)
        self.assertIn("stale", note)

        fresh = {"at": (now - timedelta(hours=1)).isoformat(), "ok": True, "checks": []}
        cache.write_text(json.dumps(fresh), encoding="utf-8")
        payload, _ = doctor.read_cache(self.root, now=now)
        self.assertEqual(payload["ok"], True)

    def test_dependency_lock_mismatch_fails_as_required(self):
        installed = metadata.version("PyYAML")
        lock = Path(self._tmp.name) / "good.lock"
        lock.write_text(f"PyYAML=={installed} \\\n    --hash=sha256:00\n", encoding="utf-8")
        result = _by_name(doctor.run_doctor(self.root, lock_path=lock, env={}), "dependency-lock")
        self.assertTrue(result.ok, result.detail)
        self.assertTrue(result.required)

        lock.write_text("PyYAML==0.0.1 \\\n    --hash=sha256:00\n", encoding="utf-8")
        result = _by_name(doctor.run_doctor(self.root, lock_path=lock, env={}), "dependency-lock")
        self.assertFalse(result.ok)
        self.assertTrue(result.required)
        self.assertIn("PyYAML", result.detail)

    def test_tracing_variable_under_strict_privacy_is_required_failure(self):
        strict = doctor.run_doctor(self.root, privacy="strict", lock_path=self.missing_lock,
                                   env={"LANGSMITH_TRACING": "true"})
        tracing = _by_name(strict, "tracing")
        self.assertFalse(tracing.ok)
        self.assertTrue(tracing.required)

        relaxed = doctor.run_doctor(self.root, privacy=None, lock_path=self.missing_lock,
                                    env={"LANGSMITH_TRACING": "true"})
        tracing = _by_name(relaxed, "tracing")
        self.assertFalse(tracing.ok)
        self.assertFalse(tracing.required)

        clean = doctor.run_doctor(self.root, privacy="strict", lock_path=self.missing_lock,
                                  env={})
        self.assertTrue(_by_name(clean, "tracing").ok)

    def test_python_floor_is_brand_minimum(self):
        results = doctor.run_doctor(self.root, lock_path=self.missing_lock, env={})
        floor = f"{brand.MIN_PYTHON[0]}.{brand.MIN_PYTHON[1]}"
        self.assertIn(f">= {floor}", _by_name(results, "python").detail)

    def test_platform_win32_is_a_required_failure(self):
        with mock.patch("sys.platform", "win32"):
            plat = doctor._platform_check()
        self.assertTrue(plat.required, "platform check must be REQUIRED")
        self.assertFalse(plat.ok)
        self.assertIn("WSL", plat.detail)

    def test_platform_posix_passes(self):
        plat = doctor._platform_check()
        self.assertTrue(plat.ok, plat.detail)
        self.assertTrue(plat.required)

    def _cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.main(["--root", str(self.root), *args])
        return code, out.getvalue()

    def test_cli_json_mode_writes_the_cache(self):
        code, text = self._cli("--json")
        data = json.loads(text)
        self.assertEqual(data["ok"], code == 0)
        self.assertIn("platform", [c["name"] for c in data["checks"]])
        self.assertTrue(doctor.cache_path(self.root).is_file(),
                        "doctor must cache results for session_start")

    def test_cli_required_failure_exits_one_and_caches_not_ok(self):
        with mock.patch("sys.platform", "win32"):
            code, _ = self._cli("--quiet")
        self.assertEqual(code, 1)
        cached, _note = doctor.read_cache(self.root)
        self.assertFalse(cached["ok"])


if __name__ == "__main__":
    unittest.main()
