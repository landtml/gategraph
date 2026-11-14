"""Boundary hardening, ported from the legacy core/tests/test_boundary.py (X1 B5b).

The contract: every boundary answers invalid input with a typed refusal, never a traceback,
never a silent pass, never silent evidence loss. Seeded fuzz runs stay deterministic.
Refusals in the sense of B01: a check that did not run is never a pass, and a marker
structure that would destroy content is refused before anything is written.

Kernel only (standard library and PyYAML). The host and gate-command halves are in
core/tests/hosts/test_boundary_hosts.py. The legacy lint --set-gate, lint CLI and approve
cases are deleted: D11 retires the direct gate set.

Run alone with: python3 core/tests/kernel/test_boundary_port.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import random
import tempfile
import unittest
from pathlib import Path

import yaml

from core import brand
from core.kernel import checks, config, memory, selflint
from core.kernel.gen import GenError, regenerate, regenerate_text
from core.kernel.types import CheckSpec, CheckStatus, Severity
from fixtures_v2 import make_project

ACCT_TOKEN = "010190" + "-1234"  # synthetic, assembled at run time
GEN_OPEN = f"<!-- {brand.GEN_MARKER} gate-table -->"
GEN_CLOSE = f"<!-- /{brand.GEN_MARKER} -->"


def mutate(obj, rng, depth=0):
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
            out[key] = mutate(out[key], rng, depth + 1)
        return out
    if isinstance(obj, list) and obj and depth < 4:
        out = list(obj)
        i = rng.randrange(len(out))
        out[i] = rng.choice(pool) if rng.random() < 0.5 else mutate(out[i], rng, depth + 1)
        return out
    return rng.choice(pool)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")
        self.adapter_path = self.root / "harness" / "adapter" / "adapter.yaml"

    def adapter(self):
        return yaml.safe_load(self.adapter_path.read_text(encoding="utf-8"))

    def write_adapter(self, data):
        self.adapter_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def facts_path(self):
        p = self.root / "harness" / "state" / "facts.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


# ---- F18: a deny policy without `id` is refused at load ---------------------------------
class TestDenyPolicyId(Base):
    def test_config_rejects_idless_deny_policy(self):
        data = self.adapter()
        data["policy"] = {"deny": [{"regex": r"\d{6}\s*-?\s*\d{4}", "summary": "no account number"}]}
        self.write_adapter(data)
        with self.assertRaises(config.ConfigError) as ctx:
            config.load(self.root)
        self.assertIn("id", str(ctx.exception))

    def test_config_loader_contract(self):
        """config.load returns a Config or raises ConfigError, nothing else (seeded fuzz)."""
        rng = random.Random(1337)
        base = self.adapter()
        for i in range(150):
            mutant = base
            for _ in range(rng.randint(1, 3)):
                mutant = mutate(mutant, rng)
            if not isinstance(mutant, dict):
                mutant = {"schema": mutant}
            self.write_adapter(mutant)
            try:
                cfg = config.load(self.root)
                cfg.gates and cfg.gates[0].id  # touch the loaded shape
            except config.ConfigError:
                pass
            except Exception as exc:
                self.fail(f"iteration {i}: config.load leaked "
                          f"{type(exc).__name__}: {exc}\nmutant: {mutant!r}")


# ---- F28: one hand-written string in facts.yaml must not blank the context ---------------
class TestFactsTolerance(Base):
    def test_read_facts_coerces_strings(self):
        self.facts_path().write_text(yaml.safe_dump(
            {"facts": [{"text": "a proper fact"}, "just a plain string"]}))
        facts = memory.read_facts(self.root)
        self.assertEqual(len(facts), 2)
        for f in facts:
            self.assertIsInstance(f, dict)
        self.assertEqual(facts[1]["text"], "just a plain string")

    def test_commit_fact_survives_mixed_entries(self):
        self.facts_path().write_text(yaml.safe_dump({"facts": ["bare string"]}))
        self.assertTrue(memory.commit_fact(self.root, "new fact"))
        texts = [f["text"] for f in memory.read_facts(self.root)]
        self.assertIn("new fact", texts)
        self.assertIn("bare string", texts)

    def test_facts_reader_contract(self):
        """read_facts returns a list of dicts, or raises FactsCorrupt (seeded fuzz)."""
        rng = random.Random(4242)
        base = {"facts": [{"at": "t", "text": "a fact"}]}
        p = self.facts_path()
        for i in range(60):
            mutant = mutate(base, rng)
            p.write_text(yaml.safe_dump(mutant))
            try:
                facts = memory.read_facts(self.root)
            except memory.FactsCorrupt:
                continue
            except Exception as exc:
                self.fail(f"iteration {i}: read_facts raised "
                          f"{type(exc).__name__} on {mutant!r}")
            self.assertIsInstance(facts, list)
            for f in facts:
                self.assertIsInstance(f, dict, f"non-dict fact from {mutant!r}")


# ---- F23: gen must never silently delete content -----------------------------------------
UNCLOSED = ("# Doc\n\n"
            f"{GEN_OPEN}\n"
            "stale table\n\n"
            "## Hand-written section\n\n"
            "important deploy process prose\n\n"
            f"{GEN_OPEN}\n"
            "second table\n"
            f"{GEN_CLOSE}\n")


class TestGenMarkerSafety(Base):
    def setUp(self):
        super().setUp()
        self.cfg = config.load(self.root)

    def test_unclosed_marker_refused(self):
        with self.assertRaises(GenError):
            regenerate_text(UNCLOSED, self.cfg)

    def test_regenerate_writes_nothing_on_bad_doc(self):
        bad = self.root / "docs"
        bad.mkdir(exist_ok=True)
        (bad / "bad.md").write_text(UNCLOSED)
        good = bad / "good.md"
        good.write_text(f"{GEN_OPEN}\nstale\n{GEN_CLOSE}\n")
        good_before = good.read_text()
        with self.assertRaises(GenError) as ctx:
            regenerate(self.root, self.cfg)
        self.assertIn("bad.md", str(ctx.exception))
        self.assertEqual((bad / "bad.md").read_text(), UNCLOSED, "gen mutated a malformed doc")
        self.assertEqual(good.read_text(), good_before, "gen wrote other docs before refusing")

    def test_nested_pair_refused(self):
        nested = (f"{GEN_OPEN}\nouter\n{GEN_OPEN}\ninner\n{GEN_CLOSE}\n{GEN_CLOSE}\n")
        with self.assertRaises(GenError):
            regenerate_text(nested, self.cfg)

    def test_dup_canon_flags_unbalanced_markers(self):
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "bad.md").write_text(UNCLOSED)
        findings = selflint.run_lint(self.root, self.cfg, tier="gating")
        self.assertTrue(any(f.check == "dup-canon" and "unbalanced" in f.message
                            for f in findings), [f.message for f in findings])

    def test_balanced_docs_still_regenerate(self):
        (self.root / "docs").mkdir(exist_ok=True)
        f = self.root / "docs" / "ok.md"
        f.write_text(f"{GEN_OPEN}\nstale\n{GEN_CLOSE}\n")
        changed = regenerate(self.root, self.cfg)
        self.assertIn(f, changed)
        self.assertIn("| # | Gate |", f.read_text())


# ---- F19: a check that cannot run never earns a gate (refusals, B01 class) ---------------
def _spec(command, severity=Severity.FATAL):
    return CheckSpec(id="c1", command=command, severity=severity)


class TestCheckExitContract(Base):
    def test_missing_binary_is_did_not_run_and_never_a_pass(self):
        result = checks.run_check(_spec("this_binary_does_not_exist_fh_xyz {files}"),
                                  self.root, files=("notes/x.md",))
        self.assertEqual(result.status, CheckStatus.DID_NOT_RUN)
        self.assertNotEqual(result.status, CheckStatus.PASS)

    def test_signal_death_is_did_not_run_and_never_a_pass(self):
        result = checks.run_check(_spec("sh -c 'kill -9 $$'"), self.root)
        self.assertEqual(result.status, CheckStatus.DID_NOT_RUN)

    def test_exit_2_is_warn_not_a_blocking_fatal(self):
        result = checks.run_check(_spec("sh -c 'echo advisory-detail >&2; exit 2'"), self.root)
        self.assertEqual(result.status, CheckStatus.WARN)

    def test_severity_warn_demotes_exit_1(self):
        result = checks.run_check(_spec("sh -c 'echo style nit; exit 1'", Severity.WARN),
                                  self.root)
        self.assertEqual(result.status, CheckStatus.WARN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
