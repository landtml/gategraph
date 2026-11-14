"""Fault classification: variant versus infrastructure (plan K7; core/loop/errors.py).

The rule the loop learned the hard way: an infrastructure failure (a spawn error, a rate
limit, an oracle exception on good code) must never be charged to the variant as a strike,
because that poisons the record. Every failure therefore maps to exactly one ``FaultRoute``.

- Variant (strike, not retryable): ``ContractError`` and ``SmokeError``.
- Rate limit (infrastructure, retryable after ``backoff_s``, no strike).
- Everything else is infrastructure, not retryable, no strike. Unknown failures fail toward
  not blaming the variant and toward escalation rather than a blind retry.

The kernel cannot import ``core.loop``, so classes are matched by name along the MRO. The
match is on the exception type, never on message text (bug B24).

Standard library only.
"""
from __future__ import annotations

from core.kernel.types import FaultKind, FaultRoute

# Backoff used when a rate-limit exception carries no usable ``retry_after_s`` hint.
DEFAULT_RATE_LIMIT_BACKOFF_S = 60.0

_VARIANT_NAMES = frozenset({"ContractError", "SmokeError"})
_RATE_LIMIT_NAMES = frozenset({"RateLimited"})


def _names_in_mro(exc: BaseException) -> set[str]:
    return {klass.__name__ for klass in type(exc).__mro__}


def _backoff_hint(exc: BaseException) -> float:
    hint = getattr(exc, "retry_after_s", None)
    if isinstance(hint, (int, float)) and not isinstance(hint, bool) and hint >= 0:
        return float(hint)
    return DEFAULT_RATE_LIMIT_BACKOFF_S


def classify(exc: BaseException) -> FaultRoute:
    """Map an exception to a :class:`FaultRoute`. The caller acts on the route only."""
    names = _names_in_mro(exc)
    label = type(exc).__name__
    # Rate limit is checked first, so it can never be a variant strike.
    if names & _RATE_LIMIT_NAMES:
        return FaultRoute(
            fault=FaultKind.INFRASTRUCTURE,
            retryable=True,
            strike=False,
            backoff_s=_backoff_hint(exc),
            reason=f"rate-limited ({label}): back off and retry the same phase",
        )
    if names & _VARIANT_NAMES:
        return FaultRoute(
            fault=FaultKind.VARIANT,
            retryable=False,
            strike=True,
            backoff_s=None,
            reason=f"variant fault ({label}): {exc}",
        )
    return FaultRoute(
        fault=FaultKind.INFRASTRUCTURE,
        retryable=False,
        strike=False,
        backoff_s=None,
        reason=f"infrastructure fault ({label}): {exc}",
    )


def is_retryable(exc: BaseException) -> bool:
    """True when the failure should be retried after a delay (rate limits only)."""
    return classify(exc).retryable
