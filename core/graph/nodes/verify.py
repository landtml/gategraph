"""The blinded verifier (plan 1.5, 1.11; bug B06).

Three graph-facing functions and one subgraph:

* ``verify_fanout(state, cfg)``: a routing function. Returns one ``Send("run_verifier", payload)``
  per verifier run of the target gate. The payload is the only thing a verifier receives:
  artifact text, rubric text, the surface's artifact digest, and identifiers for bookkeeping.
* ``run_verifier(state, cfg)``: a Send target. ``state`` is the payload. It invokes the verifier
  subgraph with artifact and rubric text only, and returns one ``VerifierVerdict``. A verdict
  whose artifact digest does not match the surface is discarded.
* ``judge_verdicts(state, cfg)``: majority vote and vote spread over the attempt's verdicts.
* ``build_verifier_graph(port)``: the verifier subgraph. Its state is ``VerifierState``
  (``artifact_text``, ``rubric_text``, ``verdict``). It has one model call and no tools.

Blinding is a property of the call signature: the subgraph state holds no path, unit name,
finding or message history, and ``run_verifier`` passes it nothing else.

``cfg`` is a Mapping with these optional keys (the runner supplies them):

* ``gates``: list of GateSpec (``verify_fanout`` only);
* ``load_artifact(gate_id) -> (text, sha256)``: the gate's artifact text and the artifact digest
  the surface recorded for it;
* ``load_rubric(path) -> text``;
* ``model_ports``: Mapping from VerifierSpec.model name to a ModelPort;
* ``spread_threshold``: float in [0, 1]; default 0.0 (any disagreement escalates).

Verdict vocabulary. The reviewer prompt asks for ``pass``, ``fail`` or ``tie``. ``tie`` is stored
as ``did_not_run`` (the verifier did not reach a decision), the kernel's VERIFIER_VERDICTS.

Spread. For the votes that are ``pass`` or ``fail``, ``spread = 1 - majority_count / votes``:
2 to 1 is 1/3, unanimous is 0. A tie or no votes has no majority and always escalates.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from core.agent.models import ModelPort
from core.kernel.types import Finding, Severity, VerifierVerdict, canonical_json

VOTES = ("pass", "fail")
TIE = "tie"
DID_NOT_RUN = "did_not_run"
_MODEL_TO_KERNEL = {"pass": "pass", "fail": "fail", TIE: DID_NOT_RUN}
_SEVERITY = {"fatal": Severity.FATAL, "warn": Severity.WARN, "note": Severity.WARN}

# Structured output requested from the verifier. ``reasoning`` comes first: the model writes its
# criterion-by-criterion reasoning before it names a verdict (bias mitigation kept from
# seed/dot_claude/agents/reviewer.md).
VERDICT_SCHEMA: dict = {
    "type": "object",
    "required": ["reasoning", "verdict", "findings"],
    "properties": {
        "reasoning": {"type": "string"},
        "verdict": {"enum": ["pass", "fail", TIE]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["criterion", "evidence", "severity"],
                "properties": {
                    "criterion": {"type": "string"},
                    "evidence": {"type": "string"},
                    "severity": {"enum": ["fatal", "warn", "note"]},
                },
            },
        },
    },
}

_PROMPT_HEAD = (
    "You are an independent reviewer. You see one artifact and one rubric, and nothing else.\n"
    "Protocol, each step mandatory:\n"
    "1. Read the rubric, then the artifact.\n"
    "2. Reason step by step through every rubric criterion BEFORE you give a verdict. Write "
    "that reasoning in the \"reasoning\" field.\n"
    "3. Every finding must cite concrete evidence: a line number, a quoted span, or a measurable "
    "property of the artifact. Put the citation in \"evidence\". A finding without evidence is "
    "discarded.\n"
    "4. The verdict is one of \"pass\", \"fail\", or \"tie\". \"tie\" means genuinely uncertain. "
    "Do not round a tie up to a pass.\n"
    "Answer with a JSON object with the keys reasoning, verdict, findings.\n"
)


class VerifierState(TypedDict, total=False):
    """The verifier subgraph's own state schema (plan 1.11)."""
    artifact_text: str
    rubric_text: str
    verdict: Optional[dict]   # {"verdict": str, "findings": list[dict]} once judged


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_prompt(artifact_text: str, rubric_text: str) -> str:
    """The whole verifier prompt. It contains the artifact and the rubric, and nothing else."""
    return (f"{_PROMPT_HEAD}\n=== RUBRIC ===\n{rubric_text}\n=== END RUBRIC ===\n\n"
            f"=== ARTIFACT ===\n{artifact_text}\n=== END ARTIFACT ===\n")


def parse_verdict(data: Any) -> tuple[str, list[dict]]:
    """Map the model's structured answer to (kernel verdict, cited findings).

    Anything malformed is ``did_not_run``. A finding with no evidence is dropped. A finding with
    an unknown severity is kept as a warning, so it cannot block on its own.
    """
    if not isinstance(data, dict):
        return DID_NOT_RUN, []
    verdict = _MODEL_TO_KERNEL.get(data.get("verdict"), DID_NOT_RUN)
    findings: list[dict] = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        evidence = str(item.get("evidence") or "").strip()
        if not evidence:
            continue
        criterion = str(item.get("criterion") or "").strip() or "unspecified"
        severity = _SEVERITY.get(item.get("severity"), Severity.WARN)
        findings.append(Finding(rule=criterion, severity=severity, path=None, line=None,
                                message=evidence).to_dict())
    return verdict, findings


