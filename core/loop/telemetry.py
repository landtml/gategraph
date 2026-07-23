"""Append-only per-call cost telemetry (OPTIONAL utility, no production caller today).

Every model call logs one line; reducing the log gives the per-model cost split. NOTE: the
cost backstop does NOT read this, it reads the receipt log (``events.py``,
``EventLog.total_cost_usd()``). An adapter may wire Telemetry for per-model attribution;
the engine runs fine without it.
Kept separate from the receipt log so cost analysis never needs to parse operational
events, and a caller who doesn't need per-model attribution can drop this file entirely.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class Telemetry:
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        model: str,
        phase: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
    ) -> None:
        row = {
            "ts": time.time(),
            "model": model,
            "phase": phase,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def total_cost_usd(self) -> float:
        return sum(e.get("cost_usd", 0.0) for e in self.events())

    def by_model(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.events():
            out[e["model"]] = out.get(e["model"], 0.0) + e.get("cost_usd", 0.0)
        return out
