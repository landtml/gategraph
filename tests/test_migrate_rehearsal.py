"""M5 rehearsal: `gategraph.py migrate` run end to end on fixtures, checked against the migration
invariants, then undone the way the runbook (docs/MIGRATION.md) tells an owner to undo it.

Contract: the seven things a migration must not break.
Each test names the item it checks. The M4 invariants (tests/test_migrate.py) are not
repeated here in full; this file runs the migration as an owner would and checks the result.

Fixtures, all built in temporary directories. No real project is read or written.
- legacy-0.4: built from the legacy-0.4 tag by tests/test_migrate.py's builder and stamped
  0.4.0-dev, which is what that tag's VERSION file says. An archived unit is committed beside the
  live units, so item 2 is checked against an archive too. The brief also names 0.2.0 and 0.3.0
  fixtures; neither tag exists, so neither fixture is built. The plan's real project was stamped
  from 0.2.0, which this rehearsal does NOT cover; the owner's dry run is the check for it.
- current: a project initialised by this template and stamped at its own version. Migration has
  nothing to do and must say so without writing.

Run alone: ``python3 tests/test_migrate_rehearsal.py``.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_migrate as tm  # noqa: E402  (the M4 fixture builder and helpers; its tests do not run here)

TEMPLATE = tm.TEMPLATE
GATEGRAPH = tm.GATEGRAPH
PROJECT = tm.PROJECT
ARCHIVE_UNIT = "fixture-old"
ARCHIVE_DIR = f"notes/archive/{ARCHIVE_UNIT}"
NEW_UNIT = "fixture-new"
UNIT_FILES = {"manifest.yaml", "evidence.jsonl"}
HOOK_SCRIPT = re.compile(r"(\w+\.py)")
_CACHE = {}


def _require_inputs():
    if tm.LEGACY_TAG not in tm.git(TEMPLATE, "tag", "--list", tm.LEGACY_TAG).split():
        raise unittest.SkipTest(f"tag {tm.LEGACY_TAG} is missing; the fixture cannot be built")
    if not (tm.TEMPLATE_VENV / "bin" / "python").exists():
        raise unittest.SkipTest("template .venv missing: run `make venv` in the template")


def _base() -> Path:
    """The temporary directory holding every fixture of this module, built once."""
    if "base" not in _CACHE:
        _require_inputs()
        base = Path(tempfile.mkdtemp(prefix="rehearsal-"))
        _CACHE["base"] = base
        (base / "fixture").mkdir()
        _CACHE["fixture"] = tm.build_fixture(base / "fixture")
    return _CACHE["base"]


def tearDownModule():
    if _CACHE.get("base"):
        shutil.rmtree(_CACHE["base"], ignore_errors=True)


def add_archive(project: Path) -> None:
    """A finished unit under notes/archive/, committed. Migration must leave all of it alone."""
    shutil.copytree(project / "notes" / "fixture-done", project / ARCHIVE_DIR)
    tm.git(project, "add", "-A")
    tm.git(project, "commit", "-m", "fixture: an archived unit")


def legacy_copy(name: str) -> Path:
    """A fresh copy of the legacy fixture, with the archived unit committed."""
    dest = _CACHE["base"] / name / PROJECT
    dest.parent.mkdir(parents=True)
    shutil.copytree(_CACHE["fixture"], dest, symlinks=True)
    add_archive(dest)
    return dest


def migrate(project: Path, *extra) -> subprocess.CompletedProcess:
    return tm.run([sys.executable, GATEGRAPH, "migrate", project, *extra])


def runner(project: Path, *args) -> subprocess.CompletedProcess:
    """The new runner (core/cli/gate.py) in the project's own venv."""
    return tm.run([tm.venv_python(project), "harness/core/cli/gate.py", *args], cwd=project)


def legacy_status_text(project: Path) -> dict:
    return {slug: tm.legacy(project, "status", "--unit", slug).stdout for slug in tm.UNITS}


