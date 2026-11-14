"""T11: the upgrade path, tested per release, plus the F25 sync-path fixes.

Two layers:
- ``TestManifestAndSidecars`` (scratch template copy, fast): `gategraph diff` must
  report upstream ADDITIONS (not claim "pristine and current" while `gategraph update`
  would stamp new files); a corrupt MANIFEST.json is a typed refusal, never a
  traceback; a resolved ``.fhnew`` sidecar is cleaned up by the next update
  instead of living forever (the seed gitignores ``*.fhnew``, so git can't
  surface it either).
- ``TestUpgradeFromPreviousRelease``: stamps the PREVIOUS git tag's template
  into a scratch project, ages it realistically (local core hotfix, deleted
  core file, adapter edit), then runs HEAD's `gategraph update` through the real sync
  path: the F9 rehearsal in permanent, per-release form. This leg is what
  keeps the consumer-A 0.1.0 → current rehearsal honest before it happens.
"""
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent

ANSWERS = {"project": "upgradeproj", "artifact_noun": "note", "unit_dir": "notes",
           "gates": ["composed", "checked", "verified"], "hashed_gates": [],
           "seed_deny": True, "loop_enabled": False}


def write_answers(base: Path) -> str:
    p = base / "answers.yaml"
    p.write_text(yaml.safe_dump(ANSWERS))
    return str(p)


