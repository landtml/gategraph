"""Guard: the one policy decision for every enforcement point (plan section 1.9; brief K6).

``decide(action, cfg)`` is a pure function: the verdict depends only on the
action and the configuration object. Rules, first deny wins:

1. ``state-dir`` (built in, whatever the adapter says). A write, edit or exec
   whose path or command text names the state directory, or a legacy name, is
   denied.
2. Configuration. ``cfg`` must provide ``deny`` (policy dicts as
   ``policy.compile_policies`` reads them), ``ask`` (``{"paths", "reason"}``
   entries), ``protected_paths`` and ``blind`` (glob lists), and may provide
   ``root``. When ``cfg`` is None, lacks those fields, or a policy fails to
   compile, every action is denied except writes and edits under
   ``harness/adapter/`` (so the configuration can be repaired).
3. Write and edit: a protected path is denied; a fatal deny policy matching the
   normalized content is denied; an ask path returns ``ask``; warn-level
   findings are returned as advice and never deny.
4. Exec: a command that names a protected or blind path is denied; one that
   names an ask path returns ``ask``. Command matching is best effort.
5. Read: a blind path is denied.
6. Anything else is allowed.

Paths are repository-relative POSIX text. Absolute paths under ``cfg.root`` are
made relative; ``..`` and ``.`` segments collapse before matching.
"""
from __future__ import annotations

import dataclasses
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import brand
from core.kernel import policy
from core.kernel.config import ADAPTER_REL
from core.kernel.globs import glob_match
from core.kernel.types import Action, ActionKind, Decision, Finding, Severity, Verdict

__all__ = ["decide", "denial_message", "POLICY_SENTENCE"]

POLICY_SENTENCE = ("Policy outranks the task: an action that policy denies stays denied, "
                   "even when the task asks for it.")

_WRITE_KINDS = (ActionKind.WRITE, ActionKind.EDIT)
_POLICY_FIELDS = ("deny", "ask", "protected_paths", "blind")
_ANCHORS = ("/", "..", "$", "~")

# A state-directory name counts when it stands alone as a path segment, so
# "x.fh" or "notes-<name>.md" are not named.
_STATE_NAMES = (brand.STATE_DIRNAME, *brand.LEGACY_STATE_DIRNAMES)
_STATE_RE = re.compile(
    r"(?<![\w.\-])(?:" + "|".join(re.escape(name) for name in _STATE_NAMES) + r")(?![\w.\-])",
    re.IGNORECASE)
_SHELL_STRIP = str.maketrans("", "", "'\"\\")
_TOKEN_SPLIT = re.compile(r"[\s;&|()<>`'\"=,]+")

_STATE_REMEDY = (f"Only the runner writes to the state directory. Do not write, edit or run "
                 f"commands that name {brand.STATE_DIRNAME}; use the harness commands instead.")
_PROTECTED_REMEDY = ("This path is harness-writable only: gates and state are managed by the "
                     "harness, not edited by hand.")
_POLICY_REMEDY = "Remove the text that the policy matches, then retry."
_BLIND_REMEDY = ("This path is blind to the work step (rubrics, gold files and verifier code). "
                 "Work from the task inputs instead.")
_ASK_REMEDY = "A human must allow or refuse this action before it runs."
_UNLOADABLE_REMEDY = (f"Fix {ADAPTER_REL.as_posix()}, then retry. Writes under "
                      f"{ADAPTER_REL.parent.as_posix()}/ are still allowed.")


class _Unloadable(Exception):
    """The configuration cannot support policy decisions."""


@dataclass(frozen=True)
class _Rules:
    policies: tuple[dict, ...]
    protected: tuple[str, ...]
    blind: tuple[str, ...]
    ask: tuple[tuple[tuple[str, ...], str], ...]


# --------------------------------------------------------------------------
# Paths and text
# --------------------------------------------------------------------------

def _root_text(cfg: Any) -> str | None:
    root = getattr(cfg, "root", None)
    if root is None:
        return None
    text = Path(root).as_posix().rstrip("/")
    return text or None


