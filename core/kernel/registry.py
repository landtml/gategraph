"""Append-only JSONL memory of every variant's lifecycle (kernel port of the legacy registry).

Append-only is deliberate: the search is long-running and survives process restarts, so state
must be crash-safe and reconstructable from disk. Each line is one card (a proposal, a gate
result, or a verdict). Queries reduce the event log to the *current* state: how many variants
graduated (the stop condition), which mechanism cells are exhausted (so the maker never
re-proposes a falsified lever), which variants are on probation.

THE EVENT CONTRACT: registry.jsonl is an EVENT LOG, not one line per iteration. Every line
carries a ``kind`` tag (``proposal`` | ``gate_result`` | ``verdict``). One iteration usually
appends a proposal and a verdict; a variant may accrue several verdicts over time, and the
LATEST verdict line per variant wins. Never count raw lines. Consume through the reduction
API (``events()``, ``graduated_ids()``, ``probation_ids()``, ``dead_end_cells()``).

Standard library only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from core.kernel.oracle import VariantVerdict


def _repair_torn_tail(path: "str | Path") -> None:
    """Truncate a torn FINAL line before appending to an append-only JSONL file.

    A kill -9 mid-append can persist a partial last line. Appending after it would
    concatenate the new record onto the fragment and corrupt the log's interior permanently.
    Truncating is correct append-only semantics: a record whose write never completed was
    never persisted. Interior corruption is left alone; readers surface it."""
    p = Path(path)
    if not p.exists():
        return
    data = p.read_bytes()
    if not data:
        return
    body = data[:-1] if data.endswith(b"\n") else data
    nl = body.rfind(b"\n")
    last = body[nl + 1:]
    if not last.strip():
        return
    try:
        json.loads(last.decode("utf-8"))
        complete = True
    except (UnicodeDecodeError, ValueError):
        complete = False
    if complete:
        if not data.endswith(b"\n"):  # whole record, missing only its terminator
            with p.open("ab") as fh:
                fh.write(b"\n")
        return
    with p.open("r+b") as fh:
        fh.truncate(nl + 1 if nl >= 0 else 0)


class Registry:
    def __init__(self, path: "str | os.PathLike") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── write ─────────────────────────────────────────────────────────────────
    def record(self, card) -> None:
        """Append one card as a JSONL line (the card must expose ``to_dict``)."""
        _repair_torn_tail(self.path)  # a kill -9 tear must not corrupt this append
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(card.to_dict(), ensure_ascii=False) + "\n")

    # ── read ──────────────────────────────────────────────────────────────────
    def events(self) -> "list[dict]":
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        out: "list[dict]" = []
        for i, line in enumerate(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A crash mid-write can only tear the FINAL line. Tolerate that, but a malformed
                # interior line is real corruption and must surface.
                if i == len(lines) - 1:
                    break
                raise
        return out

    def _cell_by_variant(self) -> "dict[str, str]":
        return {e["id"]: e["cell"] for e in self.events() if e.get("kind") == "proposal"}

    def _latest_verdict_by_variant(self) -> "dict[str, VariantVerdict]":
        out: "dict[str, VariantVerdict]" = {}
        for e in self.events():
            if e.get("kind") == "verdict":
                out[e["variant_id"]] = VariantVerdict(e["verdict"])  # later lines win
        return out

    def _latest_voids_cell_by_variant(self) -> "dict[str, bool]":
        """Per variant, whether its latest verdict counts as falsifying the mechanism cell.
        A tripwire REJECT sets ``voids_cell=False`` (a code bug, not a dead mechanism); every
        other verdict defaults to True."""
        out: "dict[str, bool]" = {}
        for e in self.events():
            if e.get("kind") == "verdict":
                out[e["variant_id"]] = bool(e.get("voids_cell", True))
        return out

    def _ids_with_verdict(self, target: VariantVerdict) -> "list[str]":
        return [cid for cid, v in self._latest_verdict_by_variant().items() if v is target]

    def graduated_ids(self) -> "list[str]":
        return self._ids_with_verdict(VariantVerdict.GRADUATE)

    def probation_ids(self) -> "list[str]":
        return self._ids_with_verdict(VariantVerdict.PROBATION)

    def latest_probation_id(self) -> "str | None":
        """The most recently probationed variant still on probation: the near-miss a
        PROBATION re-iteration should continue."""
        on_probation = set(self.probation_ids())
        latest = None
        for e in self.events():
            if e.get("kind") == "verdict" and e.get("variant_id") in on_probation \
                    and e.get("verdict") == VariantVerdict.PROBATION.value:
                latest = e["variant_id"]
        return latest

    def dead_end_cells(self) -> "set[str]":
        """Cells with at least one voiding REJECT and no GRADUATE or PROBATION: exhausted.

        A REJECT with ``voids_cell=False`` (a tripwire, a code bug and not a falsified
        mechanism) is neutral: it neither burns the cell nor keeps it alive."""
        cell_of = self._cell_by_variant()
        verdicts = self._latest_verdict_by_variant()
        voids = self._latest_voids_cell_by_variant()
        rejected: "set[str]" = set()
        alive: "set[str]" = set()
        for cid, v in verdicts.items():
            cell = cell_of.get(cid)
            if cell is None:
                continue
            if v is VariantVerdict.REJECT:
                if voids.get(cid, True):
                    rejected.add(cell)
            else:  # GRADUATE or PROBATION keeps the cell alive
                alive.add(cell)
        return rejected - alive

    def is_dead_end(self, cell: str) -> bool:
        return cell in self.dead_end_cells()