class Base(unittest.TestCase):
    """Scratch COPY of the current template (same pattern as test_fh_sync)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.template = base / "template"
        self.template.mkdir()
        for item in ("gategraph.py", "VERSION", "core", "seed"):
            src = TEMPLATE / item
            if src.is_dir():
                shutil.copytree(src, self.template / item,
                                ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, self.template / item)
        self.project = base / "upgradeproj"
        r = self.fh("init", str(self.project), "--answers", write_answers(base))
        assert r.returncode == 0, r.stdout + r.stderr

    def tearDown(self):
        self._tmp.cleanup()

    def fh(self, *args):
        return subprocess.run([sys.executable, str(self.template / "gategraph.py"), *args],
                              capture_output=True, text=True)

    def bump_template(self):
        self.template.joinpath("VERSION").write_text("9.0.0\n")


class TestManifestAndSidecars(Base):
    def test_diff_reports_new_upstream_additions(self):
        (self.template / "core" / "brand_new.py").write_text('"""new core module"""\n')
        self.bump_template()
        r = self.fh("diff", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # additions are not local drift
        self.assertIn("new upstream", r.stdout,
                      "diff must not hide files `gategraph update` would stamp:\n" + r.stdout)
        self.assertIn("brand_new.py", r.stdout)
        self.assertNotIn("pristine and current", r.stdout)

    def test_corrupt_manifest_is_a_typed_refusal(self):
        (self.project / "harness/.fh/MANIFEST.json").write_text("{ this is not json\n")
        for cmd in ("diff", "update"):
            r = self.fh(cmd, str(self.project))
            self.assertNotEqual(r.returncode, 0, f"{cmd} accepted a corrupt manifest")
            self.assertNotIn("Traceback", r.stderr,
                             f"{cmd} tracebacked on a corrupt manifest:\n{r.stderr}")
            self.assertIn("MANIFEST.json", r.stdout + r.stderr)

    def test_manifest_without_files_key_is_a_typed_refusal(self):
        (self.project / "harness/.fh/MANIFEST.json").write_text('{"version": "0.0.1"}\n')
        for cmd in ("diff", "update"):
            r = self.fh(cmd, str(self.project))
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr, r.stderr)

    def test_resolved_fhnew_is_cleaned_by_the_next_update(self):
        target = self.project / "harness/core/PROTOCOL.md"
        target.write_text(target.read_text() + "\nLOCAL HOTFIX\n")
        tpl = self.template / "core/PROTOCOL.md"
        tpl.write_text(tpl.read_text() + "\n## Upstream addition\n")
        self.bump_template()
        r = self.fh("update", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        fhnew = target.with_name(target.name + ".fhnew")
        self.assertTrue(fhnew.exists(), "drift must produce the sidecar")
        # the human resolves the drift by adopting the template version …
        target.write_bytes(fhnew.read_bytes())
        r = self.fh("update", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(fhnew.exists(),
                         "resolved .fhnew must be cleaned up, not live forever")
        r = self.fh("diff", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pristine and current", r.stdout)

    def test_stale_fhnew_next_to_pristine_file_is_removed(self):
        target = self.project / "harness/core/PROTOCOL.md"
        stale = target.with_name(target.name + ".fhnew")
        stale.write_text("left over from a long-resolved drift\n")
        r = self.fh("update", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(stale.exists(),
                         "a sidecar whose base file matches the template is stale")


class TestUpgradeFromPreviousRelease(unittest.TestCase):
    """Stamp the previous RELEASE, age it, upgrade to HEAD through the real path."""

    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["git", "-C", str(TEMPLATE), "describe", "--tags", "--exclude", "legacy-*",
                            "--abbrev=0"], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            raise unittest.SkipTest("no release tag reachable (shallow checkout?)")
        cls.prev_tag = r.stdout.strip()
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        old = base / "old-template"
        old.mkdir()
        tar_path = base / "prev.tar"
        with tar_path.open("wb") as fh:
            subprocess.run(["git", "-C", str(TEMPLATE), "archive", cls.prev_tag],
                           check=True, stdout=fh)
        with tarfile.open(tar_path) as tf:
            tf.extractall(old)
        cls.old_template = old
        cls.project = base / "upgradeproj"
        r = subprocess.run([sys.executable, str(old / "fh.py"), "init",
                            str(cls.project), "--answers", write_answers(base)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{cls.prev_tag} init failed:\n{r.stdout}{r.stderr}"

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_tmp"):
            cls._tmp.cleanup()

    def fh_head(self, *args):
        return subprocess.run([sys.executable, str(TEMPLATE / "gategraph.py"), *args],
                              capture_output=True, text=True)

    def test_full_upgrade_previous_tag_to_head(self):
        project = self.project
        # age the project realistically
        hotfix = project / "harness/core/PROTOCOL.md"
        hotfix.write_text(hotfix.read_text() + "\nLOCAL HOTFIX (pre-upgrade)\n")
        deleted = project / "harness/core/reap.py"
        deleted.unlink()
        adapter = project / "harness/adapter/adapter.yaml"
        adapter.write_text(adapter.read_text() + "# local adapter note\n")

        # HEAD's diff must tell the truth about what an update would do
        r = self.fh_head("diff", str(project))
        self.assertEqual(r.returncode, 1, "local drift must exit 1:\n" + r.stdout)
        self.assertIn("PROTOCOL.md", r.stdout)
        self.assertIn("reap.py", r.stdout)
        if (TEMPLATE / "core/tests/hosts/test_boundary_hosts.py").exists():  # added after the last release
            self.assertIn("new upstream", r.stdout,
                          f"diff hid the upstream additions since {self.prev_tag}:\n"
                          + r.stdout)

        r = self.fh_head("update", str(project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("LOCAL HOTFIX", hotfix.read_text(),
                      "update must never clobber a local core hotfix")
        fhnew = hotfix.with_name(hotfix.name + ".fhnew")
        self.assertTrue(fhnew.exists(), "drifted file must get its .fhnew")
        self.assertIn("DRIFT", r.stdout)
        self.assertTrue(deleted.exists(), "deleted core file must be restored")
        self.assertIn("# local adapter note", adapter.read_text(),
                      "update must never touch the adapter layer")
        if (TEMPLATE / "core/tests/hosts/test_boundary_hosts.py").exists():
            self.assertTrue(
                (project / "harness/core/tests/hosts/test_boundary_hosts.py").exists(),
                f"upstream additions since {self.prev_tag} were not stamped")
        manifest = json.loads((project / "harness/.fh/MANIFEST.json").read_text())
        self.assertEqual(manifest["version"],
                         (TEMPLATE / "VERSION").read_text().strip())

        # resolve the drift by adopting the template version, then re-sync
        hotfix.write_bytes(fhnew.read_bytes())
        r = self.fh_head("update", str(project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(fhnew.exists(), "resolved sidecar must be cleaned up")
        r = self.fh_head("diff", str(project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pristine and current", r.stdout)

        # the upgraded project's own stamp self-check must be green
        check = subprocess.run(["bash", "harness/core/tests/run_checks.sh"],
                               cwd=project, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
