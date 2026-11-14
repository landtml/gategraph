"""The ops pack's apply tool (brief D6): irreversible, so it needs approval and is not idempotent.

``apply_spec(target_root=None)`` returns the ToolSpec. ``needs_approval`` is
True: the toolbox refuses a call unless the caller passes ``approved=True``, and
the tool never asks a human itself. ``idempotent`` is False: the toolbox records
the result under the call's idempotency key and replays it for a repeated call.

The example apply writes only under the unit's sandbox (``<unit>/sandbox``),
one file per write step of the unit's ``plan.yaml``. ``target_root`` lets an
adapter aim the same plan at another directory inside the workspace; no test
sets it. Before it writes anything, the apply re-checks the plan and the dry-run
log. Any fatal finding refuses the call, and nothing is written.

Standard library, the kernel types, and core.tools.spec / registry only.
"""
from __future__ import annotations

import pathlib
from typing import Optional

from core.kernel.types import Action, ActionKind
from core.packs.ops import planfile
from core.tools.registry import ToolRefused
from core.tools.spec import ToolContext, ToolResult, ToolSpec

NAME = "apply_plan"
DESCRIPTION = ("Apply the write steps of the unit's approved plan under the unit's sandbox. "
               "Irreversible: needs a human approval, and a repeated call is not re-run.")


def _to_action(args: dict) -> Action:
    # The action is judged as a write under the sandbox, not as the plan file.
    return Action(kind=ActionKind.WRITE, tool=NAME, path=planfile.SANDBOX)


def apply_spec(target_root: Optional[pathlib.Path] = None) -> ToolSpec:
    """The apply tool. With no ``target_root`` it writes under ``<unit>/sandbox``."""

    def run(args: dict, ctx: ToolContext) -> ToolResult:
        return _apply(ctx, target_root)

    return ToolSpec(
        name=NAME,
        description=DESCRIPTION,
        args_schema={"type": "object", "properties": {}, "required": [],
                     "additionalProperties": False},
        effects=frozenset({"write"}),
        to_action=_to_action,
        run=run,
        idempotent=False,
        needs_approval=True,
    )


def _apply(ctx: ToolContext, target_root: Optional[pathlib.Path]) -> ToolResult:
    unit = str(ctx.unit_dir)
    plan, found = planfile.read_plan(unit)
    if plan is not None:
        found += planfile.shape_findings(plan, unit)
        found += planfile.target_findings(plan, unit)
    log = ctx.unit_dir / planfile.DRY_RUN_LOG
    text = log.read_text(encoding="utf-8") if log.is_file() else None
    found += planfile.log_findings(plan, text, str(log))
    fatal = sorted({item["rule"] for item in found if item["severity"] == planfile.FATAL})
    if fatal:
        raise ToolRefused(f"{NAME} refused, the plan does not pass its checks: {', '.join(fatal)}")

    workspace = ctx.root.resolve()
    base = (ctx.unit_dir / planfile.SANDBOX) if target_root is None else pathlib.Path(target_root)
    base = base.resolve()
    try:
        base.relative_to(workspace)
    except ValueError:
        raise ToolRefused(f"{NAME} refused, the apply root is outside the workspace") from None

    # Resolve every destination before the first write, so a refusal writes nothing.
    pending = []
    for step in plan["steps"]:
        dest = (base / step["target"]).resolve()
        try:
            dest.relative_to(base)
        except ValueError:
            raise ToolRefused(f"{NAME} refused, {step['target']!r} resolves outside the apply root") from None
        pending.append((dest, step["content"]))

    written = []
    for dest, content in pending:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(dest.relative_to(workspace).as_posix())
    return ToolResult(content=f"applied {len(pending)} step(s)", touched=tuple(written))