def _parse_model_data(result: Any) -> Any:
    if result.data is not None:
        return result.data
    try:
        return json.loads(result.text)
    except (TypeError, ValueError):
        return None


def _make_judge(port: ModelPort):
    def judge(state: VerifierState) -> dict:
        result = port.complete(build_prompt(state["artifact_text"], state["rubric_text"]),
                               schema=VERDICT_SCHEMA)
        verdict, findings = parse_verdict(_parse_model_data(result))
        return {"verdict": {"verdict": verdict, "findings": findings}}
    return judge


def build_verifier_graph(port: ModelPort):
    """The verifier subgraph for one model port. No tools; one model call."""
    graph = StateGraph(VerifierState)
    graph.add_node("judge", _make_judge(port))
    graph.add_edge(START, "judge")
    graph.add_edge("judge", END)
    return graph.compile()


def verify_fanout(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> list:
    """Routing function: one Send per verifier run of the target gate. Writes nothing."""
    gate_id = state.get("target_gate")
    gate = next((g for g in cfg.get("gates") or () if g.id == gate_id), None)
    if gate is None or not gate.verifiers:
        return []
    artifact_text, artifact_sha = cfg["load_artifact"](gate_id)
    sends = []
    for spec in gate.verifiers:
        rubric_text = cfg["load_rubric"](spec.rubric)
        for run in range(1, spec.n + 1):
            sends.append(Send("run_verifier", {
                "artifact_text": artifact_text,
                "rubric_text": rubric_text,
                "artifact_sha256": artifact_sha,
                "verifier_id": spec.id,
                "run": run,
                "model": spec.model,
            }))
    return sends


def run_verifier(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
    """Send target. ``state`` is the verify_fanout payload. Returns one verdict, or none."""
    artifact_text = state["artifact_text"]
    actual = sha256_text(artifact_text)
    if state.get("artifact_sha256") != actual:
        # The verifier would judge bytes other than the surface's. Discard, and fail closed when
        # the payload carries no surface digest at all.
        return {"verdicts": []}

    port = (cfg.get("model_ports") or {}).get(state.get("model"))
    if port is None:
        verdict, findings = DID_NOT_RUN, []
    else:
        out = build_verifier_graph(port).invoke({"artifact_text": artifact_text,
                                                 "rubric_text": state["rubric_text"],
                                                 "verdict": None})
        judged = out["verdict"]
        verdict, findings = judged["verdict"], judged["findings"]

    digest = sha256_text(canonical_json({"verdict": verdict, "findings": findings}))
    record = VerifierVerdict(
        id=state["verifier_id"], run=int(state["run"]), verdict=verdict,
        findings=tuple(Finding(**f) for f in findings), digest=digest,
        artifact_sha256=actual)
    return {"verdicts": [record.to_dict()]}


@dataclass(frozen=True)
class Tally:
    majority: str     # "pass", "fail", or DID_NOT_RUN when there is no strict majority
    spread: float     # 1 - majority_count / votes; 1.0 when there are no votes
    votes: int        # pass and fail verdicts counted


def tally(verdicts: list) -> Tally:
    """Majority and spread over the pass and fail verdicts. did_not_run verdicts are not votes."""
    votes = [v.get("verdict") for v in verdicts if v.get("verdict") in VOTES]
    if not votes:
        return Tally(majority=DID_NOT_RUN, spread=1.0, votes=0)
    counts = Counter(votes)
    top, top_count = counts.most_common(1)[0]
    other_count = sum(counts.values()) - top_count
    spread = 1 - top_count / len(votes)
    majority = top if top_count > other_count else DID_NOT_RUN
    return Tally(majority=majority, spread=spread, votes=len(votes))


def judge_verdicts(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
    """Vote, record the spread, and feed fatal findings of failing verifiers to repair.

    Records one ``verifier.spread`` finding (warn) with every decision. A spread above the
    threshold, or no majority, adds one ``verifier.spread_escalate`` finding (fatal). See the
    module docstring for why that finding is the escalation signal.
    """
    verdicts = list(state.get("verdicts") or [])
    threshold = float(cfg.get("spread_threshold", 0.0))
    result = tally(verdicts)
    findings = list(state.get("findings") or [])

    if result.majority == "fail":
        for v in verdicts:
            if v.get("verdict") == "fail":
                findings.extend(f for f in v.get("findings") or []
                                if f.get("severity") == Severity.FATAL.value)

    findings.append(Finding(
        rule="verifier.spread", severity=Severity.WARN, path=None, line=None,
        message=(f"votes={result.votes} majority={result.majority} "
                 f"spread={result.spread:.3f} threshold={threshold:.3f}")).to_dict())

    if result.majority == DID_NOT_RUN or result.spread > threshold:
        reason = ("no majority" if result.majority == DID_NOT_RUN
                  else f"spread {result.spread:.3f} above threshold {threshold:.3f}")
        findings.append(Finding(
            rule="verifier.spread_escalate", severity=Severity.FATAL, path=None, line=None,
            message=f"verifiers disagree: {reason}").to_dict())
    return {"findings": findings}


__all__ = ["DID_NOT_RUN", "Tally", "VERDICT_SCHEMA", "VerifierState", "build_prompt",
           "build_verifier_graph", "judge_verdicts", "parse_verdict", "run_verifier",
           "sha256_text", "tally", "verify_fanout"]
