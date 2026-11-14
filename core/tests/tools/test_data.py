"""Data tool: query_table is read-only and confined to the workspace (A3).

The database is a real SQLite file in a temporary workspace. Writes are refused
twice: by the statement check, and by the connection's query_only pragma.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import hashlib
import sqlite3
import tempfile
import unittest

from core.kernel.types import ActionKind, Budget
from core.tools.spec import ToolContext
from core.tools.data import make_query_table


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


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QueryTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "data").mkdir()
        self.db = self.root / "data" / "sales.sqlite"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE sales (region TEXT, total INTEGER)")
        conn.executemany("INSERT INTO sales VALUES (?, ?)",
                         [("north", 10), ("south", 20), ("west", 30)])
        conn.commit()
        conn.close()
        self.ctx = make_ctx(self.root)
        self.spec = make_query_table()

    def tearDown(self):
        self._tmp.cleanup()

    def query(self, sql, path="data/sales.sqlite", ctx=None):
        return self.spec.run({"path": path, "sql": sql}, ctx or self.ctx)

    def row_count(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
        finally:
            conn.close()

    def test_select_returns_rows_and_touches_nothing(self):
        result = self.query("SELECT region, total FROM sales ORDER BY region")
        self.assertIn("north\t10", result.content)
        self.assertIn("west\t30", result.content)
        self.assertEqual(result.touched, ())

    def test_leading_comment_and_with_are_read_queries(self):
        result = self.query("-- totals by region\nWITH t AS (SELECT SUM(total) AS s FROM sales) SELECT s FROM t")
        self.assertIn("60", result.content)

    def test_writing_statements_are_refused_and_database_unchanged(self):
        before = file_digest(self.db)
        statements = [
            "INSERT INTO sales VALUES ('east', 1)",
            "UPDATE sales SET total = 0",
            "DELETE FROM sales",
            "DROP TABLE sales",
            "CREATE TABLE extra (a)",
            "PRAGMA journal_mode = DELETE",
            "ATTACH DATABASE 'x.db' AS x",
            "WITH x AS (SELECT 1) DELETE FROM sales",
            "SELECT 1; DELETE FROM sales",
        ]
        for sql in statements:
            with self.subTest(sql=sql):
                result = self.query(sql)
                self.assertTrue(result.content.startswith("refused"), result.content)
                self.assertEqual(result.touched, ())
        self.assertEqual(self.row_count(), 3)
        self.assertEqual(file_digest(self.db), before)

    def test_sql_error_is_reported_not_raised(self):
        result = self.query("SELECT nope FROM sales")
        self.assertIn("query failed", result.content)

    def test_row_limit_is_reported(self):
        spec = make_query_table(max_rows=2)
        result = spec.run({"path": "data/sales.sqlite", "sql": "SELECT * FROM sales"}, self.ctx)
        self.assertIn("truncated at 2 rows", result.content)
        self.assertNotIn("west", result.content)

    def test_blind_database_refused(self):
        ctx = make_ctx(self.root, blind=("data/*.sqlite",))
        result = self.query("SELECT * FROM sales", ctx=ctx)
        self.assertTrue(result.content.startswith("refused"), result.content)
        self.assertIn("blind", result.content)

    def test_paths_outside_the_workspace_refused(self):
        for path in ("../sales.sqlite", "/etc/passwd", "data/../../x.sqlite"):
            with self.subTest(path=path):
                result = self.query("SELECT 1", path=path)
                self.assertTrue(result.content.startswith("refused"), result.content)

    def test_missing_database_is_reported_and_not_created(self):
        result = self.query("SELECT 1", path="data/none.sqlite")
        self.assertIn("not found", result.content)
        self.assertFalse((self.root / "data" / "none.sqlite").exists())

    def test_action_is_read_of_the_database_path(self):
        action = self.spec.action_for({"path": "data/sales.sqlite", "sql": "SELECT 1"})
        self.assertEqual(action.kind, ActionKind.READ)
        self.assertEqual(action.path, "data/sales.sqlite")
        self.assertEqual(self.spec.effects, frozenset({"read"}))


if __name__ == "__main__":
    unittest.main()
