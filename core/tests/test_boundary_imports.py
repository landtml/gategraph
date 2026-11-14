"""Import boundary: the kernel layer never imports LangGraph or LangChain.

Kernel layer = core/kernel, core/hosts, core/compat. Every file
there is parsed, and no import of langgraph, langchain* or pydantic may
appear. A subprocess then imports core.kernel and checks that none of those
packages was loaded as a side effect.

Runs with the system python3 (standard library only). Also run by phase0 with
.venv/bin/python, which has the pinned LangGraph installed, so the subprocess
check is meaningful there.
"""
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import ast
import pathlib
import subprocess
import sys
import unittest

REPO = _bootstrap.REPO
KERNEL_LAYER = ("core/kernel", "core/hosts", "core/compat")
FORBIDDEN_PREFIXES = ("langgraph", "langchain", "langsmith")
FORBIDDEN_TOP = ("pydantic", "pydantic_core")


def _forbidden(module):
    top = module.split(".")[0]
    if module.startswith(FORBIDDEN_PREFIXES):
        return True
    return top in FORBIDDEN_TOP


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.module, node.lineno


class TestBoundaryImports(unittest.TestCase):
    def test_kernel_layer_imports_no_graph_stack(self):
        if not any((REPO / d).is_dir() for d in KERNEL_LAYER):
            self.skipTest("no kernel layer in this tree (stamped project)")
        offenders = []
        files = 0
        for d in KERNEL_LAYER:
            root = REPO / d
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                files += 1
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for module, lineno in _imports(tree):
                    if _forbidden(module):
                        rel = path.relative_to(REPO)
                        offenders.append(f"{rel}:{lineno} imports {module}")
        self.assertEqual(offenders, [])
        self.assertGreater(files, 0, "no kernel-layer files found to check")

    def test_importing_core_kernel_loads_no_graph_stack(self):
        if not (REPO / "core/kernel").is_dir():
            self.skipTest("no core/kernel in this tree (stamped project)")
        code = (
            "import sys\n"
            "import core.kernel\n"
            "bad = sorted(m for m in sys.modules\n"
            "             if m.split('.')[0] in ('langgraph', 'langchain', 'langsmith',\n"
            "                                    'pydantic', 'pydantic_core')\n"
            "             or m.startswith(('langgraph', 'langchain')))\n"
            "print(bad)\n"
            "sys.exit(1 if bad else 0)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"core.kernel pulled in graph modules: {proc.stdout}{proc.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
