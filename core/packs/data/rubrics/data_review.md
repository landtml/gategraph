# Rubric: dataset unit review (pack `data`)

A reviewer reads spec.json and rows.csv and answers each item pass or fail.
The mechanical checks (schema, row bounds, null rates, totals) run first; this
rubric covers what they cannot see.

1. Purpose. spec.json says what the table is for, in the unit's notes. A table
   with no stated use is a fail.
2. Columns. Every column's type fits its values, and the names say what the
   column holds. A column named only by position is a fail.
3. Totals. Each declared total is the quantity the purpose names (for example,
   the revenue the table is meant to report), not a convenient subtotal.
4. Empty cells. Where a column is empty, the reason is in the notes, not guessed.
5. Synthetic data only. No real person's name, identifier, contact detail or
   account number appears in any cell.

Output: one line per item, `pass` or `fail` with a short reason, then `verdict:
pass` only when every item passes.
