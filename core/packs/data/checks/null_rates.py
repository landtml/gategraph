"""data-null-rates: no column's share of empty cells exceeds max_null_rate in spec.json."""
import sys
from fractions import Fraction

import _table


def check(spec, table):
    total = len(table.rows)
    if total == 0:
        return []  # an empty table is the row-count check's finding, not this one's
    limit = Fraction(spec["max_null_rate"])
    findings = []
    for index, name in enumerate(table.columns):
        nulls = sum(1 for _, values in table.rows if values[index] is None)
        if Fraction(nulls, total) > limit:
            findings.append(_table.fatal(
                "nulls.over-threshold",
                f"column '{name}' is empty in {nulls} of {total} rows, "
                f"above the declared rate {spec['max_null_rate']}",
                table.path))
    return findings


if __name__ == "__main__":
    sys.exit(_table.run(check))
