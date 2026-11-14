"""X1 exit gate: no legacy engine remains, only the allowed compatibility shims.

Final state after X1 (decisions D1, D2, D11). The gate asserts:

- every legacy-named file that remains is in ALLOWED_SHIMS, and nothing else is;
- every allowed shim exists, is short, and holds no engine logic (no classes, no
  functions beyond the CLI forwarding ones);
- core/hooks/ is gone, and core/loop/ holds only cli.py (a shim over the new search CLI);
- no non-shim file imports a shim (stamped scripts run them by path, not by import).

Stamped projects carry only the kernel layer, not the template layout, so the whole
module skips there (the same test as core/tests/test_stamp.py: parents[2] is "harness").
Run alone: ``python3 core/tests/test_no_legacy.py``. Stdlib only.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401,E402

import ast  # noqa: E402
import re  # noqa: E402
import unittest  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
IS_STAMPED = REPO.name == "harness"

# Legacy-named top-level modules: the version-1 names of the engine (D1, D2) and the B4 names.
LEGACY_TOP_STEMS = {
    "config", "manifest", "paths", "policy", "failures", "lint", "gate", "doctor",
    "harness_lint", "memory", "reap", "gen", "maintenance", "handoff",
}

# The complete allow-list (16 names). Each is a shim over core/compat/shims/, core/kernel/,
# core/hosts/ or core/cli/, or a CLI entry point that runs the new command.
ALLOWED_SHIMS = {
    # B4 eight: import shims (the first five) and script shims with a refusal (lint, gate, doctor).
    "core/config.py",
    "core/manifest.py",
    "core/paths.py",
    "core/policy.py",
    "core/failures.py",
    "core/lint.py",
    "core/gate.py",
    "core/doctor.py",
    # D2: kept as shims over the kernel, or refusing with a next: line (handoff).
    "core/harness_lint.py",
    "core/memory.py",
    "core/reap.py",
    "core/gen.py",
    "core/maintenance.py",
    "core/handoff.py",
    # D1: the stamped CLI entry points, kept as short scripts.
    "core/loop/cli.py",
    "core/eval/cli.py",
}

# Shims that are CLI entry points rather than import shims. Their length is not limited:
# loop/cli.py is the subcommand map for the stamped loop targets (D1), and eval/cli.py is the
# stamped `make eval` entry (corpus run, pass-rate and ICC report). Neither holds engine code
# of its own; the checks in test_cli_entries_have_no_legacy_imports still apply.
CLI_ENTRIES = {"core/loop/cli.py", "core/eval/cli.py"}

# Length limit for the other shims. The default is the brief's example. gate.py and lint.py
# hold argparse forwarding and the refusal messages with their next: lines, so they get more.
DEFAULT_MAX_LINES = 25
MAX_LINES = {
    "core/gate.py": 100,
    "core/lint.py": 50,
}

# Functions a shim may define: CLI forwarding (main, _refuse) and one-line delegations.
SHIM_FUNCTIONS = {"main", "_refuse", "marker_path"}
MAX_DELEGATE_LINES = 3

# Shim module names, as imported by other code.
SHIM_MODULES = {
    "core.config", "core.manifest", "core.paths", "core.policy", "core.failures",
    "core.lint", "core.gate", "core.doctor", "core.harness_lint", "core.memory",
    "core.reap", "core.gen", "core.maintenance", "core.handoff", "core.loop.cli",
    "core.eval.cli",
}
SHIM_FROM_CORE = {"config", "manifest", "paths", "policy", "failures", "lint", "gate",
                  "doctor", "harness_lint", "memory", "reap", "gen", "maintenance",
                  "handoff"}

SCAN_DIRS = ["core", "tests", "eval", "scripts"]
SKIP_PARTS = {"__pycache__", ".venv", "compat"}


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(REPO).as_posix()


def _py_files(dirs=SCAN_DIRS):
    """(relative path, absolute path) for every .py file outside core/compat/."""
    for d in dirs:
        base = REPO / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(REPO)
            if SKIP_PARTS & set(rel.parts) or rel.parts[:2] == ("core", "compat"):
                continue
            yield _rel(path), path
    for path in sorted(REPO.glob("*.py")):
        yield _rel(path), path


def _legacy_named_files():
    """Every file the gate treats as legacy-named: the top-level names above, all of
    core/loop/ and core/hooks/, and the eval engine entries (runner, ab, cli)."""
    found = set()
    for stem in LEGACY_TOP_STEMS:
        if (REPO / "core" / f"{stem}.py").is_file():
            found.add(f"core/{stem}.py")
    for sub in ("core/loop", "core/hooks"):
        base = REPO / sub
        if base.is_dir():
            found |= {_rel(p) for p in base.rglob("*.py") if "__pycache__" not in p.parts}
    for name in ("cli", "runner", "ab"):
        if (REPO / "core" / "eval" / f"{name}.py").is_file():
            found.add(f"core/eval/{name}.py")
    return found


def _imports(source: str):
    """(line, module) for every import, with `from core import x` and `from core.loop import x`
    expanded to dotted names."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
            out += [(node.lineno, f"{node.module}.{a.name}") for a in node.names]
    return out