def hook_scripts(settings: dict) -> set:
    """The script file names every hook registration runs."""
    return {name for groups in settings["hooks"].values() for g in groups for h in g["hooks"]
            for name in HOOK_SCRIPT.findall(h["command"])}


def report(done: subprocess.CompletedProcess) -> str:
    return done.stdout + done.stderr


class LegacyRehearsal(unittest.TestCase):
    """The legacy-0.4 fixture: one dry run, one apply. Tests read the shared result. Anything that
    writes, or that compares the whole tree, runs in its own copy."""

    @classmethod
    def setUpClass(cls):
        _base()
        cls.project = legacy_copy("legacy")
        cls.status_before = legacy_status_text(cls.project)
        cls.passed_before = {s: tm.passed_from_legacy_status(cls.status_before[s]) for s in tm.UNITS}
        cls.stamp_before = json.loads((cls.project / "harness/.fh/MANIFEST.json").read_text())
        cls.tree_before = tm.tree_digests(cls.project)
        cls.commits_before = tm.commit_count(cls.project)
        cls.head_before = tm.git(cls.project, "rev-parse", "HEAD").strip()
        cls.adapter_before = (cls.project / "harness/adapter/adapter.yaml").read_bytes()
        cls.settings_before = json.loads((cls.project / ".claude/settings.json").read_text())
        cls.makefile_before = (cls.project / "Makefile").read_bytes()
        cls.core_edit_before = (cls.project / "harness/core/hooks/_common.py").read_bytes()
        cls.state_before = tm.tree_digests(cls.project / "harness/state")
        cls.dry = migrate(cls.project)
        cls.tree_after_dry = tm.tree_digests(cls.project)
        cls.apply = migrate(cls.project, "--apply")
        cls.commits_after = tm.commit_count(cls.project)
        cls.head_after = tm.git(cls.project, "rev-parse", "HEAD").strip()
        cls.tree_after = tm.tree_digests(cls.project)

    def copy(self, name: str) -> Path:
        """A copy of the migrated project, for tests that run commands which write."""
        dest = _CACHE["base"] / f"{name}-of-legacy" / PROJECT
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.project, dest, symlinks=True)
        return dest

    def test_every_path_used_is_inside_a_temporary_directory(self):
        self.assertTrue(_CACHE["base"].resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertFalse(self.project.resolve().is_relative_to(TEMPLATE.resolve()))

    def test_fixture_is_stamped_at_the_legacy_tag_and_recorded_as_0_4_dev(self):
        # Read before the migration: the apply restamps the project at the template's version.
        self.assertEqual(self.stamp_before["version"], "0.4.0-dev")

    def test_dry_run_changes_no_file_and_says_so(self):
        self.assertEqual(self.dry.returncode, 0, report(self.dry))
        self.assertIn("dry run: nothing is written", self.dry.stdout)
        self.assertEqual(self.tree_after_dry, self.tree_before)

    def test_apply_exits_zero_and_makes_exactly_one_commit(self):
        self.assertEqual(self.apply.returncode, 0, report(self.apply))
        self.assertEqual(self.commits_after, self.commits_before + 1)

    def test_item_1_no_earned_gate_is_lost(self):
        for slug in tm.UNITS:
            manifest = yaml.safe_load((self.project / "notes" / slug / "manifest.yaml").read_text())
            self.assertEqual(manifest["effective"], self.passed_before[slug], slug)

    def test_item_1_the_runner_shows_the_same_gates_passed(self):
        for slug in tm.UNITS:
            status = runner(self.project, "status", "--unit", slug)
            self.assertEqual(status.returncode, 0, report(status))
            self.assertEqual(tm.passed_from_legacy_status(status.stdout), self.passed_before[slug],
                             slug)

    def test_item_2_only_manifest_and_evidence_change_inside_unit_directories(self):
        changed = tm.git(self.project, "diff", "--name-only", self.head_before, "HEAD", "--",
                         *[f"notes/{s}" for s in tm.UNITS]).split()
        self.assertTrue(changed, "the migration should have rewritten the projections")
        for name in changed:
            self.assertIn(Path(name).name, UNIT_FILES, name)
        after = self.tree_after
        for rel, digest in self.tree_before.items():
            if not rel.startswith("notes/") or rel == "notes/.active":
                continue
            if Path(rel).name in UNIT_FILES:
                continue
            self.assertEqual(after.get(rel), digest, f"{rel} changed")
        added = {rel for rel in after if rel.startswith("notes/") and rel not in self.tree_before}
        self.assertTrue(all(Path(rel).name == "evidence.jsonl" for rel in added), added)

    def test_item_2_the_archived_unit_is_byte_identical_and_was_not_imported(self):
        archive = {rel: d for rel, d in self.tree_before.items() if rel.startswith(ARCHIVE_DIR + "/")}
        self.assertTrue(archive, "the fixture must contain an archived unit")
        for rel, digest in archive.items():
            self.assertEqual(self.tree_after.get(rel), digest, f"{rel} changed")
        self.assertFalse([rel for rel in self.tree_after
                          if rel.startswith(ARCHIVE_DIR + "/") and rel not in self.tree_before])
        self.assertNotIn("schema", yaml.safe_load((self.project / ARCHIVE_DIR / "manifest.yaml")
                                                   .read_text()))

    def test_import_records_carry_the_migration_actor_and_one_per_passed_gate(self):
        lines = (self.project / "notes/fixture-done/evidence.jsonl").read_text().splitlines()
        records = [json.loads(line) for line in lines]
        self.assertEqual([r["kind"] for r in records], ["import"] * len(tm.UNITS["fixture-done"]))
        self.assertEqual({r["actor"]["id"] for r in records}, {"migrate"})
        self.assertEqual([r["gate"] for r in records], tm.UNITS["fixture-done"])

    def test_statuses_of_terminal_and_paused_units_are_kept(self):
        def status(slug):
            return yaml.safe_load((self.project / "notes" / slug / "manifest.yaml").read_text())["status"]
        self.assertEqual(status("fixture-esc"), "escalated")
        self.assertEqual(status("fixture-done"), "complete")
        self.assertEqual(status("fixture-mid"), "paused")

    def test_item_3_hook_registrations_keep_their_events_matchers_and_file_names(self):
        settings = json.loads((self.project / ".claude/settings.json").read_text())
        before = self.settings_before["hooks"]
        self.assertEqual(set(settings["hooks"]), set(before))
        for event, groups in before.items():
            self.assertEqual([g.get("matcher") for g in settings["hooks"][event]],
                             [g.get("matcher") for g in groups], event)
        self.assertEqual(hook_scripts(settings), hook_scripts(self.settings_before))
        commands = [h["command"] for groups in settings["hooks"].values() for g in groups
                    for h in g["hooks"]]
        self.assertTrue(commands)
        for command in commands:
            self.assertIn("harness/core/hosts/shims/", command)
            self.assertNotIn("harness/core/hooks/", command)
        for name in hook_scripts(settings):
            self.assertTrue((self.project / "harness/core/hosts/shims" / name).is_file(), name)

    def test_item_3_each_hook_shim_exits_zero_on_an_empty_payload(self):
        dest = self.copy("hooks")
        for name in sorted(hook_scripts(json.loads((dest / ".claude/settings.json").read_text()))):
            done = subprocess.run(["python3", f"harness/core/hosts/shims/{name}"], cwd=dest,
                                  input="{}", capture_output=True, text=True,
                                  env={**os.environ, "CLAUDE_PROJECT_DIR": str(dest)})
            self.assertEqual(done.returncode, 0, f"{name}: {report(done)}")

    def test_item_4_version_1_adapter_checks_still_run_through_the_shims(self):
        adapter = yaml.safe_load((self.project / "harness/adapter/adapter.yaml").read_text())
        v1 = [c for c in adapter["checks"] if c["id"].startswith("v1-import-")]
        self.assertEqual(len(v1), 2)
        for check in v1:
            done = subprocess.run(check["command"], shell=True, cwd=self.project,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{check['id']}: {report(done)}")

    def test_item_4_a_shim_call_writes_the_deprecation_line_to_the_hooks_log(self):
        dest = self.copy("shimlog")
        code = ("import sys; sys.path.insert(0, 'harness'); from pathlib import Path\n"
                "from core import config; config.load(Path(sys.argv[1]))\n")
        done = tm.run([sys.executable, "-c", code, dest], cwd=dest)
        self.assertEqual(done.returncode, 0, report(done))
        log = (dest / "harness/logs/hooks.log").read_text()
        self.assertIn("deprecated version-1 shim", log)
        self.assertIn("removed one minor release after the migration command ships", log)

    def test_item_4_the_adapter_is_schema_2_loads_and_keeps_the_original(self):
        self.assertEqual((self.project / "harness/adapter/adapter.v1.yaml").read_bytes(),
                         self.adapter_before)
        adapter = yaml.safe_load((self.project / "harness/adapter/adapter.yaml").read_text())
        self.assertEqual(adapter["schema"], 2)
        load = tm.run([sys.executable, "-c",
                       "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
                       "from core.kernel import config; config.load(Path(sys.argv[2]))",
                       str(self.project / "harness"), str(self.project)], cwd=self.project)
        self.assertEqual(load.returncode, 0, report(load))

    def test_item_5_the_local_core_edit_is_kept_beside_the_new_file_and_reported(self):
        local = self.project / "harness/core/hooks/_common.py.local"
        self.assertEqual(local.read_bytes(), self.core_edit_before)
        self.assertIn("_common.py", self.apply.stdout)
        self.assertIn("core/hosts/bridge.py", self.apply.stdout)

    def test_item_6_make_check_passes_and_the_makefile_is_untouched(self):
        self.assertEqual((self.project / "Makefile").read_bytes(), self.makefile_before)
        dest = self.copy("make")
        check = tm.run(["make", "-s", "check"], cwd=dest)
        self.assertEqual(check.returncode, 0, report(check))

    def test_item_6_gate_verify_passes_for_every_unit(self):
        for slug in tm.UNITS:
            verify = runner(self.project, "verify", "--unit", slug)
            self.assertEqual(verify.returncode, 0, report(verify))

    def test_a_new_unit_starts_through_the_runner(self):
        # The rehearsal stops at the first request: approvals need a terminal, so the owner walks
        # the rest of a new unit by hand (runbook step 4).
        dest = self.copy("newunit")
        started = runner(dest, "start", "--unit", NEW_UNIT)
        self.assertEqual(started.returncode, 0, report(started))
        self.assertIn(f"started {NEW_UNIT}.", started.stdout)
        self.assertIn(f"gate attempt composed --unit {NEW_UNIT}", started.stdout)

    def test_running_migrate_again_is_a_no_op(self):
        dest = self.copy("again")
        before, commits = tm.tree_digests(dest), tm.commit_count(dest)
        done = migrate(dest, "--apply")
        self.assertEqual(done.returncode, 0, report(done))
        self.assertEqual(tm.tree_digests(dest), before)
        self.assertEqual(tm.commit_count(dest), commits)


class RollbackRehearsal(unittest.TestCase):
    """The undo the runbook gives: `git revert` of the migration commit, then the state backup
    copied back over harness/state. Afterwards the legacy `gate.py status` of every unit equals
    the text recorded before the migration, and the tree is the pre-migration tree (apart from the
    git-ignored .gategraph/ that the migration creates)."""

    @classmethod
    def setUpClass(cls):
        _base()
        cls.project = legacy_copy("rollback")
        # make check writes files, so it runs before the tree is recorded and again after the
        # rollback's tree is recorded. The fixture carries a deliberate local core edit, so its
        # check fails before any migration; the rollback must give the same result, not a pass.
        cls.make_before = tm.run(["make", "-s", "check"], cwd=cls.project).returncode
        cls.status_before = legacy_status_text(cls.project)
        cls.tree_before = tm.tree_digests(cls.project)
        cls.commits_before = tm.commit_count(cls.project)
        cls.state_before = tm.tree_digests(cls.project / "harness/state")
        cls.apply = migrate(cls.project, "--apply")
        cls.migration_sha = tm.git(cls.project, "rev-parse", "HEAD").strip()
        cls.revert = tm.run(["git", "-C", cls.project, "-c", "user.name=t", "-c",
                             "user.email=t@localhost", "revert", "--no-edit", cls.migration_sha])
        backups = sorted(cls.project.parent.glob(f"{PROJECT}.migrate-backup-*"))
        cls.backup = backups[-1] if backups else None
        if cls.backup is not None:
            shutil.rmtree(cls.project / "harness/state")
            shutil.copytree(cls.backup / "state", cls.project / "harness/state", symlinks=True)
        cls.status_after = legacy_status_text(cls.project)
        cls.commits_after = tm.commit_count(cls.project)
        cls.tree_after_rollback = tm.tree_digests(cls.project)
        cls.make_after = tm.run(["make", "-s", "check"], cwd=cls.project).returncode

    def test_the_migration_applied_and_made_one_commit(self):
        self.assertEqual(self.apply.returncode, 0, report(self.apply))
        self.assertIsNotNone(self.migration_sha)

    def test_the_state_backup_exists_and_holds_the_pre_migration_state(self):
        self.assertIsNotNone(self.backup, "the migration writes a backup beside the project")
        self.assertEqual(tm.tree_digests(self.backup / "state"), self.state_before)

    def test_git_revert_is_one_new_commit_and_removes_the_import_records(self):
        self.assertEqual(self.revert.returncode, 0, report(self.revert))
        self.assertEqual(self.commits_after, self.commits_before + 2)
        self.assertFalse((self.project / "notes/fixture-done/evidence.jsonl").exists())
        self.assertFalse((self.project / "harness/adapter/adapter.v1.yaml").exists())

    def test_legacy_status_of_every_unit_is_back_to_the_recorded_values(self):
        for slug in tm.UNITS:
            self.assertEqual(self.status_after[slug], self.status_before[slug], slug)

    def test_tree_is_the_pre_migration_tree_apart_from_the_ignored_runtime_state(self):
        after = {rel: d for rel, d in self.tree_after_rollback.items()
                 if not rel.startswith(".gategraph/")}
        self.assertEqual(after, self.tree_before)

    def test_make_check_after_rollback_gives_the_same_result_as_before_the_migration(self):
        self.assertEqual(self.make_after, self.make_before)


class CurrentVersionProject(unittest.TestCase):
    """A project stamped at this template's own version has nothing to migrate. The command must
    say so, exit 0, and write nothing."""

    @classmethod
    def setUpClass(cls):
        base = _base()
        answers = base / "current-answers.yaml"
        answers.write_text(yaml.safe_dump(tm.ANSWERS))
        cls.project = base / "current" / PROJECT
        cls.project.parent.mkdir(parents=True)
        cls.init = tm.run([sys.executable, GATEGRAPH, "init", cls.project, "--answers", answers],
                          env={**os.environ, "FH_SKIP_STAMP_SELFCHECK": "1"})
        cls.tree_before = tm.tree_digests(cls.project)
        cls.commits_before = tm.commit_count(cls.project)
        cls.apply = migrate(cls.project, "--apply")

    def test_init_stamps_the_template_version(self):
        self.assertEqual(self.init.returncode, 0, report(self.init))
        stamp = json.loads((self.project / "harness/.fh/MANIFEST.json").read_text())
        self.assertEqual(stamp["version"], (TEMPLATE / "VERSION").read_text().strip())

    def test_apply_says_already_migrated_exits_zero_and_changes_nothing(self):
        self.assertEqual(self.apply.returncode, 0, report(self.apply))
        self.assertIn("already migrated", self.apply.stdout)
        self.assertEqual(tm.tree_digests(self.project), self.tree_before)
        self.assertEqual(tm.commit_count(self.project), self.commits_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
