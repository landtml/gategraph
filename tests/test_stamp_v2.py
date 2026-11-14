"""M2 tests: `gategraph.py init` stamps the schema-2 layout, and the stamped project enforces.

Contract under test:
- `init --defaults` produces a project where `make check` exits 0;
- the stamped `make check` runs the self-lint gating tier, `gate verify` over every unit,
  a policy scan over every unit, and selftest.py, which finishes in under three seconds;
- the full core test suite is not copied into the project (only selftest.py is);
- the hash manifest covers core and each selected pack separately;
- a three-gate unit walks to `complete` with the gate command;
- an approval on the token channel can be answered in a non-interactive walk, and an
  approval without its token is refused (no unauthenticated bypass).

Offline: the stamped project's venv is a bare venv whose site-packages point at the
template's locked `.venv` (no pip, no network). Without the template `.venv`, the tests that
need it are skipped with a message that says how to build it (`make venv`).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent
GATEGRAPH = TEMPLATE / "gategraph.py"
TEMPLATE_VENV = TEMPLATE / ".venv"
UNIT = "alphaunit"
TOKEN_RE = re.compile(r"approval token \(shown once, not stored\): (\S+)")


def account_number_like() -> str:
    """A string shaped like a account number, assembled at run time (never written to disk as a literal)."""
    return "".join(["010190", "-", "1234"])


def stamp(target: Path, answers: dict | None = None, extra: list[str] | None = None):
    """Run `gategraph.py init` with --defaults, or with a generated --answers file."""
    cmd = [sys.executable, str(GATEGRAPH), "init", str(target)]
    if answers is None:
        cmd.append("--defaults")
    else:
        answers_file = target.parent / f"{target.name}.answers.yaml"
        answers_file.write_text(yaml.safe_dump(answers))
        cmd += ["--answers", str(answers_file)]
    cmd += extra or []
    return subprocess.run(cmd, capture_output=True, text=True)


def require_template_venv() -> None:
    if not (TEMPLATE_VENV / "bin" / "python").exists():
        raise unittest.SkipTest("template .venv missing: run `make venv` in the template, "
                                "then re-run this test (offline: no pip is used here)")


def seed_venv(project: Path) -> Path:
    """Give the stamped project a venv without network: a bare venv whose site-packages file
    points at the template's locked site-packages, and the install stamp `make venv` checks."""
    venv = project / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True,
                   capture_output=True)
    purelib = subprocess.run(
        [str(TEMPLATE_VENV / "bin" / "python"), "-c",
         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True).stdout.strip()
    site = next(venv.glob("lib/python*/site-packages"))
    (site / "gategraph_template.pth").write_text(purelib + "\n")
    (venv / ".lock-installed").touch()  # newer than the stamped lock, so `make venv` is a no-op
    return venv / "bin" / "python"


def gate(project: Path, *args: str) -> subprocess.CompletedProcess:
    py = project / ".venv" / "bin" / "python"
    return subprocess.run([str(py), "harness/core/cli/gate.py", *args], cwd=project,
                          capture_output=True, text=True)


def make_check(project: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["make", "-s", "check"], cwd=project, capture_output=True, text=True)


def files_under(base: Path) -> set[str]:
    return {p.relative_to(base).as_posix() for p in base.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def write_unit_file(project: Path, slug: str) -> None:
    """One authored file in the unit: a gate over zero files is refused (the surface is empty)."""
    adapter = yaml.safe_load((project / "harness" / "adapter" / "adapter.yaml").read_text())
    unit = project / adapter["unit_dir"] / slug
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "note.md").write_text("an authored note\n")


class StampedDefaults(unittest.TestCase):
    """One defaults stamp, shared by the read-only assertions."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "scratchproj"
        cls.proc = stamp(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_B35_init_defaults_exits_zero(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout + self.proc.stderr)
        self.assertIn("selftest", self.proc.stdout)  # the stamp proves itself alive

    def test_core_stamped_without_core_tests_except_selftest(self):
        core = self.root / "harness" / "core"
        self.assertTrue((core / "cli" / "gate.py").is_file())
        self.assertTrue((core / "kernel" / "selflint.py").is_file())
        self.assertTrue((core / "hosts" / "shims" / "pretool_guard.py").is_file())
        self.assertTrue((core / "tests" / "selftest.py").is_file())
        self.assertTrue((core / "tests" / "run_checks.sh").is_file())
        self.assertTrue((core / "loop" / "cli.py").is_file(), "the legacy loop CLI stays stamped")
        extra = sorted(p.name for p in (core / "tests").glob("*")
                       if p.name not in ("selftest.py", "run_checks.sh") and p.name != "__pycache__")
        self.assertEqual(extra, [], "the full core test suite must not be copied")

    def test_manifest_covers_core_files_with_matching_hashes(self):
        manifest = json.loads((self.root / "harness" / ".fh" / "MANIFEST.json").read_text())
        on_disk = {f"harness/core/{rel}" for rel in files_under(self.root / "harness" / "core")}
        self.assertEqual(set(manifest["files"]), on_disk)
        for rel, digest in manifest["files"].items():
            actual = hashlib.sha256((self.root / rel).read_bytes()).hexdigest()
            self.assertEqual(digest, actual, rel)

    def test_manifest_has_no_packs_by_default(self):
        manifest = json.loads((self.root / "harness" / ".fh" / "MANIFEST.json").read_text())
        self.assertEqual(manifest.get("packs"), {})
        self.assertFalse((self.root / "packs").exists())

    def test_gitignore_covers_state_dir_and_venv(self):
        lines = (self.root / ".gitignore").read_text().splitlines()
        self.assertIn(".gategraph/", lines)
        self.assertIn(".venv/", lines)
        probe = subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q",
                                ".gategraph/state.db"], capture_output=True)
        self.assertEqual(probe.returncode, 0)

    def test_seed_names_no_legacy_gate_command(self):
        seed = TEMPLATE / "seed"
        for path in seed.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(errors="replace")
            for banned in ("gate.py approve", "--set-gate", "harness/core/gate.py",
                           "harness/core/lint.py", "gate_commands"):
                self.assertNotIn(banned, text, f"{path.name} still names {banned!r}")

    def test_B35_make_check_passes_on_fresh_stamp(self):
        require_template_venv()
        seed_venv(self.root)
        proc = make_check(self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_B35_make_check_runs_policies(self):
        require_template_venv()
        seed_venv(self.root)
        made = gate(self.root, "start", "--unit", UNIT)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        bad = self.root / "units" / UNIT / "notes.md"
        bad.write_text(f"Contact record: {account_number_like()}\n")
        failed = make_check(self.root)
        self.assertNotEqual(failed.returncode, 0, "policy scan must fail the build")
        self.assertIn("acct", failed.stdout + failed.stderr)
        bad.unlink()
        self.assertEqual(make_check(self.root).returncode, 0)

    def test_policy_scan_skips_the_gates_own_projections(self):
        # manifest.yaml holds digests and ids written from the chain. A digit run in one of
        # them is not authored content, so the scan must not fail a gate on it (a flake
        # observed in the walk test before this exclusion).
        require_template_venv()
        if not (self.root / ".venv" / "bin" / "python").exists():
            seed_venv(self.root)
        slug = "deltaunit"
        self.assertEqual(gate(self.root, "start", "--unit", slug).returncode, 0)
        manifest = self.root / "units" / slug / "manifest.yaml"
        manifest.write_text(manifest.read_text() + f"config_digest: {account_number_like()}\n")
        proc = subprocess.run([str(self.root / ".venv" / "bin" / "python"),
                               "harness/adapter/checks/policy_scan.py", "--unit", slug],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_B35_selftest_finishes_in_under_three_seconds(self):
        require_template_venv()
        py = self.root / ".venv" / "bin" / "python"
        if not py.exists():
            seed_venv(self.root)
        started = time.monotonic()
        proc = subprocess.run([str(py), "harness/core/tests/selftest.py"], cwd=self.root,
                              capture_output=True, text=True)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertLess(elapsed, 3.0, f"selftest took {elapsed:.2f}s")

    def test_three_gate_unit_walks_to_complete(self):
        require_template_venv()
        if not (self.root / ".venv" / "bin" / "python").exists():
            seed_venv(self.root)
        slug = "betaunit"
        self.assertEqual(gate(self.root, "start", "--unit", slug).returncode, 0)
        write_unit_file(self.root, slug)
        for name in ("composed", "checked", "verified"):
            done = gate(self.root, "attempt", name, "--unit", slug)
            self.assertEqual(done.returncode, 0, name + ": " + done.stdout + done.stderr)
        status = gate(self.root, "status", "--unit", slug)
        self.assertIn("complete", status.stdout, status.stdout + status.stderr)


class StampedWithPack(unittest.TestCase):
    """A stamp that selects one domain pack: the manifest covers it separately."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "packproj"
        cls.proc = stamp(cls.root, {"project": "packproj", "packs": ["document"]})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_pack_stamped_and_listed(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout + self.proc.stderr)
        adapter = yaml.safe_load((self.root / "harness" / "adapter" / "adapter.yaml").read_text())
        self.assertEqual(adapter["packs"], ["document"])
        self.assertTrue((self.root / "packs" / "document" / "pack.yaml").is_file())

    def test_manifest_covers_selected_pack_separately(self):
        manifest = json.loads((self.root / "harness" / ".fh" / "MANIFEST.json").read_text())
        pack_files = {rel for rel in files_under(self.root / "packs" / "document")
                      if not rel.startswith("examples/")}
        self.assertEqual(set(manifest["packs"]["document"]["files"]), pack_files)
        for rel, digest in manifest["packs"]["document"]["files"].items():
            actual = hashlib.sha256((self.root / "packs" / "document" / rel).read_bytes())
            self.assertEqual(digest, actual.hexdigest(), rel)
        self.assertTrue(all(not k.startswith("packs/") for k in manifest["files"]))

    def test_config_loads_pack_checks(self):
        code = ("import sys; sys.path.insert(0, 'harness'); from core.kernel import config; "
                "cfg = config.load('.'); print(' '.join(c.id for c in cfg.checks))")
        proc = subprocess.run([sys.executable, "-c", code], cwd=self.root,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("required-sections", proc.stdout)


class StampedTokenApproval(unittest.TestCase):
    """Approvals on the token channel: the walk answers them without a terminal."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "tokproj"
        cls.proc = stamp(cls.root, {"project": "tokproj", "gates": ["composed", "approved"],
                                    "approval_gates": ["approved"],
                                    "approval_channel": "token"})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_token_walk_approves_and_refuses_without_token(self):
        require_template_venv()
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout + self.proc.stderr)
        seed_venv(self.root)
        slug = "gammaunit"
        self.assertEqual(gate(self.root, "start", "--unit", slug).returncode, 0)
        write_unit_file(self.root, slug)
        self.assertEqual(gate(self.root, "attempt", "composed", "--unit", slug).returncode, 0)
        raised = gate(self.root, "attempt", "approved", "--unit", slug)
        found = TOKEN_RE.search(raised.stdout)
        self.assertIsNotNone(found, raised.stdout + raised.stderr)
        token = found.group(1)
        refused = gate(self.root, "proceed", "--unit", slug, "--decision", "approve",
                       "--by", "tester")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("token", refused.stderr)
        # `--token=` form: a token may begin with "-" (token_urlsafe), which argparse would
        # otherwise read as an option when it is given as a separate argument.
        answered = gate(self.root, "proceed", "--unit", slug, "--decision", "approve",
                        "--by", "tester", f"--token={token}")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        self.assertIn("complete", gate(self.root, "status", "--unit", slug).stdout)


class HelpText(unittest.TestCase):
    """Item 5: the stamper's help names the current generated-section marker (core/brand.py)."""

    def test_help_names_the_current_gen_marker(self):
        sys.path.insert(0, str(TEMPLATE))
        from core import brand
        out = subprocess.run([sys.executable, str(GATEGRAPH), "--help"], capture_output=True,
                             text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        text = out.stdout.replace("\n", " ")
        self.assertIn(brand.GEN_MARKER, " ".join(text.split()))
        for legacy in brand.LEGACY_GEN_MARKERS:
            self.assertNotIn(legacy, out.stdout, "the help still names a legacy marker")


class TemplateSelftest(unittest.TestCase):
    """The same selftest runs in the template (system python3: stdlib and PyYAML only)."""

    def test_selftest_passes_in_template(self):
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(TEMPLATE / "core" / "tests" / "selftest.py")],
                              cwd=TEMPLATE, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertLess(time.monotonic() - started, 3.0)


if __name__ == "__main__":
    unittest.main()
