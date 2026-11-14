"""M4 tests: `gategraph.py migrate <dir>` moves a stamped version-1 project to the schema-2 layout.

Contract: the eight steps of `gategraph migrate` (core/cli/migrate.py) and their invariants.

The fixture is built here, in a temporary directory, by stamping the template's `legacy-0.4` tag.
No real project is read or written. The fixture is recorded as 0.4.0-dev, which is what that
tag's VERSION file says.

The fixture has a hashed gate with real files: `checked` is earned through the legacy
`lint --set-gate` flow, run from the fixture's own stamped legacy core. So the hashed-surface
parity of step 8 is exercised, not only approval gates.

Run alone: ``python3 tests/test_migrate.py``.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TEMPLATE))
GATEGRAPH = TEMPLATE / "gategraph.py"
LEGACY_TAG = "legacy-0.4"
TEMPLATE_VENV = TEMPLATE / ".venv"
PROJECT = "migproj"
HASHED = ("checked",)
# gate -> how the legacy flow earned it: approve (by hand) or lint --set-gate (tool-set, hashed)
UNITS = {"fixture-mid": ["composed"], "fixture-done": ["composed", "checked", "verified"], "fixture-esc": []}
ANSWERS = {"project": PROJECT, "artifact_noun": "note", "unit_dir": "notes",
           "gates": ["composed", "checked", "verified"], "hashed_gates": list(HASHED),
           "seed_deny": True, "loop_enabled": False}
V1_CHECK = """\
import sys
sys.path.insert(0, "harness")
from core import config, manifest  # version-1 modules, through the legacy names
assert hasattr(manifest, "Manifest") and hasattr(config, "load")
"""
_FIXTURE = {}


def run(cmd, cwd=None, env=None):
    return subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                          env=env)


def tree_digests(base: Path) -> dict:
    """{relative path: sha256} of every file under base, git metadata and venv excluded."""
    out = {}
    for path in base.rglob("*"):
        rel = path.relative_to(base)
        if not path.is_file() or rel.parts[0] in (".git", ".venv") or "__pycache__" in rel.parts:
            continue
        out[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def git(project: Path, *args) -> str:
    done = run(["git", "-C", project, "-c", "user.name=t", "-c", "user.email=t@localhost", *args])
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def commit_count(project: Path) -> int:
    return int(git(project, "rev-list", "--count", "HEAD").strip())


def legacy(project: Path, *args) -> subprocess.CompletedProcess:
    return run([sys.executable, "harness/core/gate.py", *args], cwd=project)


def passed_from_legacy_status(text: str) -> list:
    """The gate names on the `gates passed:` line of a legacy `gate.py status` report."""
    for line in text.splitlines():
        if line.strip().startswith("gates passed:"):
            names = line.split(":", 1)[1].strip()
            return [] if names == "(none)" else [n.strip() for n in names.split(",")]
    raise AssertionError(f"no `gates passed:` line in:\n{text}")


def venv_python(project: Path) -> Path:
    return project / ".venv" / "bin" / "python"


def seed_venv(project: Path) -> None:
    """A venv that reuses the template's locked site-packages. Offline: no pip, no network.
    The stamp file is the one `make venv` writes, so `gategraph migrate` treats it as built."""
    venv = project / ".venv"
    subprocess.run([TEMPLATE_VENV / "bin" / "python", "-m", "venv", "--without-pip", str(venv)],
                   check=True, capture_output=True)
    purelib = subprocess.run([str(TEMPLATE_VENV / "bin" / "python"), "-c",
                              "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                             capture_output=True, text=True, check=True).stdout.strip()
    site = next(venv.glob("lib/python*/site-packages"))
    (site / "template.pth").write_text(purelib + "\n")
    lock = hashlib.sha256(TEMPLATE.joinpath("core", "requirements.lock").read_bytes()).hexdigest()
    (venv / ".lock-installed").write_text(lock + "\n")


def build_fixture(base: Path) -> Path:
    """A version-1 project stamped from legacy-0.4, with two version-1 adapter checks, one local
    core edit, a hashed gate with real files, and three units: mid-chain, complete and escalated.
    The state is committed; the venv is seeded and excluded from git."""
    template = base / "legacy_template"
    template.mkdir()
    archive = subprocess.run(["git", "-C", TEMPLATE, "archive", LEGACY_TAG],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        tf.extractall(template)
    answers = base / "answers.yaml"
    answers.write_text(yaml.safe_dump(ANSWERS))
    project = base / PROJECT
    # The tag is immutable and predates the rename, so its launcher is found, not named.
    (launcher,) = [f for f in template.glob("*.py")]
    done = run([sys.executable, launcher, "init", project, "--answers", answers],
               env={**os.environ, "FH_SKIP_STAMP_SELFCHECK": "1"})
    assert done.returncode == 0, done.stdout + done.stderr

    adapter_path = project / "harness" / "adapter" / "adapter.yaml"
    adapter = yaml.safe_load(adapter_path.read_text())
    adapter["checks"] = [
        {"id": f"v1-import-{n}", "cmd": f"python3 harness/adapter/checks/v1_import_{n}.py",
         "on": ["notes/**"], "severity": "fatal"}
        for n in ("a", "b")]
    adapter_path.write_text(yaml.safe_dump(adapter, sort_keys=False))
    for n in ("a", "b"):
        (project / "harness" / "adapter" / "checks" / f"v1_import_{n}.py").write_text(V1_CHECK)

    hook = project / "harness" / "core" / "hooks" / "_common.py"
    hook.write_text(hook.read_text() + "\n# LOCAL EDIT: host plumbing for a second host\n")

    # Legacy `start` refuses while another unit is active, so each earlier unit is paused (or
    # escalated, which is terminal) before the next one starts. The complete unit comes last.
    for slug in ("fixture-mid", "fixture-esc", "fixture-done"):
        assert legacy(project, "start", "--unit", slug).returncode == 0, slug
        if slug == "fixture-done":
            (project / "notes" / slug / "note.md").write_text("a short note\n")
        for gate in UNITS[slug]:
            if gate in HASHED:  # tool-set: earned by lint, which records the sha256 map
                done = run([sys.executable, "harness/core/lint.py", "--unit", slug,
                            "--set-gate", gate], cwd=project)
            else:
                done = legacy(project, "approve", gate, "--unit", slug, "--by", "tester")
            assert done.returncode == 0, done.stdout + done.stderr
        if slug == "fixture-esc":
            done = legacy(project, "escalate", "--unit", slug, "--note", "fixture-esc")
        elif slug == "fixture-mid":
            done = legacy(project, "pause", "--unit", slug)
        else:
            continue
        assert done.returncode == 0, done.stdout + done.stderr

    seed_venv(project)
    git(project, "init", "-b", "main")
    (project / ".git" / "info" / "exclude").write_text(".venv/\n")
    git(project, "add", "-A")
    git(project, "commit", "-m", "fixture: version-1 project")
    return project


class MigrateFixture(unittest.TestCase):
    """The built fixture is shared; each test works on its own copy, so no test sees another's
    writes. The legacy status of every unit is recorded before anything is migrated."""

    @classmethod
    def setUpClass(cls):
        if LEGACY_TAG not in git(TEMPLATE, "tag", "--list", LEGACY_TAG).split():
            raise unittest.SkipTest(f"tag {LEGACY_TAG} is missing; the fixture cannot be built")
        if not (TEMPLATE_VENV / "bin" / "python").exists():
            raise unittest.SkipTest("template .venv missing: run `make venv` in the template")
        if "fixture" not in _FIXTURE:
            _FIXTURE["base"] = tempfile.mkdtemp(prefix="migrate-test-")
            _FIXTURE["fixture"] = build_fixture(Path(_FIXTURE["base"]))
        cls.base = Path(_FIXTURE["base"])
        cls.fixture = _FIXTURE["fixture"]
        cls.status_before = {slug: legacy(cls.fixture, "status", "--unit", slug).stdout
                             for slug in UNITS}

    def copy(self, name: str) -> Path:
        dest = self.base / name / PROJECT
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.fixture, dest, symlinks=True)
        return dest

    @staticmethod
    def migrate(project: Path, *extra) -> subprocess.CompletedProcess:
        return run([sys.executable, GATEGRAPH, "migrate", project, *extra])


def tearDownModule():
    if _FIXTURE.get("base"):
        shutil.rmtree(_FIXTURE["base"], ignore_errors=True)


class TestFixture(MigrateFixture):
    def test_fixture_has_a_hashed_gate_with_real_files(self):
        # R8: `checked` was earned by the legacy lint flow, so its record carries a sha256 map.
        manifest = yaml.safe_load((self.fixture / "notes" / "fixture-done" / "manifest.yaml").read_text())
        self.assertEqual(set(manifest["gates"]["checked"]["sha256"]), {"note.md"})

    def test_fixture_is_stamped_at_the_legacy_tag_and_recorded_as_0_4_dev(self):
        # R2: built from legacy-0.4; its stamp says 0.4.0-dev, not 0.2.0.
        stamp = json.loads((self.fixture / "harness" / ".fh" / "MANIFEST.json").read_text())
        self.assertEqual(stamp["version"], "0.4.0-dev")


class TestDryRunAndRefusals(MigrateFixture):
    def test_dirty_tree_is_refused_without_allow_dirty(self):
        project = self.copy("dirty")
        (project / "scratch.txt").write_text("uncommitted\n")
        before = tree_digests(project)
        done = self.migrate(project, "--apply")
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("--allow-dirty", done.stdout + done.stderr)
        self.assertEqual(tree_digests(project), before)

    def test_dry_run_is_the_default_and_changes_no_file(self):
        project = self.copy("dry")
        before, commits = tree_digests(project), commit_count(project)
        done = self.migrate(project)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(tree_digests(project), before, "a dry run must change no file")
        self.assertEqual(commit_count(project), commits)
        self.assertIn("dry run", done.stdout)


class TestApply(MigrateFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.base / "applied" / PROJECT
        cls.project.parent.mkdir(parents=True)
        shutil.copytree(cls.fixture, cls.project, symlinks=True)
        cls.unit_before = {slug: tree_digests(cls.project / "notes" / slug) for slug in UNITS}
        cls.adapter_before = (cls.project / "harness/adapter/adapter.yaml").read_text()
        cls.core_edit_before = (cls.project / "harness/core/hooks/_common.py").read_text()
        cls.makefile_before = (cls.project / "Makefile").read_bytes()
        cls.settings_before = json.loads((cls.project / ".claude/settings.json").read_text())
        cls.commits_before = commit_count(cls.project)
        cls.head_before = git(cls.project, "rev-parse", "HEAD").strip()
        cls.done = cls.migrate(cls.project, "--apply")
        cls.commits_after = commit_count(cls.project)

    def test_apply_exits_zero_and_makes_exactly_one_commit(self):
        self.assertEqual(self.done.returncode, 0, self.done.stdout + self.done.stderr)
        self.assertEqual(self.commits_after, self.commits_before + 1)

    def test_effective_gates_of_every_unit_are_identical(self):
        for slug in UNITS:
            expected = passed_from_legacy_status(self.status_before[slug])
            manifest = yaml.safe_load((self.project / "notes" / slug / "manifest.yaml").read_text())
            self.assertEqual(manifest["effective"], expected, slug)

    def test_gate_verify_step_3_passes_for_every_unit(self):
        # R6: each thread was created through the graph, so the checkpoint the manifest names
        # is read back and matches evidence.jsonl. Verified with the project's own venv.
        for slug in UNITS:
            verify = run([venv_python(self.project), "harness/core/cli/gate.py", "verify",
                          "--unit", slug], cwd=self.project)
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertIn("step 3 checkpoint: pass", verify.stdout)

    def test_import_records_are_the_only_records_and_carry_the_migration_actor(self):
        lines = (self.project / "notes" / "fixture-done" / "evidence.jsonl").read_text().splitlines()
        records = [json.loads(line) for line in lines]
        self.assertEqual([r["kind"] for r in records], ["import", "import", "import"])
        self.assertEqual({r["actor"]["id"] for r in records}, {"migrate"})
        self.assertEqual([r["gate"] for r in records], UNITS["fixture-done"])

    def test_terminal_and_paused_statuses_are_kept(self):
        stuck = yaml.safe_load((self.project / "notes" / "fixture-esc" / "manifest.yaml").read_text())
        done = yaml.safe_load((self.project / "notes" / "fixture-done" / "manifest.yaml").read_text())
        mid = yaml.safe_load((self.project / "notes" / "fixture-mid" / "manifest.yaml").read_text())
        self.assertEqual(stuck["status"], "escalated")
        self.assertEqual(done["status"], "complete")
        self.assertEqual(mid["status"], "paused")  # a paused unit is not handed to the stop hook

    def test_only_manifest_and_evidence_change_inside_unit_directories(self):
        # Scope: files inside a unit directory (notes/<slug>/...). notes/.active is not one.
        changed = git(self.project, "diff", "--name-only", self.head_before, "HEAD", "--",
                      *[f"notes/{slug}" for slug in UNITS])
        self.assertTrue(changed.strip(), "the migration should have rewritten the projections")
        for name in changed.split():
            self.assertIn(Path(name).name, {"manifest.yaml", "evidence.jsonl"}, name)
        for slug in UNITS:
            after = tree_digests(self.project / "notes" / slug)
            for rel, digest in self.unit_before[slug].items():
                if Path(rel).name in ("manifest.yaml", "evidence.jsonl"):
                    continue
                self.assertEqual(after.get(rel), digest, f"{slug}/{rel} changed")

    def test_modified_core_file_is_reported_and_kept_beside_the_new_one(self):
        local = self.project / "harness/core/hooks/_common.py.local"
        self.assertTrue(local.exists(), "the local core edit must be kept as .local")
        self.assertEqual(local.read_text(), self.core_edit_before)
        self.assertIn("_common.py", self.done.stdout)
        # R7: the report names the host seam and where a second host is selected.
        self.assertIn("HOSTS", self.done.stdout)
        self.assertIn("core/hosts/bridge.py", self.done.stdout)

    def test_old_adapter_is_kept_as_adapter_v1_yaml(self):
        kept = self.project / "harness/adapter/adapter.v1.yaml"
        self.assertEqual(kept.read_text(), self.adapter_before)
        adapter = yaml.safe_load((self.project / "harness/adapter/adapter.yaml").read_text())
        self.assertEqual(adapter.get("schema"), 2)

    def test_adapter_is_schema_2_with_unit_placeholders_and_loads(self):
        # R4: the schema-2 adapter uses {unit}, never {slug}, and the schema-2 loader accepts it.
        text = (self.project / "harness/adapter/adapter.yaml").read_text()
        self.assertNotIn("{slug}", text)
        self.assertIn("{unit}", text)
        load = run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
                    "from core.kernel import config; config.load(Path(sys.argv[2]))",
                    str(self.project / "harness"), str(self.project)], cwd=self.project)
        self.assertEqual(load.returncode, 0, load.stdout + load.stderr)

    def test_hook_registrations_point_at_the_shims_with_the_same_events_and_matchers(self):
        # R5: same events and matchers; only the file path moves to the host shims.
        settings = json.loads((self.project / ".claude/settings.json").read_text())
        before = self.settings_before["hooks"]
        self.assertEqual(set(settings["hooks"]), set(before))
        for event, groups in before.items():
            self.assertEqual([g.get("matcher") for g in settings["hooks"][event]],
                             [g.get("matcher") for g in groups], event)
        commands = [h["command"] for groups in settings["hooks"].values()
                    for g in groups for h in g["hooks"]]
        self.assertTrue(commands)
        for command in commands:
            self.assertIn("harness/core/hosts/shims/", command)
            self.assertNotIn("harness/core/hooks/", command)
        self.assertEqual(sorted(p.name for p in (self.project / "harness/core/hooks").glob("*.py")),
                         [], "no legacy hook file is created")

    def test_make_check_of_the_migrated_project(self):
        # Item 10 (R9): the project's own Makefile, never edited, runs its `check` to exit 0. The
        # legacy check path is served by the stamped shims: harness_lint, run_checks.sh, loop/cli.py.
        self.assertEqual((self.project / "Makefile").read_bytes(), self.makefile_before)
        check = run(["make", "-s", "check"], cwd=self.project)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertFalse((self.project / "Makefile.v2").exists(),
                         "Makefile.v2 is written only when the Makefile's check fails")
        self.assertTrue((self.project / "harness/core/tests/run_checks.sh").is_file())
        self.assertTrue((self.project / "harness/core/loop/cli.py").is_file())

    def test_running_migrate_again_is_a_no_op(self):
        before, commits = tree_digests(self.project), commit_count(self.project)
        done = self.migrate(self.project, "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(tree_digests(self.project), before)
        self.assertEqual(commit_count(self.project), commits)


class TestHashedLintGateAfterMigration(MigrateFixture):
    """Item 9. The legacy `lint.py --set-gate checked` gate becomes the policy check plus the
    adapter checks. `lint.py --unit` refuses, so a translation that ran it could never be earned."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.base / "reearn" / PROJECT
        cls.project.parent.mkdir(parents=True)
        shutil.copytree(cls.fixture, cls.project, symlinks=True)
        cls.done = cls.migrate(cls.project, "--apply")

    def attempt(self, gate: str, unit: str = "fixture-mid") -> subprocess.CompletedProcess:
        return run([venv_python(self.project), "harness/core/cli/gate.py", "attempt", gate,
                    "--unit", unit], cwd=self.project)

    def test_migrated_lint_gate_runs_policy_and_adapter_checks_not_lint(self):
        self.assertEqual(self.done.returncode, 0, self.done.stdout + self.done.stderr)
        adapter = yaml.safe_load((self.project / "harness/adapter/adapter.yaml").read_text())
        checks = {c["id"]: c["command"] for c in adapter["checks"]}
        self.assertEqual(checks["policy"], "python3 harness/adapter/checks/policy_scan.py --unit {unit}")
        self.assertFalse([c for c in checks.values() if "lint.py" in c], checks)
        gate = next(g for g in adapter["gates"] if g["id"] == "checked")
        self.assertEqual(gate["checks"], ["policy", "v1-import-a", "v1-import-b"])
        self.assertTrue((self.project / "harness/adapter/checks/policy_scan.py").is_file())

    def test_migrated_hashed_gate_is_earned_by_gate_attempt(self):
        (self.project / "notes" / "fixture-mid" / "note.md").write_text("a first sketch\n")
        earned = self.attempt("checked")
        self.assertEqual(earned.returncode, 0, earned.stdout + earned.stderr)
        self.assertIn("gate checked passed for fixture-mid", earned.stdout)

    def test_hashed_gate_is_re_earned_by_gate_attempt_after_a_file_edit(self):
        project = self.base / "reedit" / PROJECT
        project.parent.mkdir(parents=True)
        shutil.copytree(self.fixture, project, symlinks=True)
        self.assertEqual(self.migrate(project, "--apply").returncode, 0)
        note = project / "notes" / "fixture-mid" / "note.md"
        note.write_text("a first sketch\n")
        self.assertEqual(run([venv_python(project), "harness/core/cli/gate.py", "attempt",
                              "checked", "--unit", "fixture-mid"], cwd=project).returncode, 0)
        note.write_text("edited after the gate was earned\n")
        again = run([venv_python(project), "harness/core/cli/gate.py", "attempt", "checked",
                     "--unit", "fixture-mid"], cwd=project)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)


