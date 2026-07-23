"""The three cards that flow through the loop and persist to the registry as JSONL.

- ``ProposalCard``, the maker's pre-registered hypothesis (what will be tested + the named
  mechanism + the kill-criteria). Written BEFORE the variant is evaluated (pre-registration).
- ``GateResult``, the locked oracle's output. Generic shape (the generalization seam):
  ``passes`` {gate: bool}, ``metrics`` raw numbers, ``tripwires`` {name: bool, True = TRIPPED},
  ``notes``. No LLM touches this; domain sign conventions live in the project's oracle.
- ``VerdictCard``, the blinded checker's output: the soft passes, the gaming audit, the
  one-sentence mechanism, and the final 3-state ``Verdict``.

All three are plain dataclasses with ``to_dict`` / ``from_dict`` so a row of the registry is
exactly one card. Validation is in ``__post_init__``, a malformed card never reaches disk.
``GateResult`` is kept intentionally generic (plan §8.1) so any project's oracle can
populate it without the schema itself encoding domain-specific sign conventions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Verdict(str, Enum):
    """The 3-state outcome."""

    GRADUATE = "GRADUATE"     # all passes + a clear mechanism → sketch graduation artifacts
    PROBATION = "PROBATION"   # near-miss but structurally promising → re-queue at the failing pass
    REJECT = "REJECT"         # objectively exhausted, or an informative "aha" kill


def _require(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required and must be a non-empty string.")
    return value


@dataclass
class ProposalCard:
    """The maker's pre-registered intent, written before the variant is evaluated."""

    id: str
    hypothesis: str
    mechanism: str        # one-sentence, grounded; black-box metric bumps are forbidden
    cell: str             # mechanism class, the dedup key against the dead-end registry
    kill_criteria: str
    params: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(self.id, "id")
        _require(self.hypothesis, "hypothesis")
        _require(self.mechanism, "mechanism")
        _require(self.cell, "cell")
        _require(self.kill_criteria, "kill_criteria")

    def to_dict(self) -> dict:
        return {"kind": "proposal", **asdict(self)}

    @classmethod
    def from_dict(cls, d: dict) -> "ProposalCard":
        return cls(
            id=d["id"],
            hypothesis=d["hypothesis"],
            mechanism=d["mechanism"],
            cell=d["cell"],
            kill_criteria=d["kill_criteria"],
            params=d.get("params", {}),
        )


@dataclass
class GateResult:
    """The locked oracle's deterministic output, generic passes + tripwire flags.

    ``tripwires`` maps a tripwire name to whether it TRIPPED (True = variant void).
    An empty dict means no tripwire fired. The oracle computes every metric itself so a
    variant can never report its own score.
    """

    variant_id: str
    passes: dict                                # {pass_name: bool}
    metrics: dict                               # the raw numbers behind each pass
    tripwires: dict = field(default_factory=dict)   # {name: tripped?}
    notes: str = ""

    @property
    def n_passed(self) -> int:
        return sum(1 for v in self.passes.values() if v)

    @property
    def tripwire_ok(self) -> bool:
        """The non-negotiable safety gate: no tripwire fired."""
        return not any(bool(v) for v in self.tripwires.values())

    def to_dict(self) -> dict:
        return {"kind": "gate_result", **asdict(self)}

    @classmethod
    def from_dict(cls, d: dict) -> "GateResult":
        return cls(
            variant_id=d["variant_id"],
            passes=dict(d["passes"]),
            metrics=dict(d["metrics"]),
            tripwires=dict(d.get("tripwires") or {}),
            notes=str(d.get("notes") or ""),
        )


@dataclass
class VerdictCard:
    """The blinded checker's output, soft passes, gaming audit, mechanism, final verdict."""

    variant_id: str
    soft_passes: dict          # {"mechanism_lead": bool, "robustness": bool}
    gaming_clean: bool         # no metric-gaming / cherry-picking / eval peeking found
    mechanism_sentence: str    # the clear one-sentence interpretation (required to GRADUATE)
    verdict: Verdict
    target_pass: str | None = None   # for PROBATION: which pass to attack on re-iteration
    why: str = ""                    # for REJECT: the reason / the "aha" learning
    voids_cell: bool = True          # does a REJECT falsify the mechanism cell (→ dead-end)? A
    #                                  tripwire REJECT (a CODE bug, not a falsified idea) sets
    #                                  this False so the cell is not burned.

    def __post_init__(self) -> None:
        _require(self.variant_id, "variant_id")
        if not isinstance(self.verdict, Verdict):
            self.verdict = Verdict(self.verdict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return {"kind": "verdict", **d}

    @classmethod
    def from_dict(cls, d: dict) -> "VerdictCard":
        return cls(
            variant_id=d["variant_id"],
            soft_passes=dict(d["soft_passes"]),
            gaming_clean=bool(d["gaming_clean"]),
            mechanism_sentence=d["mechanism_sentence"],
            verdict=Verdict(d["verdict"]),
            target_pass=d.get("target_pass"),
            why=d.get("why", ""),
            voids_cell=bool(d.get("voids_cell", True)),
        )
