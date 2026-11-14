"""Model ports (section 1.11): ``api`` (tool-calling chat models), ``claude_cli``
(subprocess, tool-less, for verifiers) and the scripted ``fake`` in core.agent.models.

Faults are defined here, before the submodules import, so that ``claude_cli`` and
``api`` can raise them without a circular import. Each fault carries ``spend``: the
real spend of every attempt made before it was raised, so the budget backstop sees it.
"""
from __future__ import annotations


class ModelFault(Exception):
    """Base for port failures. ``retryable`` tells the caller whether a later call may succeed."""

    retryable: bool = False

    def __init__(self, message: str, *, spend=None) -> None:
        super().__init__(message)
        self.spend = spend
        # Spend of the failing attempt itself, set before raising (claude_cli).
        self.usd = 0.0
        self.tokens = 0


class SpawnFault(ModelFault):
    """The CLI could not be launched, exited without usable output, or errored unclassified."""


class AuthFault(ModelFault):
    """Subscription or login problem. Escalate; do not retry blindly."""


class RateLimitFault(ModelFault):
    """A usage or rate limit. Retryable after the caller backs off."""

    retryable = True


class BudgetFault(ModelFault):
    """The per-call budget cap tripped."""


class TimeoutFault(ModelFault):
    """The subprocess exceeded its wall-clock timeout."""


class ContractFault(ModelFault):
    """Output stayed malformed or schema-violating after every strike."""

    def __init__(self, message: str, errors: "list[str]", *, spend=None) -> None:
        super().__init__(message, spend=spend)
        self.errors = errors


from core.agent.ports.claude_cli import ClaudeCliPort  # noqa: E402
from core.agent.ports.api import ApiPort  # noqa: E402

__all__ = [
    "ModelFault", "SpawnFault", "AuthFault", "RateLimitFault", "BudgetFault",
    "TimeoutFault", "ContractFault", "ClaudeCliPort", "ApiPort",
]
