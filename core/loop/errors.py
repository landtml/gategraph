"""Failure routing, the load-bearing split between *infra* and *variant* faults.

The single most damaging bug this loop can have is charging an **infrastructure** failure (a
claude spawn error, a rate-limit, an oracle exception on good code) to the **variant** as a
strike or, worse, a dead-end cell. That silently poisons the research memory: a lever that
was never actually falsified gets recorded as exhausted, and the maker never revisits it.

So every failure is routed to exactly one of:
- ``Route.INFRA``, not the variant's fault. **Escalate** (write an ``ESCALATION`` receipt a
  human reads async), do **not** strike, do **not** record a dead-end. ``RateLimited``
  additionally sets a backoff.
- ``Route.VARIANT``, attributable to the variant's own code/output (a contract violation,
  a smoke bug). Counts as a **strike**; enough strikes → a clean REJECT.

Only ``ContractError`` / ``SmokeError`` are variant faults; everything else is infra by
default, we fail *toward* not blaming the variant, because a false dead-end is costlier
than a wasted retry. ``EscalationRequested`` exists specifically for the §12 driver-backstop
(fail streak / disagreement threshold), a route neither infra nor a variant fault.
"""
from __future__ import annotations

import hashlib
import re
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.loop.workers import ContractError, RateLimited, WorkerError


class SmokeError(WorkerError):
    """The variant failed the smoke dry-run (missing/empty/broken variant file).

    A *variant* fault so it strikes and drives the repair loop, but it must **not**
    create a dead-end cell (a bug is not a falsified mechanism). ``traceback_text`` is fed
    back to a fresh maker for repair."""

    def __init__(self, message: str, traceback_text: str = "") -> None:
        super().__init__(message)
        self.traceback_text = traceback_text


class EscalationRequested(Exception):
    """The §12 confidence tier fired (fail streak / self-consistency disagreement above τ):
    stop and escalate to a human rather than confidently continuing. Routed as INFRA with an
    escalation receipt; the driver additionally requests a stop."""


class Route(str, Enum):
    INFRA = "infra"
    VARIANT = "variant"


@dataclass
class Routing:
    route: Route
    strike: bool  # does this count against the variant's strike budget?
    escalate: bool  # write an ESCALATION receipt for a human?
    backoff: bool  # set a rate-limit backoff before retrying?
    reason: str

    @property
    def is_infra(self) -> bool:
        return self.route is Route.INFRA


# Variant-attributable exception types. Everything else routes to INFRA (fail toward not blaming).
_VARIANT_TYPES = (ContractError, SmokeError)


def classify(exc: BaseException) -> Routing:
    """Map an exception to a :class:`Routing`. The driver acts on the routing, never the raw type."""
    if isinstance(exc, _VARIANT_TYPES):
        return Routing(Route.VARIANT, strike=True, escalate=False, backoff=False,
                       reason=f"variant fault: {type(exc).__name__}: {exc}")
    if isinstance(exc, RateLimited):
        return Routing(Route.INFRA, strike=False, escalate=False, backoff=True,
                       reason="rate-limited: back off and retry the same phase")
    # AuthError, BudgetExceeded, WorkerTimeout, SpawnError, EscalationRequested, oracle
    # exceptions, anything unexpected.
    return Routing(Route.INFRA, strike=False, escalate=True, backoff=False,
                   reason=f"infra fault: {type(exc).__name__}: {exc}")


def _tb(exc: BaseException) -> str:
    if getattr(exc, "traceback_text", ""):
        return exc.traceback_text  # SmokeError carries the variant's own traceback
    if exc.__traceback__ is not None:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return f"{type(exc).__name__}: {exc}"


def write_escalation(
    root: "str | Path",
    *,
    iteration: int,
    phase: str,
    exc: BaseException,
    variant: "str | None" = None,
    next_action: str = "A human should inspect this failure; the loop continued past it.",
) -> Path:
    """Write a human-readable ``ESCALATION`` receipt under ``<root>/escalations/`` and return its path.

    The loop never stalls on an escalation (escalate, never silently retry forever), it
    records this and moves on; the human reads escalations asynchronously.

    **Coalescing (storm control).** Identical faults, same variant, phase, exception type
    and message head, are keyed to ONE signature file: the first writes the full receipt, each
    repeat bumps an ``occurrences`` counter and the ``last-seen`` stamp instead of spawning a
    new file. This structurally caps a misclassified hot-loop at one receipt per distinct fault."""
    d = Path(root) / "escalations"
    d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    head = (str(exc).splitlines() or [""])[0][:160]
    sig = hashlib.sha1(f"{variant}|{phase}|{type(exc).__name__}|{head}".encode()).hexdigest()[:12]
    path = d / f"ESCALATION_{sig}.md"

    if path.exists():  # coalesce a repeat of the same fault: bump the counter, refresh last-seen
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^- occurrences: (\d+)$",
                      lambda m: f"- occurrences: {int(m.group(1)) + 1}", text, count=1)
        text = re.sub(r"(?m)^- last-seen: .*$", f"- last-seen: {ts}", text, count=1)
        path.write_text(text, encoding="utf-8")
        return path

    routing = classify(exc)
    body = (
        f"# ESCALATION: iteration {iteration}, phase {phase}\n\n"
        f"- when: {ts}\n"
        f"- last-seen: {ts}\n"
        f"- occurrences: 1\n"
        f"- variant: {variant or 'none'}\n"
        f"- route: {routing.route.value} (strike={routing.strike}, backoff={routing.backoff})\n"
        f"- exception: {type(exc).__name__}: {exc}\n"
        f"- next action: {next_action}\n\n"
        f"## Traceback\n\n```\n{_tb(exc)}\n```\n"
    )
    path.write_text(body, encoding="utf-8")
    return path
