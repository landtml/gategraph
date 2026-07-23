"""Best-effort 5-hour-window usage estimator, the rate-limit guard's core.

Honest scope: Claude Code does NOT expose the live rate-limit percentage. What we CAN do is
sum the token usage recorded in the local transcript JSONLs over the trailing 5 hours (the
limit is account-wide, so callers glob ALL projects) and compare to a USER-CALIBRATED ceiling.
An estimate, not a guaranteed cutoff, the guard ships DISABLED; if you can't calibrate it,
leave it off and rely on the cost/iteration backstops.
Kept intentionally minimal, no config-reading CLI of its own, because the driver's
``_usage_ok`` guard is the only consumer.
"""

from __future__ import annotations

import glob
import json
from datetime import datetime


def tokens_in_window(records: "list[dict]", now_ts: float, window_hours: float = 5.0) -> int:
    """Sum ``tokens`` over records whose ``ts`` (epoch seconds) is within ``window_hours`` of now."""
    cutoff = now_ts - window_hours * 3600.0
    return sum(int(r["tokens"]) for r in records if r["ts"] >= cutoff)


def over_threshold(used: int, ceiling: "int | None", halt_pct: float) -> bool:
    """True when ``used`` has reached ``halt_pct``% of ``ceiling``. ``None`` ceiling ⇒ never."""
    if not ceiling:
        return False
    return used >= ceiling * (halt_pct / 100.0)


def _iter_usage_records(projects_glob: str):
    """Yield ``{ts, tokens}`` for every usage record in the transcript JSONLs."""
    for path in glob.glob(projects_glob):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    msg = o.get("message") if isinstance(o.get("message"), dict) else None
                    u = (msg or o).get("usage")
                    ts = o.get("timestamp")
                    if not u or not ts:
                        continue
                    try:
                        epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    tokens = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                              + u.get("cache_creation_input_tokens", 0)
                              + u.get("cache_read_input_tokens", 0))
                    yield {"ts": epoch, "tokens": tokens}
        except OSError:
            continue