def _relative(text: str, root: str | None) -> str:
    """Repository-relative POSIX form: root prefix removed, dot segments collapsed."""
    text = policy.normalize(text).replace("\\", "/")
    if root and text.startswith(root + "/"):
        text = text[len(root) + 1:]
    return posixpath.normpath(text) if text else ""


def _forms(rel: str) -> list[str]:
    """Spellings a path is matched under. An absolute or anchored path (one that may
    carry a prefix, such as ``$ROOT/x`` or ``../repo/x``) also matches by suffix."""
    forms = [rel]
    if rel.startswith(_ANCHORS):
        parts = rel.split("/")
        forms += ["/".join(parts[i:]) for i in range(1, len(parts))]
    return [form.casefold() for form in forms if form]


def _hits(patterns: tuple[str, ...], rel: str) -> bool:
    return any(glob_match(pattern, form) for pattern in patterns for form in _forms(rel))


def _tokens(command: str, root: str | None) -> list[str]:
    """Shell-ish tokens of a command, as repository-relative paths. Quotes and
    backslashes are removed in a second variant, so ``'.gate''bench'`` is seen."""
    norm = policy.normalize(command)
    out: list[str] = []
    for variant in {norm, norm.translate(_SHELL_STRIP)}:
        out += [_relative(raw, root) for raw in _TOKEN_SPLIT.split(variant) if raw]
    return out


def _names(patterns: tuple[str, ...], rel: str | None, command: str | None,
           root: str | None) -> bool:
    if not patterns:
        return False
    if rel and _hits(patterns, rel):
        return True
    return bool(command) and any(_hits(patterns, tok) for tok in _tokens(command, root))


def _ask_reason(entries: tuple[tuple[tuple[str, ...], str], ...], rel: str | None,
                command: str | None, root: str | None) -> str | None:
    for patterns, reason in entries:
        if _names(patterns, rel, command, root):
            return reason
    return None


