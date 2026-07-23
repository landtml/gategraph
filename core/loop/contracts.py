"""JSON-Schema contracts for worker outputs + a stdlib validator.

The driver **re-validates** every worker's structured output against these schemas, it never
trusts ``--json-schema`` conformance alone (FLAGS.md §6). A validation failure is a strike,
retried with the concrete errors fed back to a fresh worker (see ``workers.run_contract``).

The validator is a deliberately small stdlib subset of JSON Schema (the template's only
external dependency is PyYAML, plan §3), covering exactly what these contracts use:
``type`` object/string/boolean/number/array, ``required``, ``additionalProperties: false``,
``properties``, ``minLength``, ``minItems``, ``items``, ``enum``, ``minimum``/``maximum``.
Kept intentionally narrow: only the JSON-Schema features these particular contracts use,
nothing more, so there's no external validator dependency to track.
"""
from __future__ import annotations

from core.loop.schemas import ProposalCard, Verdict, VerdictCard

_STR = {"type": "string", "minLength": 1}

MAKER_CONTRACT: dict = {
    "type": "object",
    "required": ["id", "hypothesis", "mechanism", "cell", "kill_criteria", "dead_end_citations"],
    "additionalProperties": False,
    "properties": {
        "id": _STR,
        "hypothesis": _STR,
        "mechanism": _STR,
        "cell": _STR,
        "kill_criteria": _STR,
        # process rule turned schema check: the maker must cite the dead-ends it checked against
        # (brief lines / registry cell ids). Empty list → contract violation → strike.
        "dead_end_citations": {"type": "array", "items": _STR, "minItems": 1},
        "params": {"type": "object"},
    },
}

# The checker emits *judgements only*, never the verdict. GRADUATE/PROBATION/REJECT is computed
# deterministically by ``decision.decide`` from these fields + the locked gate result, so the
# bar cannot drift and a worker cannot rubber-stamp its own gavel. ``mechanism_sentence`` may be
# an empty string (a clean "no clear mechanism" → not GRADUATE), so it is intentionally not _STR.
CHECKER_CONTRACT: dict = {
    "type": "object",
    "required": [
        "variant_id",
        "soft_passes",
        "gaming_clean",
        "mechanism_sentence",
        "structurally_promising",
        "exhausted",
    ],
    "additionalProperties": False,
    "properties": {
        "variant_id": _STR,
        "soft_passes": {
            "type": "object",
            "required": ["mechanism_lead", "robustness"],
            "additionalProperties": False,
            "properties": {"mechanism_lead": {"type": "boolean"}, "robustness": {"type": "boolean"}},
        },
        "gaming_clean": {"type": "boolean"},
        "mechanism_sentence": {"type": "string"},
        "structurally_promising": {"type": "boolean"},
        "exhausted": {"type": "boolean"},
        "notes": {"type": "string"},
    },
}

JUDGE_CONTRACT: dict = {
    "type": "object",
    "required": ["decision", "confidence", "reason"],
    "additionalProperties": False,
    "properties": {
        "decision": {"enum": ["DISTINCT", "DUPLICATE"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": _STR,
    },
}


def validate(instance, schema: dict, path: str = "") -> "list[str]":
    """Return a list of human-readable validation errors ([] if valid). Never raises."""
    errs: "list[str]" = []
    label = path or "<root>"
    t = schema.get("type")

    if t == "object":
        if not isinstance(instance, dict):
            return [f"{label}: expected an object, got {type(instance).__name__}"]
        for req in schema.get("required", []):
            if req not in instance:
                errs.append(f"{label}: {req!r} is a required property")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for k in instance:
                if k not in props:
                    errs.append(f"{label}: additional property {k!r} is not allowed")
        for k, sub in props.items():
            if k in instance:
                errs.extend(validate(instance[k], sub, f"{path}/{k}" if path else k))
    elif t == "string":
        if not isinstance(instance, str):
            errs.append(f"{label}: expected a string")
        elif len(instance) < schema.get("minLength", 0):
            errs.append(f"{label}: shorter than minLength {schema['minLength']}")
    elif t == "boolean":
        if not isinstance(instance, bool):
            errs.append(f"{label}: expected a boolean")
    elif t == "number":
        if isinstance(instance, bool) or not isinstance(instance, (int, float)):
            errs.append(f"{label}: expected a number")
        else:
            if "minimum" in schema and instance < schema["minimum"]:
                errs.append(f"{label}: {instance} is below minimum {schema['minimum']}")
            if "maximum" in schema and instance > schema["maximum"]:
                errs.append(f"{label}: {instance} is above maximum {schema['maximum']}")
    elif t == "array":
        if not isinstance(instance, list):
            errs.append(f"{label}: expected an array")
        else:
            if len(instance) < schema.get("minItems", 0):
                errs.append(f"{label}: fewer than minItems {schema['minItems']}")
            item_schema = schema.get("items")
            if item_schema:
                for i, item in enumerate(instance):
                    errs.extend(validate(item, item_schema, f"{label}[{i}]"))

    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{label}: {instance!r} is not one of {schema['enum']}")
    return errs


def proposal_from_contract(d: dict, variant_id: str) -> ProposalCard:
    """Build a locked :class:`ProposalCard` from a validated maker payload.

    ``dead_end_citations`` lives in ``params`` so the registry row keeps it without modifying
    the locked schema. ``id`` is forced to the driver-assigned ``variant_id``, the worker
    cannot rename its own cell.
    """
    params = dict(d.get("params") or {})
    params["dead_end_citations"] = list(d["dead_end_citations"])
    return ProposalCard(
        id=variant_id,
        hypothesis=d["hypothesis"],
        mechanism=d["mechanism"],
        cell=d["cell"],
        kill_criteria=d["kill_criteria"],
        params=params,
    )


def checker_kwargs(d: dict) -> dict:
    """Extract the exact keyword arguments :func:`decision.decide` consumes from a validated
    checker payload. The worker's role ends at judgement; the verdict is computed, not asserted."""
    return {
        "soft_passes": dict(d["soft_passes"]),
        "gaming_clean": bool(d["gaming_clean"]),
        "mechanism_sentence": d["mechanism_sentence"],
        "structurally_promising": bool(d["structurally_promising"]),
        "exhausted": bool(d["exhausted"]),
    }


def verdict_from_contract(
    d: dict, variant_id: str, verdict: Verdict, target_pass: "str | None", why: str
) -> VerdictCard:
    """Assemble the locked :class:`VerdictCard` from the checker's judgements + the
    **driver-computed** ``verdict``/``target_pass``/``why`` (from :func:`decision.decide`).
    The worker supplies none of the last three, that is the anti-drift seam."""
    return VerdictCard(
        variant_id=variant_id,
        soft_passes=dict(d["soft_passes"]),
        gaming_clean=bool(d["gaming_clean"]),
        mechanism_sentence=d["mechanism_sentence"],
        verdict=verdict,
        target_pass=target_pass,
        why=why,
    )
