"""Append-only receipts, evidence attached to every phase transition.

Distinct from the *registry* (``registry.py``, the append-only log of research cards). This
log is the **operational** trail: one JSONL receipt per state-machine transition, so a human
can reconstruct exactly what the driver did, when, at what cost, and under which methodology
version, without trusting any worker's prose.

Every receipt carries the **manifest stamp** (``harness_version`` + ``config_hash`` +
``manifest_fingerprint``, from ``manifest.py``) so each transition is traceable to the
methodology that produced it; the driver computes the stamp once per run.
Deliberately small: a receipt is one dataclass and one JSONL append, so the operational
trail stays cheap to write and trivial to replay.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Receipt:
    """One phase transition. ``from_phase``/``to_phase`` serialize as ``from``/``to``."""

    iteration: int
    from_phase: str
    to_phase: str
    detail: str = ""
    variant: "str | None" = None
    artifact_paths: "list[str]" = field(default_factory=list)
    cost_usd: float = 0.0
    tokens: int = 0
    verdict: "str | None" = None  # set on the DECIDE/RECORD receipt
    outcome: "str | None" = None  # infra-vs-variant routing tag (see errors.py), when relevant
    stamp: dict = field(default_factory=dict)  # manifest provenance
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["from"] = d.pop("from_phase")
        d["to"] = d.pop("to_phase")
        return d


def repair_torn_tail(path: "str | Path") -> None:
    """Truncate a torn FINAL line before appending to an append-only JSONL file.

    A kill -9 mid-append can persist a partial last line. Appending after it would
    concatenate the new record onto the fragment, corrupting the log's *interior*
    permanently. Truncating is correct append-only semantics: a record whose write
    never completed was never persisted. Interior corruption is untouched (readers
    surface it)."""
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


class EventLog:
    """Append receipts to ``events.jsonl`` and read them back. Writes are append-only (crash-safe)."""

    def __init__(self, path: "str | Path", stamp: "dict | None" = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stamp = dict(stamp or {})

    def emit(
        self,
        iteration: int,
        from_phase: str,
        to_phase: str,
        *,
        detail: str = "",
        variant: "str | None" = None,
        artifact_paths: "list[str] | None" = None,
        cost_usd: float = 0.0,
        tokens: int = 0,
        verdict: "str | None" = None,
        outcome: "str | None" = None,
    ) -> Receipt:
        """Build, persist, and return a receipt (stamped with the run's manifest provenance)."""
        r = Receipt(
            iteration=iteration,
            from_phase=str(from_phase),
            to_phase=str(to_phase),
            detail=detail,
            variant=variant,
            artifact_paths=list(artifact_paths or []),
            cost_usd=cost_usd,
            tokens=tokens,
            verdict=verdict,
            outcome=outcome,
            stamp=dict(self._stamp),
        )
        repair_torn_tail(self.path)  # a kill -9 tear must not corrupt this append
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
        return r

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
                # Append-only ⇒ a crash mid-write can only tear the FINAL line. Tolerate
                # that (the kill -9 guarantee must include the receipts log), but interior
                # corruption is real damage we surface, not swallow. Same contract as
                # registry.events().
                if i == len(lines) - 1:
                    break
                raise
        return out

    def tail(self, n: int) -> "list[dict]":
        return self.events()[-n:]

    def total_cost_usd(self) -> float:
        return sum(float(e.get("cost_usd", 0.0)) for e in self.events())


def render_status(
    engine_status: dict,
    state: "dict | None",
    recent: "list[dict]",
    *,
    banner: str = "",
) -> str:
    """Render ``STATUS.md``: the scoreboard + the live operational state (current
    iteration/phase) + the last few receipts. Human-first."""
    lines: "list[str]" = ["# loop: run status", ""]
    if banner:
        lines += [f"> {banner}", ""]

    st = engine_status
    lines += [
        f"**Graduated: {st.get('n_graduated', 0)}/{st.get('target', '?')}**  ·  "
        f"iterations: {st.get('iterations', 0)}  ·  cost: ${st.get('cost_usd', 0.0):.2f}",
        f"**Stop:** {st.get('stop')}: {st.get('stop_reason', '')}",
        "",
        f"- Graduated: {', '.join(st.get('graduated', [])) or 'none'}",
        f"- Probation (re-queue): {', '.join(st.get('probation', [])) or 'none'}",
        f"- Dead-end cells: {', '.join(st.get('dead_end_cells', [])) or 'none'}",
        "",
    ]

    if state is not None:
        paused = " (backing off)" if state.get("backoff_until") else ""
        lines += [
            "## Current iteration",
            f"- iteration {state.get('iteration')} · variant {state.get('variant_id') or 'none'} · "
            f"phase **{state.get('phase')}**{paused}",
            f"- attempts {state.get('attempts', 0)} · strikes {state.get('strikes', 0)}"
            + (f" · {state.get('detail')}" if state.get("detail") else ""),
            "",
        ]

    lines += ["## Recent transitions"]
    if recent:
        for e in recent:
            tag = f" [{e['verdict']}]" if e.get("verdict") else ""
            out = f" ({e['outcome']})" if e.get("outcome") else ""
            lines.append(
                f"- iter {e.get('iteration')} {e.get('from')}→{e.get('to')}{tag}{out}"
                + (f": {e['detail']}" if e.get("detail") else "")
            )
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"
