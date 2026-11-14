"""Check plan-targets: the plan touches only its declared targets, all inside the sandbox.

Usage: check_targets.py <unit>. Exit 0 pass, 1 fatal (undeclared or escaping
target), 2 warn (a declared target with no step), per packs/ops/planfile.py.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import planfile  # noqa: E402


def main(argv):
    if len(argv) != 2:
        print("usage: check_targets.py <unit>", file=sys.stderr)
        return 1
    unit = argv[1]
    plan, findings = planfile.read_plan(unit)
    if plan is not None:
        findings += planfile.target_findings(plan, f"{unit}/{planfile.PLAN}")
    return planfile.emit(findings)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