class TestMakefileV2Fallback(MigrateFixture):
    """Item 10 fallback: when the project's own `check` fails after the migration, Makefile.v2 is
    written beside the Makefile (the Makefile itself is never edited), and it must pass."""

    def test_failing_make_check_writes_makefile_v2_and_keeps_the_makefile(self):
        project = self.copy("fallback")
        broken = ".PHONY: check\ncheck:\n\tfalse\n"
        (project / "Makefile").write_text(broken)
        git(project, "add", "-A")
        git(project, "commit", "-m", "fixture: a Makefile whose check fails")
        done = self.migrate(project, "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Makefile.v2", done.stdout)
        self.assertEqual((project / "Makefile").read_text(), broken, "the Makefile is never edited")
        v2 = run(["make", "-s", "-f", "Makefile.v2", "check"], cwd=project)
        self.assertEqual(v2.returncode, 0, v2.stdout + v2.stderr)


class TestRollbackEndToEnd(MigrateFixture):
    """Item 11: the step-8 rollback, end to end. A surface changes after the snapshot and before
    verify, so effective gates differ; migrate must put the state directories, adapter.yaml and
    settings.json back byte for byte, make no commit, and exit non-zero."""

    def test_step_8_mismatch_restores_state_adapter_and_settings_byte_identically(self):
        from unittest import mock
        sys.path.insert(0, str(TEMPLATE))
        from core.cli import migrate
        project = self.copy("rollback")
        adapter_before = (project / "harness/adapter/adapter.yaml").read_bytes()
        settings_before = (project / ".claude/settings.json").read_bytes()
        state_before = tree_digests(project / "harness/state")
        tree_before = tree_digests(project)
        commits_before = commit_count(project)
        head_before = git(project, "rev-parse", "HEAD").strip()
        real_gen_docs = migrate._gen_docs

        def tamper_then_gen(project_path, journal, plan):
            # The surface of the hashed gate changes after the snapshot; step 8 must notice.
            (project_path / "notes" / "fixture-done" / "note.md").write_text("tampered\n")
            return real_gen_docs(project_path, journal, plan)

        with mock.patch.object(migrate, "_gen_docs", side_effect=tamper_then_gen):
            code, plan = migrate.run(project, apply=True)
        text = "\n".join(plan.lines)
        self.assertNotEqual(code, 0, text)
        self.assertIn("ROLLED BACK", text)
        self.assertEqual((project / "harness/adapter/adapter.yaml").read_bytes(), adapter_before)
        self.assertEqual((project / ".claude/settings.json").read_bytes(), settings_before)
        self.assertEqual(tree_digests(project / "harness/state"), state_before)
        self.assertFalse((project / ".gategraph").exists(), "the new state dir was not there before")
        self.assertFalse((project / "harness/adapter/adapter.v1.yaml").exists())
        self.assertEqual(commit_count(project), commits_before)
        self.assertEqual(git(project, "rev-parse", "HEAD").strip(), head_before)
        # The tampered file is the user's own artifact, not a migration write: the rollback does
        # not undo it. Every other file of the project is back to its byte-identical state.
        self.assertEqual((project / "notes/fixture-done/note.md").read_text(), "tampered\n")
        after = tree_digests(project)
        for rel, digest in tree_before.items():
            if rel == "notes/fixture-done/note.md":
                continue
            self.assertEqual(after.get(rel), digest, f"{rel} was not restored")
        self.assertEqual(set(after) - set(tree_before), set(), "the rollback left new files")


