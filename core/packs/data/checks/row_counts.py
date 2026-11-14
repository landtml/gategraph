"""data-row-counts: the number of data rows is within the declared bounds."""
import sys

import _table


def check(spec, table):
    count = len(table.rows)
    low, high = spec["rows"]["min"], spec["rows"]["max"]
    if count < low:
        return [_table.fatal("rows.below-min",
                             f"{count} data rows, the declared minimum is {low}", table.path)]
    if count > high:
        return [_table.fatal("rows.above-max",
                             f"{count} data rows, the declared maximum is {high}", table.path)]
    return []


if __name__ == "__main__":
    sys.exit(_table.run(check))
