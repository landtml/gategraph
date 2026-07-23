"""Boundary-hardening tests, plus the T9 fuzz fixture.

The contract under test: every CLI/hook boundary answers invalid input with a
typed refusal, never a traceback, never a silent pass, never silent evidence
loss. The fuzz classes are seeded and deterministic; per the T9 calibration
rule they were watched RED on the unfixed tree (F18/F28 among others) before
the fixes landed.
"""
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402
from fixtures import (ACCT_TOKEN, default_adapter, mkroot, pre_event,  # noqa: E402
                      run_cli, run_hook, write_adapter)

from core import config, gen, harness_lint, memory  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def gate(self, *args, **kw):
        return run_cli("gate.py", *args, root=self.root, **kw)

    def lint(self, *args, **kw):
        return run_cli("lint.py", *args, root=self.root, **kw)

    def manifest_data(self, slug="u1") -> dict:
        return yaml.safe_load(
            (self.root / "units" / slug / "manifest.yaml").read_text())

    def assert_no_traceback(self, proc):
        self.assertNotIn("Traceback", proc.stderr,
                         f"raw traceback leaked:\n{proc.stderr}")
        self.assertNotIn("Traceback", proc.stdout,
                         f"raw traceback leaked:\n{proc.stdout}")


# ── F18: a deny policy without `id` must not disable enforcement ────────────
class TestDenyPolicyId(Base):
    def _idless_adapter(self):
        adapter = default_adapter()
        adapter["policies"]["deny"] = [
            {"regex": r"\d{6}\s*-?\s*\d{4}", "summary": "no account number"}]
        return adapter

    def test_config_rejects_idless_deny_policy(self):
        write_adapter(self.root, self._idless_adapter())
        with self.assertRaises(config.ConfigError) as ctx:
            config.load(self.root)
        self.assertIn("id", str(ctx.exception))

    def test_pretool_fail_closes_on_idless_policy(self):
        # End to end: the exact P4-audit repro. An id-less policy must DENY the
        # write (fail-closed), not silently allow it.
        write_adapter(self.root, self._idless_adapter())
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "doc.md"),
                        "content": f"id: {ACCT_TOKEN}"})
        r = run_hook("pretool_guard.py", ev, self.root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("deny", r.stdout,
                      f"ACCT write was ALLOWED under a broken policy: {r.stdout!r}")


# ── F19: a check that cannot run must not let the gate be earned ────────────
class TestCheckExitContract(Base):
    def _walk_to_checked(self, checks):
        adapter = default_adapter()
        adapter["checks"] = checks
        write_adapter(self.root, adapter)
        self.gate("start", "--unit", "u1", "--session-id", "S1")
        (self.root / "units/u1/note.md").write_text("a clean research note\n")
        self.gate("approve", "composed", "--unit", "u1")
        self.gate("approve", "checked", "--unit", "u1")

    def test_missing_binary_is_fatal_and_blocks_gate(self):
        self._walk_to_checked([{
            "id": "broken-tool",
            "cmd": "this_binary_does_not_exist_fh_xyz {files}",
            "on": ["units/**/*.md"], "severity": "fatal"}])
        r = self.lint("--unit", "u1", "--set-gate", "verified")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("did not run", r.stdout + r.stderr)
        self.assertNotIn("verified", self.manifest_data()["gates"])

    def test_exit_2_is_warn_and_gate_sets(self):
        # Pin the documented contract: exit 2 = warn findings, advisory
        # exit (1), gate still earned.
        self._walk_to_checked([{
            "id": "warny", "cmd": "sh -c 'echo advisory-detail >&2; exit 2'",
            "on": ["units/**/*.md"]}])
        r = self.lint("--unit", "u1", "--set-gate", "verified")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("verified", self.manifest_data()["gates"])

    def test_exit_127_and_signal_do_not_grant_gate(self):
        self._walk_to_checked([{
            "id": "self-kill", "cmd": "sh -c 'kill -9 $$'",
            "on": ["units/**/*.md"]}])
        r = self.lint("--unit", "u1", "--set-gate", "verified")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("verified", self.manifest_data()["gates"])

    def test_severity_warn_demotes_exit_1(self):
        # The seed schema documents checks[].severity: fatal|warn, honor it:
        # a warn-severity check may report findings (exit 1) without blocking.
        self._walk_to_checked([{
            "id": "advisory-check", "cmd": "sh -c 'echo style nit; exit 1'",
            "on": ["units/**/*.md"], "severity": "warn"}])
        r = self.lint("--unit", "u1", "--set-gate", "verified")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("verified", self.manifest_data()["gates"])


# ── F22: `start` must never wipe earned evidence ────────────────────────────
class TestStartIdempotent(Base):
    def test_restart_preserves_gates(self):
        self.gate("start", "--unit", "u1", "--session-id", "S1")
        self.gate("approve", "composed", "--unit", "u1")
        before = self.manifest_data()["gates"]
        self.assertIn("composed", before)
        r = self.gate("start", "--unit", "u1", "--session-id", "S2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("composed", self.manifest_data()["gates"],
                      "re-running start destroyed earned gate evidence")
        # and it must say what it did instead of pretending a fresh start
        self.assertIn("proceed", (r.stdout + r.stderr).lower())

    def test_start_on_terminal_unit_refused(self):
        self.gate("start", "--unit", "u1", "--session-id", "S1")
        self.gate("escalate", "--unit", "u1", "--note", "stuck")
        r = self.gate("start", "--unit", "u1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("escalated", r.stdout + r.stderr)
        self.assertEqual(self.manifest_data()["status"], "escalated")


# ── F23: gatebench gen must never silently delete content ───────────────────
class TestGenMarkerSafety(Base):
    UNCLOSED = ("# Doc\n\n"
                "<!-- fh:gen gate-table -->\n"
                "stale table\n\n"
                "## Hand-written section\n\n"
                "important deploy process prose\n\n"
                "<!-- fh:gen gate-table -->\n"
                "second table\n"
                "<!-- /fh:gen -->\n")

    def _cfg(self):
        return config.load(self.root)

    def test_unclosed_marker_refused(self):
        with self.assertRaises(gen.GenError):
            gen.regenerate_text(self.UNCLOSED, self._cfg())

    def test_regenerate_project_writes_nothing_on_bad_doc(self):
        bad = self.root / "docs"
        bad.mkdir(exist_ok=True)
        (bad / "bad.md").write_text(self.UNCLOSED)
        good = self.root / "docs" / "good.md"
        good.write_text("<!-- fh:gen gate-table -->\nstale\n<!-- /fh:gen -->\n")
        good_before = good.read_text()
        with self.assertRaises(gen.GenError) as ctx:
            gen.regenerate_project(self.root, self._cfg())
        self.assertIn("bad.md", str(ctx.exception))
        self.assertEqual((bad / "bad.md").read_text(), self.UNCLOSED,
                         "gen mutated a malformed doc")
        self.assertEqual(good.read_text(), good_before,
                         "gen wrote other docs before refusing, must pre-scan")

    def test_nested_pair_refused(self):
        nested = ("<!-- fh:gen gate-table -->\nouter\n"
                  "<!-- fh:gen gate-table -->\ninner\n<!-- /fh:gen -->\n"
                  "<!-- /fh:gen -->\n")
        with self.assertRaises(gen.GenError):
            gen.regenerate_text(nested, self._cfg())

    def test_dup_canon_flags_unbalanced_markers(self):
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "bad.md").write_text(self.UNCLOSED)
        findings = harness_lint.check_dup_canon(self.root, self._cfg())
        self.assertTrue(
            any("unbalanced" in f.message and f.tier == "gating" for f in findings),
            f"no unbalanced-marker finding in: {[f.message for f in findings]}")

    def test_balanced_docs_still_regenerate(self):
        doc = self.root / "docs"
        doc.mkdir(exist_ok=True)
        f = doc / "ok.md"
        f.write_text("<!-- fh:gen gate-table -->\nstale\n<!-- /fh:gen -->\n")
        changed = gen.regenerate_project(self.root, self._cfg())
        self.assertIn(f, changed)
        self.assertIn("| # | Gate |", f.read_text())


# ── F24: hostile unit slugs are refused, not path-joined ────────────────────
class TestSlugValidation(Base):
    BAD_SLUGS = ["../../fz-escape", "units/nested", "a/../../b",
                 "-leading-dash", ".hidden", "", "a b"]

    def test_gate_refuses_hostile_slugs(self):
        # snapshot each slug's resolved target (some resolve OUTSIDE the
        # repo, into shared tmp, compare per-target, never the whole dir)
        targets = {s: (self.root / "units" / s).resolve()
                   for s in self.BAD_SLUGS if s}
        before = {s: t.exists() for s, t in targets.items()}
        for slug in self.BAD_SLUGS:
            r = self.gate("start", "--unit", slug)
            self.assertNotEqual(r.returncode, 0, f"slug {slug!r} accepted")
            self.assert_no_traceback(r)
            # our validator says "slug"; argparse refuses option-lookalikes
            # (e.g. -leading-dash) with its own usage error, both are typed
            out = (r.stdout + r.stderr).lower()
            self.assertTrue("slug" in out or "error:" in out, slug)
        for slug, t in targets.items():
            self.assertEqual(t.exists(), before[slug],
                             f"slug {slug!r} created {t} despite refusal")
        self.assertFalse((self.root / "units" / "units").exists())

    def test_lint_refuses_hostile_slug(self):
        r = self.lint("--unit", "../x", "--set-gate", "verified")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assert_no_traceback(r)


# ── F28: one hand-written string in facts.yaml must not blank context ───────
class TestFactsTolerance(Base):
    def _write_facts(self, entries):
        p = self.root / "harness" / "state" / "facts.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump({"facts": entries}))

    def test_read_facts_coerces_strings(self):
        self._write_facts([{"text": "a proper fact"}, "just a plain string"])
        facts = memory.read_facts(self.root)
        self.assertEqual(len(facts), 2)
        for f in facts:
            self.assertIsInstance(f, dict)
        self.assertEqual(facts[1]["text"], "just a plain string")

    def test_commit_fact_survives_mixed_entries(self):
        self._write_facts(["bare string"])
        self.assertTrue(memory.commit_fact(self.root, "new fact"))
        texts = [f["text"] for f in memory.read_facts(self.root)]
        self.assertIn("new fact", texts)
        self.assertIn("bare string", texts)

    def test_session_start_still_prints_context(self):
        self._write_facts([{"text": "visible fact"}, "bare string fact"])
        ev = {"session_id": "S1", "cwd": str(self.root),
              "hook_event_name": "SessionStart"}
        r = run_hook("session_start.py", ev, self.root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("visible fact", r.stdout,
                      "session context was silently blanked by one bad entry")


# ── F29: typos produce typed refusals, not tracebacks ───────────────────────
class TestTypedRefusals(Base):
    def test_approve_missing_unit(self):
        r = self.gate("approve", "composed", "--unit", "no-such-unit")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assert_no_traceback(r)
        self.assertIn("error", r.stderr.lower())

    def test_pause_and_escalate_missing_unit(self):
        for args in (("pause", "--unit", "typo"),
                     ("escalate", "--unit", "typo", "--note", "x")):
            r = self.gate(*args)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assert_no_traceback(r)

    def test_lint_set_gate_missing_unit(self):
        r = self.lint("--unit", "no-such-unit", "--set-gate", "verified")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assert_no_traceback(r)

    def test_lint_missing_unit_is_not_clean(self):
        r = self.lint("--unit", "no-such-unit")
        self.assertEqual(r.returncode, 2,
                         f"lint reported success for a nonexistent unit: "
                         f"{r.stdout + r.stderr}")
        self.assert_no_traceback(r)

    def test_lint_outside_file_no_traceback(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("clean prose\n")
            outside = fh.name
        try:
            r = self.lint("--file", outside)
            self.assert_no_traceback(r)
            self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        finally:
            Path(outside).unlink()


# ── T9: seeded fuzz, the general detector the targeted tests calibrate ─────
def _mutate(obj, rng, depth=0):
    """One random structural mutation of a config-shaped object."""
    pool = [None, True, False, 0, -1, 3.14, "", "x", [], {}, [1, 2],
            {"a": 1}, ["mixed", {"b": 2}]]
    if isinstance(obj, dict) and obj and depth < 4:
        key = rng.choice(sorted(obj, key=str))
        action = rng.random()
        out = dict(obj)
        if action < 0.3:
            del out[key]
        elif action < 0.6:
            out[key] = rng.choice(pool)
        elif action < 0.7:
            out[True] = out.pop(key)  # the YAML-boolean-key class
        else:
            out[key] = _mutate(out[key], rng, depth + 1)
        return out
    if isinstance(obj, list) and obj and depth < 4:
        out = list(obj)
        i = rng.randrange(len(out))
        if rng.random() < 0.5:
            out[i] = rng.choice(pool)
        else:
            out[i] = _mutate(out[i], rng, depth + 1)
        return out
    return rng.choice(pool)


class TestBoundaryFuzz(Base):
    N_CONFIG = 150
    N_FACTS = 60
    N_HOOK = 25

    def test_config_loader_contract(self):
        """config.load returns a Config or raises ConfigError, nothing else."""
        rng = random.Random(1337)
        for i in range(self.N_CONFIG):
            mutant = default_adapter()
            for _ in range(rng.randint(1, 3)):
                mutant = _mutate(mutant, rng)
            if not isinstance(mutant, dict):
                mutant = {"fh_schema": mutant}
            write_adapter(self.root, mutant)
            try:
                cfg = config.load(self.root)
                cfg.gate_command(str(cfg.gates[0]) if cfg.gates else "g", "slug")
            except config.ConfigError:
                pass
            except Exception as exc:
                self.fail(f"iteration {i}: config.load leaked "
                          f"{type(exc).__name__}: {exc}\nmutant: {mutant!r}")

    def test_facts_reader_contract(self):
        """read_facts returns a list of dicts and never raises."""
        rng = random.Random(4242)
        base = {"facts": [{"at": "t", "text": "a fact"}]}
        p = self.root / "harness" / "state" / "facts.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        for i in range(self.N_FACTS):
            mutant = _mutate(base, rng)
            p.write_text(yaml.safe_dump(mutant))
            try:
                facts = memory.read_facts(self.root)
            except Exception as exc:
                self.fail(f"iteration {i}: read_facts raised "
                          f"{type(exc).__name__} on {mutant!r}")
            self.assertIsInstance(facts, list)
            for f in facts:
                self.assertIsInstance(f, dict, f"non-dict fact from {mutant!r}")

    def test_pretool_guard_fail_closed_under_config_garbage(self):
        """For every adapter mutant: hook exits 0, no traceback on stderr, and
        if the config is unloadable the ACCT write is DENIED, never allowed."""
        rng = random.Random(2024)
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "doc.md"),
                        "content": f"id: {ACCT_TOKEN}"})
        for i in range(self.N_HOOK):
            mutant = default_adapter()
            for _ in range(rng.randint(1, 3)):
                mutant = _mutate(mutant, rng)
            if not isinstance(mutant, dict):
                mutant = {"fh_schema": mutant}
            write_adapter(self.root, mutant)
            r = run_hook("pretool_guard.py", ev, self.root)
            self.assertEqual(r.returncode, 0, f"iteration {i}")
            self.assertNotIn("Traceback", r.stderr, f"iteration {i}")
            if r.stdout.strip():
                json.loads(r.stdout)  # any output must be valid hook JSON
            try:
                config.load(self.root)
                loadable = True
            except Exception:
                loadable = False  # loader contract has its own fuzz test
            if not loadable:
                self.assertIn("deny", r.stdout,
                              f"iteration {i}: unloadable config but the write "
                              f"was ALLOWED (fail-open)\nmutant: {mutant!r}")

    def test_gate_cli_hostile_slug_sweep(self):
        rng = random.Random(99)
        chars = "abz09-_./\\ $;\n"
        slugs = {"".join(rng.choice(chars) for _ in range(rng.randint(1, 12)))
                 for _ in range(20)}
        for slug in slugs:
            r = self.gate("start", "--unit", slug)
            self.assertIn(r.returncode, (0, 1, 2), repr(slug))
            self.assertNotIn("Traceback", r.stderr, repr(slug))


if __name__ == "__main__":
    unittest.main()