class TestStopAndProceed(MigrateFixture):
    """Step 5 with no interpreter for the graph: the migration stops after step 4 with the tree
    consistent and committed; the re-run completes the units from the step-1 snapshot."""

    def test_stop_after_step_4_then_proceed_completes_the_units(self):
        from unittest import mock
        sys.path.insert(0, str(TEMPLATE))
        from core.cli import migrate
        project = self.copy("proceed")
        with mock.patch.object(migrate, "_graph_python", return_value=None):
            code, plan = migrate.run(project, apply=True)
        self.assertEqual(code, 0, "\n".join(plan.lines))
        self.assertIn("stopped after step 4", "\n".join(plan.lines))
        adapter = yaml.safe_load((project / "harness/adapter/adapter.yaml").read_text())
        self.assertEqual(adapter["schema"], 2)
        legacy_manifest = yaml.safe_load((project / "notes/fixture-done/manifest.yaml").read_text())
        self.assertEqual(legacy_manifest.get("schema", 1), 1, "units wait for the environment")
        self.assertTrue((project / ".gategraph" / "migrate" / "snapshot.json").is_file())
        self.assertIsNone(git_status_of_snapshot(project))

        code, plan = migrate.run(project, apply=True)
        self.assertEqual(code, 0, "\n".join(plan.lines))
        for slug in UNITS:
            expected = passed_from_legacy_status(self.status_before[slug])
            manifest = yaml.safe_load((project / "notes" / slug / "manifest.yaml").read_text())
            self.assertEqual(manifest["effective"], expected, slug)


