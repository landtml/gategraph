"""Plan files of the ops pack (brief D6): the plan, its declared targets, the dry-run log.

A unit holds ``plan.yaml`` and, after a dry run, ``dry_run.log``. Every target
is a path relative to the unit's sandbox. The three check scripts (run as
subprocesses under the exit-code contract) and the apply tool all read plans
through this module, so they agree on every rule.

Findings are dicts with keys rule, severity, path, line, message; ``emit``
prints them one JSON object per line and returns the exit code: 0 pass, 1 when
a finding is fatal, 2 when only warnings were found. Standard library and
PyYAML only.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

import yaml

PLAN = "plan.yaml"
DRY_RUN_LOG = "dry_run.log"
SANDBOX = "sandbox"      # the apply tool writes here, under the unit
VERSION = 1
OPS = ("write",)
FATAL = "fatal"
WARN = "warn"


def finding(rule: str, severity: str, path: str, message: str) -> dict:
    return {"rule": rule, "severity": severity, "path": path, "line": None, "message": message}


def emit(findings: list) -> int:
    """Print the findings as JSON lines and return the check exit code."""
    for item in findings:
        print(json.dumps(item, sort_keys=True))
    if any(item["severity"] == FATAL for item in findings):
        return 1
    return 2 if findings else 0


def read_plan(unit: str) -> tuple[Any, list]:
    """(the parsed plan or None, findings). ``unit`` is relative to the project root."""
    where = f"{unit}/{PLAN}"
    path = pathlib.Path(unit) / PLAN
    if not path.is_file():
        return None, [finding("plan.missing", FATAL, where, "plan.yaml is missing from the unit")]
    try:
        plan = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        return None, [finding("plan.unparseable", FATAL, where, f"plan.yaml is not valid YAML: {exc}")]
    if not isinstance(plan, dict):
        return None, [finding("plan.shape", FATAL, where, "plan.yaml root must be a mapping")]
    return plan, []


def shape_findings(plan: dict, where: str) -> list:
    """The plan has the declared shape: version, a target list, and write steps."""
    out: list = []

    def bad(message: str) -> None:
        out.append(finding("plan.shape", FATAL, where, message))

    if plan.get("version") != VERSION:
        bad(f"version must be {VERSION}")
    targets = plan.get("targets")
    if not isinstance(targets, list) or not targets:
        bad("targets must be a non-empty list")
    elif not all(isinstance(t, str) and t for t in targets):
        bad("every target must be a non-empty string")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        bad("steps must be a non-empty list")
        return out
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            bad(f"steps[{index}] must be a mapping")
            continue
        if step.get("op") not in OPS:
            bad(f"steps[{index}].op must be one of {list(OPS)}")
        if not isinstance(step.get("target"), str):
            bad(f"steps[{index}].target must be a string")
        if not isinstance(step.get("content"), str):
            bad(f"steps[{index}].content must be a string")
    return out


def target_reason(target: str) -> str | None:
    """Why a target is not a safe relative path inside the sandbox, or None."""
    if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
        return "is absolute"
    if "\\" in target:
        return "uses a backslash"
    if ".." in target.split("/"):
        return "leaves the sandbox"
    return None


def target_findings(plan: dict, where: str) -> list:
    """Every target is safe and declared, and every step writes a declared target.

    A plan whose shape is wrong is reported by ``shape_findings``; this returns
    nothing for it, so one problem is not reported twice.
    """
    targets, steps = plan.get("targets"), plan.get("steps")
    if not isinstance(targets, list) or not isinstance(steps, list):
        return []
    out: list = []
    declared: set[str] = set()
    for index, target in enumerate(targets):
        if not isinstance(target, str):
            continue
        declared.add(target)
        reason = target_reason(target)
        if reason:
            out.append(finding("plan.target-escapes", FATAL, where,
                               f"targets[{index}] {target!r} {reason}"))
    used: set[str] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("target"), str):
            continue
        target = step["target"]
        used.add(target)
        if target not in declared:
            out.append(finding("plan.target-undeclared", FATAL, where,
                               f"steps[{index}] writes {target!r}, which the plan does not declare"))
    for target in sorted(declared - used):
        out.append(finding("plan.target-unused", WARN, where,
                           f"declared target {target!r} has no step"))
    return out


def dry_run_lines(plan: dict) -> list[str]:
    """The dry-run log for ``plan``: one line per step, ``OK`` or ``ERROR``. Writes nothing."""
    declared = set(plan["targets"])
    lines = []
    for step in plan["steps"]:
        op, target = step["op"], step["target"]
        reason = target_reason(target)
        if reason:
            lines.append(f"ERROR {op} {target}: {reason}")
        elif target not in declared:
            lines.append(f"ERROR {op} {target}: not a declared target")
        else:
            lines.append(f"OK {op} {target}")
    return lines


def log_findings(plan: dict | None, text: str | None, where: str) -> list:
    """The findings of a dry-run log. ``text`` is None when the log is absent.

    Every line must be ``OK <op> <target>`` or ``ERROR ...``. A plan step with no
    OK line is incomplete. ``plan`` may be None (its own problems are reported
    by ``read_plan``); the coverage test is then skipped.
    """
    if text is None:
        return [finding("dry_run.missing", FATAL, where, "dry_run.log is missing from the unit")]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return [finding("dry_run.empty", FATAL, where, "dry_run.log has no lines")]
    out: list = []
    ok_targets: set[str] = set()
    for number, line in enumerate(lines, 1):
        if line.startswith("ERROR"):
            out.append(finding("dry_run.error", FATAL, where, f"line {number}: {line}"))
        elif line.startswith("OK ") and len(line.split(" ", 2)) == 3:
            ok_targets.add(line.split(" ", 2)[2])
        else:
            out.append(finding("dry_run.unrecognised", FATAL, where,
                               f"line {number} is neither OK nor ERROR: {line}"))
    if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
        for index, step in enumerate(plan["steps"]):
            if isinstance(step, dict) and isinstance(step.get("target"), str):
                if step["target"] not in ok_targets:
                    out.append(finding("dry_run.incomplete", FATAL, where,
                                       f"no OK line for steps[{index}] ({step['target']})"))
    return out
