"""Evidence chain (plan section 1.8): record validation, linking and the optional MAC.

``parse_record`` validates one record field by field and rejects anything that
is not a well-formed record (bug B33: a bare string was accepted as a passed
gate). ``verify_chain`` walks a chain and reports the first broken link by
``seq``. ``make_record`` appends to a verified chain and takes the surface
DIGEST, never the surface map: the map lives in the surface store (surface.py).

Standard library only.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any

from core.kernel.types import EvidenceRecord, RecordKind

RECORD_VERSION = 1
# evidence.jsonl lines carry the surface map under this key, outside the digest.
SURFACE_KEY = "surface"
_RECORD_FIELDS = frozenset(f.name for f in dataclasses.fields(EvidenceRecord))
_REQUIRED_FIELDS = _RECORD_FIELDS - {"mac"}
_PASSING_KINDS = (RecordKind.GATE, RecordKind.APPROVAL, RecordKind.IMPORT)
# Only migration writes an import record (plan 4.9, step 6). The actor id is the guard.
IMPORT_ACTOR_ID = "migrate"
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ChainError(Exception):
    """A record is malformed, or the chain is broken at ``seq``."""

    def __init__(self, message: str, seq: int | None = None):
        super().__init__(message if seq is None else f"record {seq}: {message}")
        self.seq = seq
        self.reason = message


def is_digest(value: Any) -> bool:
    """True for a lowercase sha256 hex string."""
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _key_bytes(key: Any) -> bytes:
    if isinstance(key, str):
        return key.encode("utf-8")
    if isinstance(key, (bytes, bytearray)):
        return bytes(key)
    raise TypeError("mac_key must be bytes or str")


def _mac(key: bytes, digest: str) -> str:
    return hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest()


def _actor_id(actor: Any) -> Any:
    if isinstance(actor, Mapping):
        return actor.get("id")
    return getattr(actor, "id", None)


def _import_refused(kind: Any, actor: Any) -> bool:
    """True when an import record is written by anyone but migration (R1 guard)."""
    return kind == RecordKind.IMPORT.value and _actor_id(actor) != IMPORT_ACTOR_ID


def parse_record(raw: Any, seq: int | None = None) -> EvidenceRecord:
    """Validate one record (a mapping or an EvidenceRecord) and return it.

    The ``surface`` key of an evidence.jsonl line is accepted and ignored here:
    it sits outside the digest and is checked by the caller against
    ``surface_digest``.
    """
    if isinstance(raw, EvidenceRecord):
        raw = raw.to_dict()
    if not isinstance(raw, Mapping):
        raise ChainError("record is not a mapping", seq)
    data = {key: value for key, value in raw.items() if key != SURFACE_KEY}
    unknown = sorted(set(data) - _RECORD_FIELDS)
    if unknown:
        raise ChainError(f"unknown field(s): {', '.join(unknown)}", seq)
    missing = sorted(_REQUIRED_FIELDS - set(data))
    if missing:
        raise ChainError(f"missing field(s): {', '.join(missing)}", seq)
    if not isinstance(data["producers"], list):
        raise ChainError("producers must be a list", seq)
    try:
        rec = EvidenceRecord.from_dict(dict(data))
    except (TypeError, ValueError, KeyError) as exc:
        raise ChainError(f"malformed record: {exc}", seq) from exc

    if rec.v != RECORD_VERSION:
        raise ChainError(f"unsupported record version {rec.v!r}", seq)
    if type(rec.seq) is not int or rec.seq < 0:
        raise ChainError("seq must be a non-negative integer", seq)
    if rec.gate is not None and not isinstance(rec.gate, str):
        raise ChainError("gate must be a string or null", seq)
    if rec.kind in _PASSING_KINDS and not rec.gate:
        raise ChainError(f"{rec.kind.value} record names no gate", seq)
    if _import_refused(rec.kind, rec.actor):
        raise ChainError("import records are written only by migration", seq)
    try:
        when = datetime.datetime.fromisoformat(rec.at)
    except (TypeError, ValueError) as exc:
        raise ChainError("at is not an ISO 8601 timestamp", seq) from exc
    if when.tzinfo is None:
        raise ChainError("at needs a UTC offset", seq)
    for field in ("surface_digest", "config_digest", "digest"):
        if not is_digest(getattr(rec, field)):
            raise ChainError(f"{field} is not a sha256 digest", seq)
    if rec.prev != "" and not is_digest(rec.prev):
        raise ChainError("prev is neither empty nor a sha256 digest", seq)
    if rec.mac is not None and not is_digest(rec.mac):
        raise ChainError("mac is not a sha256 hex string", seq)
    for producer in rec.producers:
        if not is_digest(producer.digest):
            raise ChainError(f"producer {producer.id!r} digest is not a sha256 digest", seq)
        if producer.findings_digest is not None and not is_digest(producer.findings_digest):
            raise ChainError(f"producer {producer.id!r} findings digest is malformed", seq)
    return rec


def verify_chain(chain, mac_key: Any = None) -> str:
    """Check every record's seq, prev link, digest and (with a key) mac.

    Returns the digest of the head record, or "" for an empty chain. Raises
    ChainError whose ``seq`` is the first record that fails. A record's mac is
    checked only when ``mac_key`` is given; without a key it is not checked.
    """
    key = None if mac_key is None else _key_bytes(mac_key)
    prev_digest = ""
    for seq, raw in enumerate(chain):
        rec = parse_record(raw, seq)
        if rec.seq != seq:
            raise ChainError(f"seq is {rec.seq}, expected {seq}", seq)
        if rec.prev != prev_digest:
            raise ChainError("prev does not link to the previous record", seq)
        if rec.digest != rec.compute_digest():
            raise ChainError("digest does not match the record", seq)
        if key is not None:
            if rec.mac is None:
                raise ChainError("mac missing", seq)
            if not hmac.compare_digest(_mac(key, rec.digest), rec.mac):
                raise ChainError("mac does not verify", seq)
        prev_digest = rec.digest
    return prev_digest


def make_record(chain, *, kind, gate: str | None, at: str, surface_digest: str,
                producers, config_digest: str, actor, mac_key: Any = None) -> EvidenceRecord:
    """Append a record to a verified chain and return it (the caller stores it).

    ``surface_digest`` must be the digest string returned by ``store_surface``.
    Passing the surface map is refused, so the map can never reach a record.
    """
    if not isinstance(surface_digest, str):
        raise TypeError("surface_digest is the digest string of the surface, not the map")
    if not is_digest(surface_digest):
        raise ValueError("surface_digest is not a sha256 digest")
    key = None if mac_key is None else _key_bytes(mac_key)
    if _import_refused(kind, actor):
        raise ValueError("import records are written only by migration (actor "
                         f"{IMPORT_ACTOR_ID!r})")
    chain = list(chain)
    head = verify_chain(chain, mac_key=key)
    rec = EvidenceRecord(
        v=RECORD_VERSION, seq=len(chain), kind=RecordKind(kind), gate=gate, at=at,
        surface_digest=surface_digest, producers=tuple(producers),
        config_digest=config_digest, actor=actor, prev=head, digest="")
    rec = dataclasses.replace(rec, digest=rec.compute_digest())
    if key is not None:
        rec = dataclasses.replace(rec, mac=_mac(key, rec.digest))
    return parse_record(rec, len(chain))


__all__ = ["ChainError", "is_digest", "make_record", "parse_record", "verify_chain",
           "SURFACE_KEY"]
