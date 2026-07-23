"""BRIEF, the dead-ends digest handed to the maker at the top of every iteration.

Its task: tell the maker what NOT to re-propose (the exhausted cells), so novelty is spent on
live ground. The deterministic digest is the contract and the floor, always available, never
fails, never blocks an iteration. An optional ``llm_fn`` may prose-refine the same material
(e.g. through a cross-model sleeve); any failure silently falls back to the digest.
Project-specific richness (a taxonomy axis, a research log) stays out of core by design
(plan §8.3): BRIEF degrades to this deterministic digest, and an adapter is free to layer
more material on top via ``llm_fn``.
"""
from __future__ import annotations


def dead_ends_digest(registry, *, max_words: int = 200) -> str:
    """The deterministic brief: exhausted cells, word-capped. Never raises."""
    dead_cells = sorted(registry.dead_end_cells())
    lines = ["DEAD-ENDS BRIEF: do not re-propose these; spend novelty on distinct mechanisms."]
    lines.append("Exhausted cells (exact, from the registry): "
                 + (", ".join(dead_cells) if dead_cells else "none recorded yet") + ".")
    text = " ".join(lines)
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]) + " …"
    return text


def build_brief(registry, *, llm_fn=None, max_words: int = 200) -> str:
    """Assemble the brief. Deterministic by default; an ``llm_fn(material) -> str`` may refine
    it, but any failure falls back. Never raises."""
    base = dead_ends_digest(registry, max_words=max_words)
    if llm_fn is None:
        return base
    try:
        out = llm_fn(base)
        return out.strip() if out and out.strip() else base
    except Exception:  # noqa: BLE001, a flaky brief must never block an iteration
        return base


def make_brief_fn(registry, llm_fn=None):
    """A zero-arg ``brief_fn`` for the driver (``brief_fn()`` → the current brief string)."""
    return lambda: build_brief(registry, llm_fn=llm_fn)
