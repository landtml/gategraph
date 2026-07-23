"""Crash-safe per-iteration state, the spine of restartable operation.

The driver is a long-running loop that must survive ``kill -9``, a context reset, or a machine
reboot mid-iteration and pick up **exactly** where it left off. So the loop keeps *no* durable
state in memory, every phase transition is persisted here first, then acted on. On restart the
driver loads the latest iteration file and re-enters at its recorded ``phase``.

Two invariants make that safe:
- **Atomic writes.** We write a sibling temp file and ``os.replace`` it over the target, a rename is
  atomic on POSIX, so a reader (or a crash) never sees a half-written JSON.
- **Phase is a typed enum, persisted as its string value.** No free-text states, so a proceeded
  driver can only ever be at a known point in ``BRIEF → … → PACE``.

(State persistence here is intentionally plain: one dataclass, one atomic write, no ORM.
The phase list is FIXED by design, a new phase is a version bump, not a plugin API; plan §8.3.)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class Phase(str, Enum):
    """The pipeline stages, in order. ``str``-valued so it serializes as e.g. ``"propose"``."""

    BRIEF = "brief"
    PROPOSE = "propose"
    DEDUP = "dedup"
    SMOKE = "smoke"
    ORACLE = "oracle"
    CHECK = "check"
    DECIDE = "decide"
    RECORD = "record"
    PACE = "pace"
    DONE = "done"


# The canonical order the driver walks. ``next_phase`` reads from this single source of truth.
PHASE_ORDER: "tuple[Phase, ...]" = (
    Phase.BRIEF,
    Phase.PROPOSE,
    Phase.DEDUP,
    Phase.SMOKE,
    Phase.ORACLE,
    Phase.CHECK,
    Phase.DECIDE,
    Phase.RECORD,
    Phase.PACE,
    Phase.DONE,
)


def next_phase(phase: Phase) -> Phase:
    """The phase that follows ``phase`` in the pipeline (``DONE`` is terminal → returns itself)."""
    i = PHASE_ORDER.index(phase)
    return PHASE_ORDER[min(i + 1, len(PHASE_ORDER) - 1)]


@dataclass
class IterationState:
    """Everything needed to proceed one iteration from disk. One file per iteration."""

    iteration: int
    variant_id: "str | None" = None
    phase: str = Phase.BRIEF.value  # stored as the enum's string value for JSON
    attempts: int = 0  # variant-level re-iterations (drift backstop counts these)
    strikes: int = 0  # contract strikes on the current worker phase
    probation_target: "str | None" = None  # a failing pass a PROBATION re-iteration must attack
    backoff_until: "float | None" = None  # epoch secs; set on RateLimited, PACE waits it out
    backoff_count: int = 0  # consecutive rate-limit backoffs on THIS iteration → lengthens the sleep
    artifacts: dict = field(default_factory=dict)  # phase → produced path(s) (audit + proceed)
    detail: str = ""  # last transition note (mirrors the receipt)
    started_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    @property
    def phase_enum(self) -> Phase:
        return Phase(self.phase)

    def advance(self, phase: Phase) -> None:
        """Move to ``phase`` and reset the per-phase strike counter (attempts persist across phases)."""
        self.phase = phase.value
        self.strikes = 0
        self.updated_ts = time.time()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "IterationState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class StateStore:
    """Reads/writes ``<root>/iterations/iter_<n>.json`` atomically and finds the latest to proceed."""

    def __init__(self, root: "str | os.PathLike") -> None:
        self.dir = Path(root) / "iterations"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, iteration: int) -> Path:
        return self.dir / f"iter_{iteration}.json"

    def save(self, state: IterationState) -> Path:
        """Atomically persist ``state``. Write a temp sibling, ``fsync``, then ``os.replace``."""
        state.updated_ts = time.time()
        target = self.path(state.iteration)
        tmp = target.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)  # atomic on POSIX, no reader ever sees a partial file
        return target

    def load(self, iteration: int) -> IterationState:
        with self.path(iteration).open(encoding="utf-8") as fh:
            return IterationState.from_dict(json.load(fh))

    def latest_iteration(self) -> "int | None":
        """The highest iteration index on disk, or ``None`` if the loop has never run."""
        nums = []
        for p in self.dir.glob("iter_*.json"):
            try:
                nums.append(int(p.stem.split("_", 1)[1]))
            except (IndexError, ValueError):
                continue  # ignore stray/corrupt filenames
        return max(nums) if nums else None

    def load_latest(self) -> "IterationState | None":
        n = self.latest_iteration()
        return self.load(n) if n is not None else None
