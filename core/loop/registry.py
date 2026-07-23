"""Append-only JSONL memory of every variant's lifecycle.

Append-only is deliberate: the loop is long-running and survives context summarisation /
process restarts, so state must be crash-safe and reconstructable from disk. Each line is one
card (``ProposalCard`` / ``GateResult`` / ``VerdictCard``). Queries reduce the event log to the
*current* state: how many graduated (the stop condition), which mechanism cells are exhausted
(so the maker never re-proposes a falsified lever), which variants are on probation.

THE EVENT CONTRACT (F4, read this before consuming registry.jsonl):
registry.jsonl is an EVENT LOG, not one-line-per-iteration. Every line carries a ``kind`` tag
(``proposal`` | ``gate_result`` | ``verdict``); one iteration usually appends a proposal AND a
verdict, a repaired iteration appends more, and a variant may accrue several verdicts over
time, the LATEST verdict line per variant wins. Never count raw lines. Consume through the
reduction API (``events()`` / ``graduated_ids()`` / ``probation_ids()`` / ``dead_end_cells()``),
which encodes verdict-latest-wins; docs/ADAPTER_GUIDE.md carries the same contract for
adapter authors.

The event-log shape (append only, never mutate, reduce on read) is what makes
crash-safe, concurrency-safe history practical here without a database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.loop.events import repair_torn_tail
from core.loop.schemas import Verdict


class Registry:
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── write ─────────────────────────────────────────────────────────────────
    def record(self, card) -> None:
        """Append one card as a JSONL line (card must expose ``to_dict``)."""
        repair_torn_tail(self.path)  # a kill -9 tear must not corrupt this append
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(card.to_dict(), ensure_ascii=False) + "\n")

    # ── read ──────────────────────────────────────────────────────────────────
    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        out: list[dict] = []
        for i, line in enumerate(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # Append-only ⇒ a crash mid-write can only tear the FINAL line. Tolerate that (so the
                # loop can always re-read its own memory after a kill -9), but a malformed interior
                # line is real corruption we must surface, not silently swallow.
                if i == len(lines) - 1:
                    break
                raise
        return out

    def _cell_by_variant(self) -> dict[str, str]:
        return {e["id"]: e["cell"] for e in self.events() if e.get("kind") == "proposal"}

    def _latest_verdict_by_variant(self) -> dict[str, Verdict]:
        out: dict[str, Verdict] = {}
        for e in self.events():
            if e.get("kind") == "verdict":
                out[e["variant_id"]] = Verdict(e["verdict"])  # later lines overwrite earlier
        return out

    def _latest_voids_cell_by_variant(self) -> dict[str, bool]:
        """Per variant, whether its latest verdict counts as falsifying the mechanism cell. A
        tripwire REJECT sets ``voids_cell=False`` (a code bug, not a dead mechanism); every other
        verdict defaults True."""
        out: dict[str, bool] = {}
        for e in self.events():
            if e.get("kind") == "verdict":
                out[e["variant_id"]] = bool(e.get("voids_cell", True))
        return out

    def _ids_with_verdict(self, target: Verdict) -> list[str]:
        return [cid for cid, v in self._latest_verdict_by_variant().items() if v is target]

    def graduated_ids(self) -> list[str]:
        return self._ids_with_verdict(Verdict.GRADUATE)

    def probation_ids(self) -> list[str]:
        return self._ids_with_verdict(Verdict.PROBATION)

    def latest_probation_id(self) -> "str | None":
        """The most-recently-probationed variant still on probation, the near-miss a PROBATION
        re-iteration should continue (its code is fed to the next maker)."""
        on_probation = set(self.probation_ids())
        latest = None
        for e in self.events():
            if e.get("kind") == "verdict" and e.get("variant_id") in on_probation \
                    and e.get("verdict") == Verdict.PROBATION.value:
                latest = e["variant_id"]
        return latest

    def dead_end_cells(self) -> set[str]:
        """Cells with at least one *voiding* REJECT and no GRADUATE/PROBATION, exhausted approaches.

        A REJECT with ``voids_cell=False`` (a tripwire = a code bug, not a falsified mechanism)
        is neutral: it neither burns the cell nor keeps it alive, so the maker may revisit that
        mechanism with correct code."""
        cell_of = self._cell_by_variant()
        verdicts = self._latest_verdict_by_variant()
        voids = self._latest_voids_cell_by_variant()
        rejected: set[str] = set()
        alive: set[str] = set()
        for cid, v in verdicts.items():
            cell = cell_of.get(cid)
            if cell is None:
                continue
            if v is Verdict.REJECT:
                if voids.get(cid, True):  # a non-voiding (tripwire) REJECT is neutral, not a dead-end
                    rejected.add(cell)
            else:  # GRADUATE or PROBATION keeps the cell alive
                alive.add(cell)
        return rejected - alive

    def is_dead_end(self, cell: str) -> bool:
        return cell in self.dead_end_cells()