def _names_state_dir(text: str | None) -> bool:
    if not text:
        return False
    norm = policy.normalize(text)
    return bool(_STATE_RE.search(norm) or _STATE_RE.search(norm.translate(_SHELL_STRIP)))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _globs(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{name} must be a list of globs")
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{name} entries must be strings")
    return tuple(item.casefold() for item in value)


def _rules(cfg: Any) -> _Rules:
    if cfg is None:
        raise _Unloadable("no configuration was loaded")
    missing = [name for name in _POLICY_FIELDS if not hasattr(cfg, name)]
    if missing:
        raise _Unloadable("the configuration has no " + ", ".join(missing))
    try:
        pols = tuple(p for p in policy.compile_policies(cfg) if p["pretool"])
        protected = _globs(cfg.protected_paths, "protected_paths")
        blind = _globs(cfg.blind, "blind")
        ask = tuple((_globs(entry["paths"], "ask.paths"),
                     str(entry.get("reason", "this path requires confirmation")))
                    for entry in cfg.ask)
    except Exception as exc:  # a policy set we cannot read refuses, it does not pass
        raise _Unloadable(f"{type(exc).__name__}: {exc}") from exc
    return _Rules(pols, protected, blind, ask)


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------

def _allow(reason: str = "no rule applies", findings: tuple[Finding, ...] = ()) -> Decision:
    return Decision(verdict=Verdict.ALLOW, rule=None, reason=reason, remedy=None,
                    findings=findings)


def _deny(rule: str, reason: str, remedy: str,
          findings: tuple[Finding, ...] = ()) -> Decision:
    return Decision(verdict=Verdict.DENY, rule=rule, reason=reason, remedy=remedy,
                    findings=findings)


def _in_scope(pol: dict, rel: str | None) -> bool:
    scopes = pol["scope"]
    if rel is None:
        return "any" in scopes
    return policy.scope_match(scopes, rel)


def _content_findings(content: str | None, rel: str | None,
                      pols: tuple[dict, ...]) -> tuple[Finding, ...]:
    if not content:
        return ()
    scoped = [pol for pol in pols if _in_scope(pol, rel)]
    if not scoped:
        return ()
    found = policy.scan_text(content, scoped, rel or "")
    return tuple(dataclasses.replace(finding, path=rel) for finding in found)


def _advice(findings: tuple[Finding, ...]) -> Decision:
    if not findings:
        return _allow()
    rules = ", ".join(sorted({finding.rule for finding in findings}))
    return _allow(f"allowed; {len(findings)} advisory finding(s) from warn-level policies: "
                  f"{rules}", findings)


def _unloadable(action: Action, rel: str | None, why: str) -> Decision:
    adapter_write = (action.kind in _WRITE_KINDS and rel is not None
                     and rel.casefold().startswith(ADAPTER_REL.parent.as_posix() + "/"))
    if adapter_write:
        return _allow(f"the configuration cannot be loaded ({why}); the write is allowed "
                      f"because it targets {ADAPTER_REL.parent.as_posix()}/ so the "
                      f"configuration can be repaired")
    return _deny("config-unloadable",
                 f"the configuration cannot be loaded ({why}), so policy cannot run; every "
                 f"action is denied except writes under {ADAPTER_REL.parent.as_posix()}/",
                 _UNLOADABLE_REMEDY)


def _write(action: Action, rel: str | None, rules: _Rules, root: str | None) -> Decision:
    if rel is not None and _hits(rules.protected, rel):
        return _deny("protected-path", f"{rel} is a protected path", _PROTECTED_REMEDY)
    findings = _content_findings(action.content, rel, rules.policies)
    fatal = [finding for finding in findings if finding.severity is Severity.FATAL]
    if fatal:
        first = fatal[0]
        return _deny(first.rule, f"[{first.rule}] {first.message} (line {first.line})",
                     _POLICY_REMEDY, findings)
    reason = _ask_reason(rules.ask, rel, None, root)
    if reason is not None:
        return Decision(verdict=Verdict.ASK, rule="ask-path", reason=reason,
                        remedy=_ASK_REMEDY, findings=findings)
    return _advice(findings)


def _exec(action: Action, rel: str | None, rules: _Rules, root: str | None) -> Decision:
    command = action.command
    if _names(rules.protected, rel, command, root):
        return _deny("protected-path", "the command names a protected path", _PROTECTED_REMEDY)
    if _names(rules.blind, rel, command, root):
        return _deny("blind-path", "the command names a blind path", _BLIND_REMEDY)
    reason = _ask_reason(rules.ask, rel, command, root)
    if reason is not None:
        return Decision(verdict=Verdict.ASK, rule="ask-path", reason=reason,
                        remedy=_ASK_REMEDY, findings=())
    return _allow()


def _read(rel: str | None, rules: _Rules, root: str | None) -> Decision:
    if _names(rules.blind, rel, None, root):
        return _deny("blind-path", f"{rel} is a blind path", _BLIND_REMEDY)
    return _allow()


def decide(action: Action, cfg: Any) -> Decision:
    """The decision for one action under the configuration ``cfg`` (None when the
    configuration could not be loaded). Pure: no I/O, no state."""
    root = _root_text(cfg)
    rel = _relative(action.path, root) if action.path else None
    if action.kind in _WRITE_KINDS or action.kind is ActionKind.EXEC:
        if _names_state_dir(action.path) or _names_state_dir(action.command):
            return _deny("state-dir",
                         f"the action names the state directory ({brand.STATE_DIRNAME}); "
                         f"only the runner writes there",
                         _STATE_REMEDY)
    try:
        rules = _rules(cfg)
    except _Unloadable as exc:
        return _unloadable(action, rel, str(exc))
    if action.kind is ActionKind.READ:
        return _read(rel, rules, root)
    if action.kind in _WRITE_KINDS:
        return _write(action, rel, rules, root)
    if action.kind is ActionKind.EXEC:
        return _exec(action, rel, rules, root)
    return _allow()


def denial_message(decision: Decision) -> str:
    """Text for the model or the host: the rule, the reason, the remedy, and the
    instruction hierarchy sentence that every denial carries."""
    lines = [
        f"Rule: {decision.rule or 'none'}",
        f"Reason: {decision.reason}",
        f"Remedy: {decision.remedy or 'none given'}",
    ]
    lines += [f"Finding: [{f.rule}] line {f.line}: {f.message}" for f in decision.findings]
    lines.append(POLICY_SENTENCE)
    return "\n".join(lines)
