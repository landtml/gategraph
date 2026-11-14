"""corpus.py, the versioned regression corpus (§13): schema, loader, perturbations.

A case is one YAML mapping in `<corpus_dir>/*.yaml`. Three kinds:

- ``detection``: an artifact text + the policy ids that MUST fire (``expect.fire``)
  and must NOT fire (``expect.quiet``; ``["*"]`` = nothing may fire). Runs the
  real policy engine, this is the precision/recall regression net for the pack.
- ``machinery``: a named evidence-chain scenario (out-of-order refusal, staleness
  reopen, …) run against the real manifest code.
- ``live``: a model prompt + gold checks, for a live run; the deterministic runner
  skips it.

Every case carries ``set: iterate|heldout`` (tune only against iterate; heldout
is never tuned against) and ``adversarial: true|false`` (~20% target).
"""
from __future__ import annotations

import random
from pathlib import Path

import yaml

KINDS = {"detection", "machinery", "live"}
SETS = {"iterate", "heldout"}
SCENARIOS = {
    "out_of_order_refused",
    "staleness_reopens",
    "reset_invalidates_later",
    "escalate_is_terminal",
}


class CorpusError(Exception):
    pass


# Corpus files never store deny-shaped literals on disk (the same rule the test
# fixtures follow), placeholders are expanded here, assembled at runtime.
SYNTH_TOKENS = {"{{ACCOUNT}}": "010190" + "-" + "1234"}


def _expand(text: str) -> str:
    for k, v in SYNTH_TOKENS.items():
        text = text.replace(k, v)
    return text


def _fail(case_id: str, src: str, msg: str) -> "CorpusError":
    return CorpusError(f"{src}: case {case_id!r}: {msg}")


def _validate(c: dict, src: str) -> None:
    if not isinstance(c, dict) or not c.get("id"):
        raise CorpusError(f"{src}: every case needs an `id`")
    cid = c["id"]
    kind = c.get("kind")
    if kind not in KINDS:
        raise _fail(cid, src, f"unknown kind {kind!r} (one of {sorted(KINDS)})")
    if c.get("set") not in SETS:
        raise _fail(cid, src, f"unknown set {c.get('set')!r} (iterate|heldout)")
    if not isinstance(c.get("gate"), str) or not c["gate"]:
        raise _fail(cid, src, "needs `gate` (histogram key)")
    if kind == "detection":
        if not isinstance(c.get("artifact"), str):
            raise _fail(cid, src, "detection needs an `artifact` text")
        if not isinstance(c.get("path"), str):
            raise _fail(cid, src, "detection needs a repo-relative `path` for scoping")
        exp = c.get("expect")
        if (not isinstance(exp, dict)
                or not isinstance(exp.get("fire"), list)
                or not isinstance(exp.get("quiet"), list)):
            raise _fail(cid, src, "detection needs expect: {fire: [...], quiet: [...]}")
    elif kind == "machinery":
        if c.get("scenario") not in SCENARIOS:
            raise _fail(cid, src, f"unknown scenario {c.get('scenario')!r} "
                                  f"(one of {sorted(SCENARIOS)})")
    elif kind == "live":
        if not isinstance(c.get("prompt"), str) or not c["prompt"].strip():
            raise _fail(cid, src, "live needs a `prompt`")
        gold = c.get("gold")
        if not isinstance(gold, dict):
            raise _fail(cid, src, "live needs a `gold` mapping")
        if not isinstance(gold.get("quiet", []), list):
            raise _fail(cid, src, "gold.quiet must be a list of policy ids")
        for req in gold.get("require") or []:
            if not isinstance(req, dict) or not req.get("regex"):
                raise _fail(cid, src, "gold.require entries need a `regex`")


def load_corpus(corpus_dir: Path) -> "list[dict]":
    """Load and validate every case under `<corpus_dir>/*.yaml`, sorted by id."""
    cases: "list[dict]" = []
    seen: "set[str]" = set()
    files = sorted(Path(corpus_dir).glob("*.yaml"))
    if not files:
        raise CorpusError(f"no corpus files (*.yaml) in {corpus_dir}")
    for f in files:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            raise CorpusError(f"{f.name}: expected a YAML list of cases")
        for c in data:
            _validate(c, f.name)
            if c["id"] in seen:
                raise CorpusError(f"{f.name}: duplicate case id {c['id']!r}")
            seen.add(c["id"])
            c.setdefault("adversarial", False)
            for field in ("artifact", "prompt"):
                if isinstance(c.get(field), str):
                    c[field] = _expand(c[field])
            cases.append(c)
    return sorted(cases, key=lambda c: c["id"])


# Consonant digraph noise: line insertions that no sane policy regex matches,
# so a policy firing on them is itself a real precision finding.
_NOISE_ALPHABET = "zqxwvjkfhb"


def perturb(text: str, seed: int) -> str:
    """Seeded, verdict-preserving mutation: seed 0 is the identity; other seeds
    INSERT noise/blank lines only, existing lines are never edited, so any
    line-based scan verdict must survive."""
    if seed == 0:
        return text
    rng = random.Random(seed)
    lines = text.splitlines()
    inserts = rng.randint(1, 3)
    for _ in range(inserts):
        pos = rng.randint(0, len(lines))
        if rng.random() < 0.4:
            noise = ""
        else:
            noise = " ".join("".join(rng.choice(_NOISE_ALPHABET) for _ in range(4))
                             for _ in range(rng.randint(2, 4)))
        lines.insert(pos, noise)
    out = "\n".join(lines)
    return out + ("\n" if text.endswith("\n") else "")