def git_status_of_snapshot(project: Path):
    """None when the snapshot is not in git (the state directory is ignored)."""
    done = run(["git", "-C", project, "ls-files", "--", ".gategraph"])
    return done.stdout.strip() or None


class TestJournalAndDecisions(unittest.TestCase):
    """In-process checks of the rollback journal and the step-8 comparison."""

    def test_journal_puts_back_every_path_it_touched(self):
        from core.cli import migrate
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kept.txt").write_text("original\n")
            journal = migrate.Journal(root)
            journal.text("kept.txt", "changed\n")
            journal.text("new/created.txt", "new\n")
            journal.remove("kept.txt")
            journal.rollback()
            self.assertEqual((root / "kept.txt").read_text(), "original\n")
            self.assertFalse((root / "new").exists())

    def test_effective_gate_difference_is_reported(self):
        from core.cli import migrate
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(yaml.safe_dump({"effective": ["composed"]}))
            plan = migrate.Plan()
            same = migrate._check_effective({"u": manifest}, {"u": {"passed": ["composed"]}},
                                            Path(tmp), plan)
            self.assertEqual(same, [])
            differing = migrate._check_effective({"u": manifest},
                                                 {"u": {"passed": ["composed", "checked"]}},
                                                 Path(tmp), migrate.Plan())
            self.assertEqual(len(differing), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
