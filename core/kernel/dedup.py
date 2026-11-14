"""DEDUP: reject a re-run of a settled mechanism before spending an oracle run on it.

Re-proposing a falsified lever, often under a renamed cell, is the search's cardinal waste.
This is a two-layer sieve, cheap to costly:

1. **Exact match.** The proposal's normalised cell is already in ``registry.dead_end_cells()``:
   DUPLICATE, with zero model calls and zero oracle runs.
2. **Blinded judge** (optional, only when a ``judge_fn`` is wired). The gray zone where the
   mechanism resembles a prior dead end without an exact key match. DUPLICATE at high confidence
   blocks; DUPLICATE at low confidence is allowed but flagged (a false DUPLICATE silently kills
   novelty, which is worse than one wasted run); DISTINCT proceeds. With no judge or no
   neighbours, the variant proceeds clean.

Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass

_STOP = {  # de-noise the token-overlap ranking so it keys on mechanism words, not filler
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "with", "by", "is", "it",
    "that", "this", "as", "at", "be", "via", "its", "not", "no", "new", "s",
}


@dataclass
class DedupResult:
    distinct: bool               # True: proceed to smoke; False: DUPLICATE, a zero-compute REJECT
    reason: str
    confidence: float = 1.0
    flagged: bool = False        # surfaced in receipts (a low-confidence duplicate let through)
    matched_cell: "str | None" = None
    source: str = "clean"        # which layer decided: exact | judge | clean

    def to_dict(self) -> dict:
        return {
            "distinct": self.distinct, "reason": self.reason, "confidence": self.confidence,
            "flagged": self.flagged, "matched_cell": self.matched_cell, "source": self.source,
        }


def normalize_cell(cell: str) -> str:
    """Canonical string form for exact-match dedup: lower-cased, trimmed parts joined by ``/``."""
    parts = [p.strip().lower() for p in str(cell).split("/")]
    return "/".join(p for p in parts if p != "") if any(parts) else str(cell).strip().lower()


def _tokens(*strings: str) -> set:
    out: set = set()
    for s in strings:
        for raw in str(s).replace("/", " ").replace("_", " ").replace("-", " ").split():
            t = "".join(ch for ch in raw.lower() if ch.isalnum())
            if t and t not in _STOP and not t.isdigit():
                out.add(t)
    return out


def dead_end_context(registry) -> "list[dict]":
    """Build ``{cell, mechanism, why}`` for every currently dead cell, read-side over the
    registry. The proposal rows give each cell its mechanism; the latest REJECT verdict gives
    the "aha". Only cells reported by ``dead_end_cells()`` are included."""
    events = registry.events()
    cell_by_cand = {e["id"]: e["cell"] for e in events if e.get("kind") == "proposal"}
    mech_by_cell: dict = {}
    for e in events:
        if e.get("kind") == "proposal":
            mech_by_cell.setdefault(e["cell"], e.get("mechanism", ""))
    why_by_cell: dict = {}
    for e in events:
        if e.get("kind") == "verdict" and e.get("verdict") == "REJECT":
            cell = cell_by_cand.get(e.get("variant_id"))
            if cell:
                why_by_cell[cell] = e.get("why", "")  # later lines overwrite: the latest kill
    return [
        {"cell": c, "mechanism": mech_by_cell.get(c, ""), "why": why_by_cell.get(c, "")}
        for c in sorted(registry.dead_end_cells())
    ]


def nearest_dead_ends(card, registry, k: int = 8) -> "list[dict]":
    """The ``k`` dead-end cells most similar to ``card`` by mechanism-token overlap (Jaccard)."""
    want = _tokens(card.cell, card.mechanism, card.hypothesis)
    scored: "list[tuple[float, dict]]" = []
    for ctx in dead_end_context(registry):
        have = _tokens(ctx["cell"], ctx["mechanism"], ctx["why"])
        union = want | have
        score = (len(want & have) / len(union)) if union else 0.0
        scored.append((score, ctx))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [ctx for score, ctx in scored[:k]]


def dedup_check(
    card,
    registry,
    *,
    judge_fn=None,
    k: int = 8,
    dup_confidence_floor: float = 0.6,
) -> DedupResult:
    """Adjudicate ``card`` against settled cells. ``judge_fn(card, neighbours) -> {decision,
    confidence, reason}`` is the blinded worker; omit it for the registry-key layer only."""
    # 1. exact normalised match against the live dead-end set.
    norm = normalize_cell(card.cell)
    dead_norm = {normalize_cell(c): c for c in registry.dead_end_cells()}
    if norm in dead_norm:
        return DedupResult(False, f"exact dead-end cell match: {dead_norm[norm]}", 1.0,
                           matched_cell=dead_norm[norm], source="exact")

    # 2. blinded judge over the nearest dead-end neighbours (gray zone only).
    neighbours = nearest_dead_ends(card, registry, k) if judge_fn is not None else []
    if not neighbours:
        return DedupResult(True, "no near dead-end neighbours; distinct", 1.0, source="clean")
    j = judge_fn(card, neighbours)
    decision = str(j.get("decision", "DISTINCT")).upper()
    conf = float(j.get("confidence", 0.0))
    why = str(j.get("reason", ""))
    if decision == "DUPLICATE":
        if conf >= dup_confidence_floor:
            return DedupResult(False, f"judge: DUPLICATE: {why}", conf,
                               matched_cell=neighbours[0]["cell"], source="judge")
        return DedupResult(True, f"low-confidence duplicate, allowed and flagged: {why}", conf,
                           flagged=True, matched_cell=neighbours[0]["cell"], source="judge")
    return DedupResult(True, f"judge: DISTINCT: {why}", conf, source="judge")
