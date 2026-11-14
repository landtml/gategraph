A check on this dataset unit failed. The failing gate, check and finding are
listed below. Fix only the file the finding names.

- Schema findings: change the header or the values in rows.csv to match
  spec.json. Change spec.json only if the declared column itself is wrong, and
  say so.
- Row-count findings: add or remove data rows within the declared bounds. Do not
  change the bounds to make the count pass.
- Null-rate findings: fill the empty cells from the source, or state in the
  notes why they stay empty. Do not change max_null_rate to make the check pass.
- Total findings: correct the rows if the sum is wrong. Do not change the
  declared total to match a wrong sum.
