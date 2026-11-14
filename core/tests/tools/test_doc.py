"""Document tools: extract_text and render_document (A3).

External programs are stood in for by the test interpreter itself, so the tests
never depend on a PDF or document toolchain being installed. A missing program
is simulated with a name that is not on PATH.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import tempfile
import unittest

from core.kernel.types import ActionKind, Budget
from core.tools.spec import ToolContext
from core.tools.doc import make_extract_text, make_render_document

MISSING = "nosuch-extractor-7f3a"
PY = sys.executable
CAT_SCRIPT = "import sys; sys.stdout.write(open(sys.argv[1], encoding='utf-8').read())"
COPY_SCRIPT = "import sys, shutil; shutil.copy(sys.argv[1], sys.argv[2])"


class MemoryJournal:
    def seen(self, key):
        return None

    def record(self, key, result):
        pass


def make_ctx(root, blind=()):
    unit = root / "unit"
    scratch = root / "scratch"
    unit.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    return ToolContext(root=root, unit_dir=unit, scratch_dir=scratch,
                       writable=("unit", "scratch"), blind=tuple(blind),
                       budget=Budget(), journal=MemoryJournal())


class ExtractTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "paper.txt").write_text("alpha beta\n", encoding="utf-8")
        self.ctx = make_ctx(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_extractor_is_reported_clearly(self):
        spec = make_extract_text(command=(MISSING, "{path}", "-"))
        result = spec.run({"path": "docs/paper.txt"}, self.ctx)
        self.assertTrue(result.content.startswith("extractor not installed"), result.content)
        self.assertIn(MISSING, result.content)
        self.assertEqual(result.touched, ())

    def test_text_comes_from_the_configured_extractor(self):
        spec = make_extract_text(command=(PY, "-c", CAT_SCRIPT, "{path}"))
        result = spec.run({"path": "docs/paper.txt"}, self.ctx)
        self.assertEqual(result.content, "alpha beta\n")
        self.assertEqual(result.touched, ())

    def test_output_is_truncated_to_max_chars(self):
        spec = make_extract_text(command=(PY, "-c", CAT_SCRIPT, "{path}"), max_chars=5)
        result = spec.run({"path": "docs/paper.txt"}, self.ctx)
        self.assertTrue(result.content.startswith("alpha"), result.content)
        self.assertIn("truncated", result.content)

    def test_blind_path_refused(self):
        ctx = make_ctx(self.root, blind=("docs/*.txt",))
        spec = make_extract_text(command=(PY, "-c", CAT_SCRIPT, "{path}"))
        result = spec.run({"path": "docs/paper.txt"}, ctx)
        self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertIn("blind", result.content)

    def test_path_outside_workspace_refused(self):
        spec = make_extract_text(command=(PY, "-c", CAT_SCRIPT, "{path}"))
        result = spec.run({"path": "../etc/passwd"}, self.ctx)
        self.assertTrue(result.content.startswith("refused"), result.content)

    def test_missing_file_reported(self):
        spec = make_extract_text(command=(PY, "-c", CAT_SCRIPT, "{path}"))
        result = spec.run({"path": "docs/none.pdf"}, self.ctx)
        self.assertIn("not found", result.content)

    def test_action_is_a_read_and_extractor_effect_declared(self):
        spec = make_extract_text(command=(PY, "-c", CAT_SCRIPT, "{path}"))
        action = spec.action_for({"path": "docs/paper.txt"})
        self.assertEqual(action.kind, ActionKind.READ)
        self.assertEqual(action.path, "docs/paper.txt")
        self.assertEqual(spec.effects, frozenset({"read", "exec"}))


class RenderDocumentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "note.md").write_text("# Note\n", encoding="utf-8")
        self.ctx = make_ctx(self.root)
        self.spec = make_render_document(command=(PY, "-c", COPY_SCRIPT, "{source}", "{output}"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_renders_inside_unit_and_reports_touched(self):
        result = self.spec.run({"source": "docs/note.md", "output": "unit/out/note.html"}, self.ctx)
        self.assertEqual(result.touched, ("unit/out/note.html",))
        self.assertEqual((self.root / "unit" / "out" / "note.html").read_text(encoding="utf-8"),
                         "# Note\n")

    def test_output_in_scratch_is_allowed(self):
        result = self.spec.run({"source": "docs/note.md", "output": "scratch/note.html"}, self.ctx)
        self.assertEqual(result.touched, ("scratch/note.html",))
        self.assertTrue((self.root / "scratch" / "note.html").exists())

    def test_output_outside_unit_and_scratch_refused(self):
        result = self.spec.run({"source": "docs/note.md", "output": "docs/copied.md"}, self.ctx)
        self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertFalse((self.root / "docs" / "copied.md").exists())
        self.assertEqual(result.touched, ())

    def test_missing_renderer_is_reported(self):
        spec = make_render_document(command=(MISSING, "{source}", "{output}"))
        result = spec.run({"source": "docs/note.md", "output": "unit/x.html"}, self.ctx)
        self.assertTrue(result.content.startswith("renderer not installed"), result.content)
        self.assertEqual(result.touched, ())

    def test_command_must_name_output(self):
        with self.assertRaises(ValueError):
            make_render_document(command=(PY, "-c", "pass", "{source}"))

    def test_action_is_a_write_of_the_output(self):
        action = self.spec.action_for({"source": "docs/note.md", "output": "unit/x.html"})
        self.assertEqual(action.kind, ActionKind.WRITE)
        self.assertEqual(action.path, "unit/x.html")


if __name__ == "__main__":
    unittest.main()
