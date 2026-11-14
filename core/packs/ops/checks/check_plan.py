"""Check plan-parses: the unit's plan.yaml exists, parses, and has the plan shape.

Usage: check_plan.py <unit>. Exit 0 pass, 1 fatal, per packs/ops/planfile.py.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import planfile  # noqa: E402


def main(argv):
    if len(argv) != 2:
        print("usage: check_plan.py <unit>", file=sys.stderr)
        return 1
    unit = argv[1]
    plan, findings = planfile.read_plan(unit)
    if plan is not None:
        findings += planfile.shape_findings(plan, f"{unit}/{planfile.PLAN}")
    return planfile.emit(findings)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
