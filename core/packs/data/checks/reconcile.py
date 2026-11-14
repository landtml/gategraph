"""data-reconcile: each declared total equals the exact sum of its column.

Sums are decimal, never float. Empty cells contribute nothing (the null-rate
check reports them).
"""
import sys
from decimal import Decimal

import _table


def check(spec, table):
    findings = []
    for total in spec["totals"]:
        name = total["column"]
        where = table.path
        if name not in table.columns:
            findings.append(_table.fatal("totals.unknown-column",
                                         f"total for column '{name}', which is not in the header",
                                         where, 1))
            continue
        index = table.columns.index(name)
        acc = Decimal(0)
        bad = False
        for line, values in table.rows:
            value = values[index]
            if value is None:
                continue
            if not _table.DECIMAL.fullmatch(value):
                findings.append(_table.fatal("totals.not-numeric",
                                             f"column '{name}' has a non-numeric value '{value}'",
                                             where, line))
                bad = True
                break
            acc += Decimal(value)
        if bad:
            continue
        declared = Decimal(total["declared"])
        if acc != declared:
            findings.append(_table.fatal("totals.mismatch",
                                         f"column '{name}' sums to {acc}, "
                                         f"the declared total is {declared}", where))
    return findings


if __name__ == "__main__":
    sys.exit(_table.run(check))