# Non-shim files that import a shim on purpose. Each is a legacy-producer contract or a
# script that refuses to run (D8), so the import is not a live dependency on the old engine.
EXEMPT_IMPORTERS = {
    "core/tests/kernel/fixtures/legacy_review_gate_v1.py",  # a version-1 producer (test_v1_api)
    "tests/golden/make_golden.py",                          # D8: refuses to run without the engine
}


def _is_shim_module(name: str) -> bool:
    if name in SHIM_MODULES:
        return True
    head, _, tail = name.partition(".")
    return head == "core" and tail in SHIM_FROM_CORE


@unittest.skipIf(IS_STAMPED, "stamped project: the template layout is absent")
class NoLegacyTest(unittest.TestCase):

    def test_only_allowed_shims_remain(self):
        present = _legacy_named_files()
        extra = sorted(present - ALLOWED_SHIMS)
        self.assertEqual(extra, [], "legacy-named files that are not allowed shims")

    def test_every_allowed_shim_exists(self):
        missing = sorted(p for p in ALLOWED_SHIMS if not (REPO / p).is_file())
        self.assertEqual(missing, [], "allowed shims that are missing")

    def test_hooks_directory_is_gone(self):
        self.assertFalse((REPO / "core" / "hooks").exists(), "core/hooks/ must be deleted")

    def test_loop_holds_only_the_cli_shim(self):
        base = REPO / "core" / "loop"
        if not base.exists():
            self.fail("core/loop/cli.py is missing")
        left = sorted(_rel(p) for p in base.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts)
        self.assertEqual(left, ["core/loop/cli.py"], "core/loop/ must hold only cli.py")

    def test_shims_are_short_and_hold_no_engine_logic(self):
        bad = []
        for rel in sorted(ALLOWED_SHIMS):
            path = REPO / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            if rel not in CLI_ENTRIES:
                limit = MAX_LINES.get(rel, DEFAULT_MAX_LINES)
                if len(lines) > limit:
                    bad.append(f"{rel}: {len(lines)} lines, limit {limit}")
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        bad.append(f"{rel}:{node.lineno} defines class {node.name}")
                    elif isinstance(node, ast.FunctionDef):
                        span = (node.end_lineno or node.lineno) - node.lineno + 1
                        if node.name not in SHIM_FUNCTIONS:
                            bad.append(f"{rel}:{node.lineno} defines def {node.name}")
                        elif node.name == "marker_path" and span > MAX_DELEGATE_LINES:
                            bad.append(f"{rel}:{node.lineno} marker_path is not a delegation")
        self.assertEqual(bad, [], "shim is too long or holds engine logic")

    def test_cli_entries_have_no_legacy_imports(self):
        bad = []
        for rel in sorted(CLI_ENTRIES):
            path = REPO / rel
            if not path.is_file():
                bad.append(f"{rel}: missing")
                continue
            for line, mod in _imports(path.read_text(encoding="utf-8")):
                deleted = (mod == "core.hooks" or mod.startswith("core.hooks.")
                           or (mod.startswith("core.loop.") and mod != "core.loop.cli")
                           or mod in {"core.eval.runner", "core.eval.ab"})
                if deleted:
                    bad.append(f"{rel}:{line} imports {mod}")
        self.assertEqual(bad, [])

    def test_no_non_shim_file_imports_a_shim(self):
        found = []
        for rel, path in _py_files():
            if rel in ALLOWED_SHIMS or rel in EXEMPT_IMPORTERS or rel == "core/tests/test_no_legacy.py":
                continue
            try:
                source = path.read_text(encoding="utf-8")
                imports = _imports(source)
            except (SyntaxError, UnicodeDecodeError):
                continue
            found += [f"{rel}:{line} imports {mod}" for line, mod in imports
                      if _is_shim_module(mod)]
        self.assertEqual(found, [], "non-shim files import a shim")

    def test_makefile_has_no_legacy_targets(self):
        makefile = (REPO / "Makefile").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^eval-ab:", makefile, re.M), "eval-ab target remains")
        eval_rule = re.search(r"^eval:[^\n]*\n((?:\t.*\n?)+)", makefile, re.M)
        self.assertIsNotNone(eval_rule, "no eval target")
        self.assertIn("runner_v2.py", eval_rule.group(1))
        self.assertIn("eval/pack_v2", eval_rule.group(1))

    def test_test_core_runs_this_gate(self):
        makefile = (REPO / "Makefile").read_text(encoding="utf-8")
        rule = re.search(r"^\tpython3 -m unittest discover -s core/tests -p '([^']*)'",
                         makefile, re.M)
        self.assertIsNotNone(rule, "test-core discover line not found")
        self.assertNotIn("[!n]", rule.group(1), "test-core must not exclude test_no_legacy.py")


if __name__ == "__main__":
    unittest.main()
