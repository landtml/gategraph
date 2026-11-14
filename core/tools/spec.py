"""Tool specification and guard contract (plan section 1.10, brief C4).

Every tool the native actor can call is a ``ToolSpec``. The guard sees a call
through ``ToolSpec.action_for(args)``, which returns the ``Action`` the decision
function judges. A ``ToolResult`` reports what the call touched, relative to the
workspace, so the post-call scan can be exact.

A tool's run function must never call ``interrupt()``. The guard middleware
decides and interrupts before the tool runs, so a tool body runs at most once
per attempt. A tool that interrupts from its own body is entered twice on
proceed (plan section 1.12, amended after F1 Q4).

Standard library plus ``core.kernel.types`` only. This module must import
without LangChain or LangGraph installed.
"""
from __future__ import annotations

import dataclasses
import hashlib
import pathlib
from typing import Any, Callable, Protocol, runtime_checkable

from core.kernel.types import Action, ActionKind, Budget, Spend, canonical_json

# Effects a tool may declare. Anything else is rejected at construction.
ALLOWED_EFFECTS = frozenset({"read", "write", "exec", "net"})

# The effect each action kind requires. ``edit`` is a write.
EFFECT_OF_KIND = {
    ActionKind.READ: "read",
    ActionKind.WRITE: "write",
    ActionKind.EDIT: "write",
    ActionKind.EXEC: "exec",
    ActionKind.NET: "net",
}


def _check_relative_path(path: Any) -> str:
    """A touched path must be a non-empty relative path with no parent escape."""
    if not isinstance(path, str) or path == "":
        raise ValueError(f"touched path must be a non-empty string, got {path!r}")
    posix = pathlib.PurePosixPath(path)
    windows = pathlib.PureWindowsPath(path)
    if posix.is_absolute() or windows.drive or windows.root:
        raise ValueError(f"touched path must be relative, got {path!r}")
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError(f"touched path must stay inside the workspace, got {path!r}")
    return path


@dataclasses.dataclass(frozen=True)
class ToolResult:
    """What a tool returns. ``touched`` lists workspace-relative paths it changed."""
    content: str
    touched: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    cost: Spend = Spend()

    def __post_init__(self) -> None:
        touched = tuple(self.touched)
        for path in touched:
            _check_relative_path(path)
        object.__setattr__(self, "touched", touched)
        object.__setattr__(self, "artifacts", tuple(self.artifacts))


@runtime_checkable
class EffectsJournal(Protocol):
    """Records tool results keyed by ``idempotency_key``.

    ``seen(key)`` returns the recorded result for a key, or None when the key has
    not been recorded. ``record(key, result)`` stores a result.
    """

    def seen(self, key: str) -> ToolResult | None: ...

    def record(self, key: str, result: ToolResult) -> None: ...


@dataclasses.dataclass(frozen=True)
class ToolContext:
    """Everything a tool body may use. Paths are absolute."""
    root: pathlib.Path
    unit_dir: pathlib.Path
    scratch_dir: pathlib.Path
    writable: tuple[str, ...]
    blind: tuple[str, ...]
    budget: Budget
    journal: EffectsJournal


@dataclasses.dataclass(frozen=True)
class ToolSpec:
    """One tool the native actor can call.

    ``run`` must never call ``interrupt()``: the guard middleware decides and
    interrupts before the tool runs (plan section 1.12).

    ``to_action`` maps the arguments to the ``Action`` the guard judges. Use
    ``action_for`` to call it, because ``action_for`` checks that the returned
    action's kind is covered by ``effects``.
    """
    name: str
    description: str
    args_schema: dict
    effects: frozenset[str]
    to_action: Callable[[dict], Action]
    run: Callable[[dict, ToolContext], ToolResult]
    idempotent: bool
    needs_approval: bool = False

    def __post_init__(self) -> None:
        effects = frozenset(self.effects)
        if not effects:
            raise ValueError(f"tool {self.name!r} declares no effects")
        undeclared = sorted(effects - ALLOWED_EFFECTS)
        if undeclared:
            raise ValueError(
                f"tool {self.name!r} declares unknown effect(s): {', '.join(undeclared)}"
            )
        object.__setattr__(self, "effects", effects)

    def action_for(self, args: dict) -> Action:
        """Call ``to_action`` and check that the action matches ``effects``."""
        action = self.to_action(args)
        if not isinstance(action, Action):
            raise TypeError(
                f"to_action of {self.name!r} must return an Action, "
                f"got {type(action).__name__}"
            )
        required = EFFECT_OF_KIND[action.kind]
        if required not in self.effects:
            raise ValueError(
                f"tool {self.name!r} returned a {action.kind.value} action "
                f"but does not declare the {required!r} effect"
            )
        return action


def idempotency_key(thread_id: str, tool_call_id: str) -> str:
    """sha256 over the canonical JSON of (thread_id, tool_call_id).

    Canonical JSON keeps the two parts separate, so ("ab", "c") and ("a", "bc")
    give different keys.
    """
    body = canonical_json([thread_id, tool_call_id])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
