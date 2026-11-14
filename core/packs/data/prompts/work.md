You are preparing one dataset unit in the directory named by the unit.

The unit holds two files:
- rows.csv: the table, with a header line. An empty cell is a null.
- spec.json: the declaration. It names the source file, the columns with their
  types (integer, decimal, date, text), the row bounds, the largest share of
  empty cells allowed per column (max_null_rate), and the totals that the table
  must reconcile to, as exact decimal strings.

Write rows.csv and spec.json so that the header matches the declared columns in
the declared order, every value parses as its column's type, the row count is
within bounds, and each declared total equals the exact sum of its column. Do not
round totals to make them match; if the sum differs, report the difference
instead of editing the declaration.
