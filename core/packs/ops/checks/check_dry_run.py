"""Check dry-run-clean: the unit's dry_run.log exists and shows no error.

Usage: check_dry_run.py <unit>. Exit 0 pass, 1 fatal, per packs/ops/planfile.py.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import planfile  # noqa: E402


def main(argv):
    if len(argv) != 2:
        print("usage: check_dry_run.py <unit>", file=sys.stderr)
        return 1
    unit = argv[1]
    path = pathlib.Path(unit) / planfile.DRY_RUN_LOG
    text = path.read_text(encoding="utf-8") if path.is_file() else None
    plan, _ = planfile.read_plan(unit)
    findings = planfile.log_findings(plan, text, f"{unit}/{planfile.DRY_RUN_LOG}")
    return planfile.emit(findings)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
