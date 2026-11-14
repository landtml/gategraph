"""data-schema: the header and the values match the columns declared in spec.json."""
import sys

import _table


def check(spec, table):
    declared = [column["name"] for column in spec["columns"]]
    found = table.columns
    where = table.path
    findings = []
    for name in sorted(set(found)):
        if found.count(name) > 1:
            findings.append(_table.fatal("schema.duplicate-column",
                                         f"column '{name}' appears more than once", where, 1))
    for name in declared:
        if name not in found:
            findings.append(_table.fatal("schema.missing-column",
                                         f"column '{name}' is declared but not in the header",
                                         where, 1))
    for name in found:
        if name not in declared:
            findings.append(_table.fatal("schema.unexpected-column",
                                         f"column '{name}' is in the header but not declared",
                                         where, 1))
    if not findings and declared != found:
        findings.append(_table.fatal("schema.column-order",
                                     "the header order differs from the declared order",
                                     where, 1))
    for column in spec["columns"]:
        if column["name"] not in found:
            continue
        index = found.index(column["name"])
        for line, values in table.rows:
            value = values[index]
            if value is not None and not _table.conforms(column["type"], value):
                findings.append(_table.fatal(
                    "schema.type-mismatch",
                    f"column '{column['name']}' expects {column['type']}, got '{value}'",
                    where, line))
                break
    return findings


if __name__ == "__main__":
    sys.exit(_table.run(check))
