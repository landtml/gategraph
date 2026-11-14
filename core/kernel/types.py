"""Kernel types: the shared vocabulary of the rebuild.

Every type is a frozen dataclass (or an enum) with ``to_dict()`` returning only
JSON-plain values (dict, list, str, int, float, bool, None) and ``from_dict()``
rebuilding the object. ``from_dict`` rejects unknown fields. Enums serialize as
their string value. Standard library only.
"""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import types as _pytypes
import typing
from typing import Any, Union, get_args, get_origin

# --------------------------------------------------------------------------
# Plain conversion and canonical JSON
# --------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    """Convert a value to JSON-plain form (dataclasses, enums, tuples, dicts)."""
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def canonical_json(obj: Any) -> str:
    """Byte-stable JSON: sorted keys, no whitespace, non-ASCII kept as is."""
    return json.dumps(_plain(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Coercion driven by type hints (used by __post_init__ and from_dict)
# --------------------------------------------------------------------------

_HINTS_CACHE: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    cached = _HINTS_CACHE.get(cls)
    if cached is None:
        cached = typing.get_type_hints(cls)
        _HINTS_CACHE[cls] = cached
    return cached


def _coerce(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(hint)
    if origin is Union or origin is _pytypes.UnionType:
        options = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(options) == 1:
            return _coerce(options[0], value)
        return value
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return value if isinstance(value, hint) else hint(value)
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        if isinstance(value, hint):
            return value
        if isinstance(value, dict):
            return hint.from_dict(value)
        raise TypeError(f"expected a mapping for {hint.__name__}, got {type(value).__name__}")
    if origin is tuple:
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_coerce(args[0], item) for item in value)
        return tuple(_coerce(arg, item) for arg, item in zip(args, value))
    if origin is list:
        (arg,) = get_args(hint)
        return [_coerce(arg, item) for item in value]
    if origin is dict:
        _, arg = get_args(hint)
        return {key: _coerce(arg, item) for key, item in value.items()}
    return value


class _Plain:
    """Mixin: plain conversion, strict rebuild, and field coercion."""

    def __post_init__(self) -> None:
        hints = _hints(type(self))
        for field in dataclasses.fields(self):
            current = getattr(self, field.name)
            coerced = _coerce(hints[field.name], current)
            if coerced is not current:
                object.__setattr__(self, field.name, coerced)

    def to_dict(self) -> dict:
        return {field.name: _plain(getattr(self, field.name))
                for field in dataclasses.fields(self)}

    @classmethod
    def from_dict(cls, data: dict):
        if not isinstance(data, dict):
            raise TypeError(f"{cls.__name__}.from_dict expects a dict")
        known = {field.name for field in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"{cls.__name__} has no field(s): {', '.join(unknown)}")
        return cls(**data)


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class Severity(str, enum.Enum):
    FATAL = "fatal"
    WARN = "warn"


class ActionKind(str, enum.Enum):
    WRITE = "write"
    EDIT = "edit"
    EXEC = "exec"
    NET = "net"
    READ = "read"


class Verdict(str, enum.Enum):
    """Guard verdict (section 1.9)."""
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class CheckStatus(str, enum.Enum):
    PASS = "pass"
    FATAL = "fatal"
    WARN = "warn"
    DID_NOT_RUN = "did_not_run"
    SKIPPED = "skipped"


class ProducerKind(str, enum.Enum):
    CHECK = "check"
    VERIFIER = "verifier"
    HUMAN = "human"


class RecordKind(str, enum.Enum):
    GATE = "gate"
    APPROVAL = "approval"
    ESCALATION = "escalation"
    WAIVER = "waiver"
    IMPORT = "import"
    CONFIG = "config"


class ActorKind(str, enum.Enum):
    TOOL = "tool"
    VERIFIER = "verifier"
    HUMAN = "human"


class HumanKind(str, enum.Enum):
    """The four interrupt kinds (section 1.12)."""
    WORK = "work"
    APPROVAL = "approval"
    ASK = "ask"
    ESCALATION = "escalation"


class HumanAnswer(str, enum.Enum):
    """Every answer a human can give (section 1.12)."""
    ATTEMPT = "attempt"
    APPROVE = "approve"
    REJECT = "reject"
    ALLOW = "allow"
    REFUSE = "refuse"
    RETRY = "retry"
    ABANDON = "abandon"
    WAIVE = "waive"


VERIFIER_VERDICTS = ("pass", "fail", "did_not_run")

# Section 1.12 table: which answers each request kind accepts. ``waive`` is
# offered only when the adapter allows waivers; that is configuration, not type.
HUMAN_ANSWERS = {
    HumanKind.WORK: (HumanAnswer.ATTEMPT,),
    HumanKind.APPROVAL: (HumanAnswer.APPROVE, HumanAnswer.REJECT),
    HumanKind.ASK: (HumanAnswer.ALLOW, HumanAnswer.REFUSE),
    HumanKind.ESCALATION: (HumanAnswer.RETRY, HumanAnswer.ABANDON, HumanAnswer.WAIVE),
}


# --------------------------------------------------------------------------
# Findings, actions, decisions (sections 1.9, 1.10)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Finding(_Plain):
    rule: str
    severity: Severity
    path: str | None
    line: int | None
    message: str
    # Check output (section 1.9, JSON on standard output) carries no remedy.
    remedy: str | None = None


@dataclasses.dataclass(frozen=True)
class Action(_Plain):
    kind: ActionKind
    tool: str
    path: str | None = None
    content: str | None = None
    command: str | None = None
    url: str | None = None


@dataclasses.dataclass(frozen=True)
class Decision(_Plain):
    """Result of guard.decide (section 1.9)."""
    verdict: Verdict
    rule: str | None
    reason: str
    remedy: str | None
    findings: tuple[Finding, ...]


# --------------------------------------------------------------------------
# Checks and verifiers (sections 1.7, 1.13 and the K5, G3, G5 briefs)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class CheckSpec(_Plain):
    id: str
    command: str
    severity: Severity
    timeout_s: float | None = None
    # Glob list of written paths that trigger this check after a write (section
    # 1.9 write-triggered checks; section 1.13 pack ``on:``). Empty means gate-only.
    on: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class CheckResult(_Plain):
    id: str
    exit: int | None
    status: CheckStatus
    findings: tuple[Finding, ...]
    duration_s: float
    # sha256 of the command string plus the bytes of the script it names
    # (section 1.8, producer digest for a check). Computed where the script runs.
    digest: str


@dataclasses.dataclass(frozen=True)
class VerifierSpec(_Plain):
    id: str
    rubric: str
    n: int
    model: str


@dataclasses.dataclass(frozen=True)
class VerifierVerdict(_Plain):
    id: str
    run: int
    verdict: str
    findings: tuple[Finding, ...]
    digest: str
    # sha256 of the artifact bytes the verifier judged; a mismatch with the
    # surface discards the verdict (brief G5, section 1.11 blinded subagent).
    artifact_sha256: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.verdict not in VERIFIER_VERDICTS:
            raise ValueError(f"verdict must be one of {VERIFIER_VERDICTS}, got {self.verdict!r}")


# --------------------------------------------------------------------------
# Gates (section 1.7)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Approval(_Plain):
    required: bool
    channel: str


@dataclasses.dataclass(frozen=True)
class SurfaceSpec(_Plain):
    """Glob rules that decide which files a gate's surface covers."""
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class RepairSpec(_Plain):
    """``repair: {max_attempts: N}`` in the gate YAML (section 1.7)."""
    max_attempts: int


@dataclasses.dataclass(frozen=True)
class GateSpec(_Plain):
    """One gate. Section 1.7 YAML omits most keys; the defaults match that.

    ``needs`` defaults to the previous gate in the YAML; the loader resolves
    that before construction, so the type default is the empty tuple.
    """
    id: str
    needs: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    verifiers: tuple[VerifierSpec, ...] = ()
    approval: Approval | None = None
    surface: SurfaceSpec | None = None
    produces: tuple[str, ...] = ()
    repair: RepairSpec | None = None
    seals_unit: bool = False


@dataclasses.dataclass(frozen=True)
class Surface:
    """Relative path to sha256. Serialized as the map itself, never wrapped."""
    files: dict[str, str]

    def to_dict(self) -> dict:
        return dict(self.files)

    @classmethod
    def from_dict(cls, data: dict) -> "Surface":
        if not isinstance(data, dict):
            raise TypeError("Surface.from_dict expects a dict")
        return cls(files=dict(data))

    def digest(self) -> str:
        return _sha256(canonical_json(self.files))


# --------------------------------------------------------------------------
# Evidence (section 1.8)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ProducerRecord(_Plain):
    id: str
    kind: ProducerKind
    digest: str
    exit: int | None
    verdict: str | None
    findings_digest: str | None


@dataclasses.dataclass(frozen=True)
class Actor(_Plain):
    kind: ActorKind
    id: str
    channel: str | None


@dataclasses.dataclass(frozen=True)
class EvidenceRecord(_Plain):
    v: int
    seq: int
    kind: RecordKind
    gate: str | None
    at: str
    surface_digest: str
    producers: tuple[ProducerRecord, ...]
    config_digest: str
    actor: Actor
    prev: str
    digest: str
    mac: str | None = None

    def compute_digest(self) -> str:
        """sha256 of canonical JSON over every field except digest and mac."""
        body = {key: value for key, value in self.to_dict().items()
                if key not in ("digest", "mac")}
        return _sha256(canonical_json(body))


# --------------------------------------------------------------------------
# Bounds, humans, faults (sections 1.6, 1.11, 1.12, 1.14, K7)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Spend(_Plain):
    usd: float = 0.0
    tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    first_started_at: str | None = None
    # Section 1.14: iteration backstop is checked in pace and dispatch; elapsed
    # time is accumulated in state so a restart does not reset it (bug B17).
    iterations: int = 0
    elapsed_s: float = 0.0


@dataclasses.dataclass(frozen=True)
class Budget(_Plain):
    max_usd: float | None = None
    max_tokens: int | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_wall_s: float | None = None
    # Section 1.14 names three backstops: cost, iteration and wall-clock.
    max_iterations: int | None = None
    # K7: all bounds null is refused unless this explicit opt-in is set.
    allow_unbounded: bool = False


@dataclasses.dataclass(frozen=True)
class HumanRequest(_Plain):
    id: str
    kind: HumanKind
    gate: str | None
    channel: str
    summary: str
    at: str
    # Typed payload per kind (section 1.12 table), JSON-plain.
    payload: dict[str, Any]
    # Section 1.12, channel token: the one-time token is printed to the
    # terminal when the request is raised; only its sha256 is kept, so the
    # token itself never lands in a checkpoint. Set only for channel "token".
    token_digest: str | None = None


@dataclasses.dataclass(frozen=True)
class HumanDecision(_Plain):
    request_id: str
    answer: HumanAnswer
    by: str
    channel: str
    note: str | None
    at: str


class FaultKind(str, enum.Enum):
    """K7: whose fault a failure is."""
    VARIANT = "variant"            # contract and smoke errors: counts as a strike
    INFRASTRUCTURE = "infrastructure"  # everything unknown, and rate limits


@dataclasses.dataclass(frozen=True)
class FaultRoute(_Plain):
    fault: FaultKind
    retryable: bool
    strike: bool                       # counts against the variant / fail_streak
    backoff_s: float | None = None     # retry delay when retryable (rate limit)
    reason: str = ""
