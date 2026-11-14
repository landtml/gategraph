"""Kernel decisions for one host event (plan 1.16; brief H1).

``handle(event, root, cfg)`` turns a HostEvent into a HostReply:

* pre_tool: the guard decides (``guard.decide``). A shell command also writes a
  stamp under the state directory, so the post-tool hook can tell what it touched.
* post_tool: the touched paths are scanned with the deny policies and the write-
  triggered checks; a fatal finding blocks. A touched path inside the active unit arms
  the session marker (the stop gate reads it).
* stop, session_start: not decided here; the legacy hooks keep them until their tasks.

``cfg`` is what ``config.load`` returned, or None when it could not be loaded; the
guard then fails closed for writes (its own rule), and post-tool fails open.

Touched paths after a shell command (B31): every file under the active unit whose
modification time is at or after the stamp written by the pre-tool hook. With no
stamp for the session, nothing is known to be touched and nothing is scanned.

Standard library, PyYAML and the kernel only.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml

from core import brand
from core.hosts.bridge import HostEvent, HostReply
from core.kernel import checks, guard, policy
from core.kernel.paths import marker_path, state_dir, valid_slug
from core.kernel.types import ActionKind, CheckStatus, Severity, Verdict

BUDGET_ENV = brand.ENV_PREFIX + "HOOK_CHECK_BUDGET"
DEFAULT_BUDGET_S = 75.0
STAMP_PREFIX = "shell-stamp-"
SKIP_PREFIXES = (brand.STATE_DIRNAME + "/", "harness/logs/")
_WRITE_KINDS = (ActionKind.WRITE, ActionKind.EDIT)


def handle(event: HostEvent, root: Path, cfg: Any) -> HostReply:
    if event.kind == "pre_tool":
        return _pre_tool(event, Path(root), cfg)
    if event.kind == "post_tool":
        return _post_tool(event, Path(root), cfg)
    return HostReply(Verdict.ALLOW)


# --------------------------------------------------------------------------
# pre-tool
# --------------------------------------------------------------------------

def _pre_tool(event: HostEvent, root: Path, cfg: Any) -> HostReply:
    action = event.action
    if action is None:
        return HostReply(Verdict.ALLOW)
    if action.kind is ActionKind.EXEC and event.session:
        _write_stamp(root, event.session)
    decision = guard.decide(action, cfg)
    if decision.verdict is Verdict.DENY:
        return HostReply(Verdict.DENY, reason=guard.denial_message(decision))
    if decision.verdict is Verdict.ASK:
        return HostReply(Verdict.ASK, reason=decision.reason)
    return HostReply(Verdict.ALLOW)


def _stamp_path(root: Path, session: str) -> Path:
    if not valid_slug(session):
        raise ValueError(f"invalid session id: {session!r}")
    return state_dir(root) / f"{STAMP_PREFIX}{session}"


def _write_stamp(root: Path, session: str) -> None:
    path = _stamp_path(root, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, None)  # the stamp's mtime is now, before the command runs


# --------------------------------------------------------------------------
# post-tool
# --------------------------------------------------------------------------

def _post_tool(event: HostEvent, root: Path, cfg: Any) -> HostReply:
    if cfg is None or event.action is None:
        return HostReply(Verdict.ALLOW)
    unit = _active_slug(root, cfg)
    touched = _touched(event, root, cfg, unit)
    if not touched:
        return HostReply(Verdict.ALLOW)
    if unit and event.session and any(_in_unit(rel, cfg, unit) for rel in touched):
        marker = marker_path(root, event.session)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    fatal, advice = _lint(root, cfg, touched, unit)
    if fatal:
        return HostReply(Verdict.DENY, reason=_blocked_text(fatal), block=True)
    if advice:
        return HostReply(Verdict.ALLOW, context=_advisory_text(advice))
    return HostReply(Verdict.ALLOW)


def _active_slug(root: Path, cfg: Any) -> str | None:
    pointer = root / cfg.unit_dir / ".active"
    if not pointer.is_file():
        return None
    try:
        data = yaml.safe_load(pointer.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    slug = data.get("slug") if isinstance(data, dict) else None
    return slug if valid_slug(slug) else None


def _in_unit(rel: str, cfg: Any, unit: str) -> bool:
    return rel.startswith(f"{cfg.unit_dir}/{unit}/")


def _relative(root: Path, text: str | None) -> str | None:
    if not text:
        return None
    try:
        variant = Path(text)
        if not variant.is_absolute():
            variant = root / variant
        return variant.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _touched(event: HostEvent, root: Path, cfg: Any, unit: str | None) -> list[str]:
    action = event.action
    if action.kind in _WRITE_KINDS:
        rel = _relative(root, action.path)
        found = [rel] if rel else []
    elif action.kind is ActionKind.EXEC:
        found = _touched_since_stamp(root, cfg, unit, event.session)
    else:
        found = []
    return [rel for rel in found if not rel.startswith(SKIP_PREFIXES)]


def _touched_since_stamp(root: Path, cfg: Any, unit: str | None,
                         session: str | None) -> list[str]:
    if not unit or not session:
        return []
    stamp = _stamp_path(root, session)
    if not stamp.is_file():
        return []
    since = stamp.stat().st_mtime_ns
    found = []
    for path in sorted((root / cfg.unit_dir / unit).rglob("*")):
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime_ns >= since:
            found.append(path.relative_to(root).as_posix())
    return found


def _budget_s() -> float:
    try:
        return float(os.environ.get(BUDGET_ENV, DEFAULT_BUDGET_S))
    except ValueError:
        return DEFAULT_BUDGET_S


def _lint(root: Path, cfg: Any, rels: list[str], unit: str | None) -> tuple[list[str], list[str]]:
    fatal: list[str] = []
    advice: list[str] = []
    rels = [rel for rel in rels if (root / rel).is_file()]  # a deleted file has nothing to lint
    policies = policy.compile_policies(cfg)
    for rel in rels:
        for finding in policy.scan_path(root / rel, root, policies).findings:
            line = f"[{finding.rule}] {rel}:{finding.line} {finding.message}"
            (fatal if finding.severity is Severity.FATAL else advice).append(line)
    selected = checks.select_checks(cfg.checks, rels)
    deadline = time.monotonic() + _budget_s()
    for spec in selected:
        result = checks.run_check(spec, root, files=rels, unit=unit or "", deadline=deadline)
        messages = [f.message for f in result.findings] or [f"check {spec.id}: {result.status.value}"]
        if result.status is CheckStatus.FATAL:
            fatal += [f"check {spec.id}: {m}" for m in messages]
        elif result.status in (CheckStatus.WARN, CheckStatus.SKIPPED, CheckStatus.DID_NOT_RUN):
            advice += [f"check {spec.id}: {m}" for m in messages]
    return fatal, advice


def _blocked_text(fatal: list[str]) -> str:
    lines = ["PostToolUse lint BLOCKED: fatal policy violation(s):"]
    lines += [f"  - {item}" for item in fatal]
    lines.append("Fix the content and re-write it (this content was flagged, "
                 "not accepted as final).")
    return "\n".join(lines)


def _advisory_text(advice: list[str]) -> str:
    return "\n".join(["PostToolUse lint (advisory, not blocking):"] + [f"  - {item}" for item in advice])
