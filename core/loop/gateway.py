"""The semantic worker seam, turns "propose a lever" / "check this variant" into validated cards.

``workers.py`` is the *mechanical* seam (argv → subprocess → parsed envelope). This is the
*semantic* one the driver talks to: it knows the agent prompts, assembles the driver-inlined
context, calls ``run_contract`` with the right JSON-Schema, and returns a locked card /
judgement, never letting a worker touch the registry. Two implementations share one interface
so the driver is oblivious:

- :class:`ClaudeGateway`, real ``claude -p`` workers (maker writes the variant file
  hook-scoped to the variant dir; checker reasons over inlined artifacts with no tools).
- :class:`MockGateway`, scripted, no subprocess/spend, for the driver's e2e and proceed tests.

Splitting the semantic seam from the mechanical one this way is what makes the driver's
tests fast: swap in :class:`MockGateway` and the same decision logic runs with no
subprocess and no spend.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.loop.contracts import (
    CHECKER_CONTRACT,
    JUDGE_CONTRACT,
    MAKER_CONTRACT,
    proposal_from_contract,
)
from core.loop.errors import SmokeError
from core.loop.schemas import ProposalCard
from core.loop.workers import (
    maker_spec,
    reasoner_spec,
    run_contract,
    subprocess_runner,
    write_settings_file,
)

_AGENTS = Path(__file__).resolve().parent / "agents"

VARIANT_FILE = "variant.py"  # the single filename the maker authors and the oracle tariffs


def _tokens(usage: dict) -> int:
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


@dataclass
class ProposeResult:
    card: ProposalCard
    experiment_path: str
    cost_usd: float = 0.0
    tokens: int = 0


@dataclass
class CheckResult:
    judgement: dict  # the checker payload (soft_passes/gaming_clean/…); driver runs decision.decide
    cost_usd: float = 0.0
    tokens: int = 0


@dataclass
class JudgeResult:
    decision: str    # "DISTINCT" | "DUPLICATE"
    confidence: float
    reason: str
    cost_usd: float = 0.0
    tokens: int = 0


class WorkerGateway(ABC):
    @abstractmethod
    def propose(
        self,
        *,
        variant_id: str,
        variant_dir: str,
        brief: str,
        probation_target: "str | None" = None,
        probation_code: "str | None" = None,
        repair_feedback: str = "",
        proposal_space: "list[str] | None" = None,
    ) -> ProposeResult:
        """Author ``<variant_dir>/variant.py`` and return its validated ProposalCard.

        ``proposal_space``, when non-empty, is the oracle's allowed cell vocabulary.
        the maker must pick ``cell`` from it (F2: keeps exact-match dedup meaningful)."""

    @abstractmethod
    def check(
        self, *, variant_id: str, experiment_path: str, gate_result: dict
    ) -> CheckResult:
        """Blinded tariff: return the checker's judgement payload (no verdict, the driver decides)."""

    @abstractmethod
    def judge(self, *, card, neighbours: "list[dict]") -> JudgeResult:
        """Blinded dedup call: is ``card`` DISTINCT from the near dead-end ``neighbours``?"""


class ClaudeGateway(WorkerGateway):
    """Real workers. ``model`` picks the tier; auth is subscription (workers.py)."""

    def __init__(self, model: str = "opus", runner=None) -> None:
        self.model = model
        self.runner = runner or subprocess_runner
        self._maker_md = (_AGENTS / "maker.md").read_text(encoding="utf-8")
        self._checker_md = (_AGENTS / "checker.md").read_text(encoding="utf-8")
        self._judge_md = (_AGENTS / "judge.md").read_text(encoding="utf-8")

    def propose(self, *, variant_id, variant_dir, brief, probation_target=None,
                probation_code=None, repair_feedback="", proposal_space=None):
        Path(variant_dir).mkdir(parents=True, exist_ok=True)
        settings = write_settings_file(str(Path(variant_dir) / "_settings.json"))
        parts = [
            self._maker_md,
            f"\n\n## Your variant id (use verbatim)\n{variant_id}",
            f"\n\n## Your variant directory (write ONLY here)\n{variant_dir}",
            f"\n\n## Dead-ends brief\n{brief or '(none available, reason from the repo)'}",
        ]
        if proposal_space:
            cells = "\n".join(f"- {c}" for c in proposal_space)
            parts.append(
                "\n\n## Allowed mechanism cells: set `cell` to EXACTLY one of these\n"
                f"{cells}\n(An invented cell name weakens dedup; pick the closest listed cell.)"
            )
        if probation_target:
            parts.append(f"\n\n## PROBATION re-iteration: attack ONLY this failing pass\n{probation_target}")
            if probation_code:
                # Hand over the near-miss's actual code so the maker CONTINUES it (fix the one
                # failing pass) rather than starting a fresh lever.
                parts.append(
                    "\n\n## The probationed variant to CONTINUE (build on this, don't restart)\n"
                    f"```python\n{probation_code}\n```"
                )
        if repair_feedback:
            # feedback from a SMOKE bug, a DEDUP duplicate, or a reflexion critique, the text says which.
            parts.append(f"\n\n## Your previous attempt was rejected: address this and rewrite\n```\n{repair_feedback}\n```")
        spec = maker_spec(
            "".join(parts),
            write_scope=variant_dir,
            settings_path=settings,
            model=self.model,
        )
        data, res = run_contract(spec, MAKER_CONTRACT, self.runner)
        card = proposal_from_contract(data, variant_id)
        experiment_path = str(Path(variant_dir) / VARIANT_FILE)
        if not Path(experiment_path).exists():
            # A valid card but no code file is a variant fault the repair loop should catch.
            raise SmokeError(f"maker returned a card but wrote no {VARIANT_FILE}")
        return ProposeResult(card, experiment_path, res.cost_usd, _tokens(res.usage))

    def check(self, *, variant_id, experiment_path, gate_result):
        code = Path(experiment_path).read_text(encoding="utf-8")
        prompt = (
            f"{self._checker_md}\n\n"
            f"## variant_id\n{variant_id}\n\n"
            f"## {VARIANT_FILE}\n```python\n{code}\n```\n\n"
            f"## gate_result.json (the LOCKED oracle, do not recompute)\n"
            f"```json\n{json.dumps(gate_result, indent=2)}\n```\n"
        )
        spec = reasoner_spec(prompt, model=self.model)
        data, res = run_contract(spec, CHECKER_CONTRACT, self.runner)
        return CheckResult(data, res.cost_usd, _tokens(res.usage))

    def judge(self, *, card, neighbours):
        nbrs = [
            {"cell": n.get("cell", ""), "mechanism": n.get("mechanism", ""), "why": n.get("why", "")}
            for n in neighbours
        ]
        prompt = (
            f"{self._judge_md}\n\n"
            f"## new proposal\n"
            f"```json\n{json.dumps({'hypothesis': card.hypothesis, 'mechanism': card.mechanism, 'cell': card.cell}, indent=2)}\n```\n\n"
            f"## near dead-end neighbours (already settled)\n"
            f"```json\n{json.dumps(nbrs, indent=2)}\n```\n"
        )
        spec = reasoner_spec(prompt, model=self.model)
        data, res = run_contract(spec, JUDGE_CONTRACT, self.runner)
        return JudgeResult(data["decision"], float(data["confidence"]), data["reason"],
                           res.cost_usd, _tokens(res.usage))


class MockGateway(WorkerGateway):
    """Scripted gateway for tests / ``--mock-workers``. Delegates to injected callables.

    Defaults produce a clean, GRADUATE-worthy variant so a bare e2e "just works"; tests pass their
    own ``propose_fn``/``check_fn`` to drive REJECT, tripwire, smoke-repair, and infra branches."""

    def __init__(
        self,
        propose_fn: "Callable[..., ProposeResult] | None" = None,
        check_fn: "Callable[..., CheckResult] | None" = None,
        judge_fn: "Callable[..., JudgeResult] | None" = None,
    ) -> None:
        self._propose_fn = propose_fn or default_mock_propose
        self._check_fn = check_fn or default_mock_check
        self._judge_fn = judge_fn or default_mock_judge

    def propose(self, **kw) -> ProposeResult:
        return self._propose_fn(**kw)

    def check(self, **kw) -> CheckResult:
        return self._check_fn(**kw)

    def judge(self, **kw) -> JudgeResult:
        return self._judge_fn(**kw)


# --- default mock behaviours (a clean graduate) -----------------------------------------------

_GOOD_VARIANT = "VARIANT = 'toy'  # trivially importable; toy oracle defaults to all-pass\n"


def default_mock_propose(*, variant_id, variant_dir, brief, probation_target=None,
                         probation_code=None, repair_feedback="", **_):
    d = Path(variant_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / VARIANT_FILE
    path.write_text(_GOOD_VARIANT, encoding="utf-8")
    card = ProposalCard(
        id=variant_id,
        hypothesis="mock hypothesis",
        mechanism="mock grounded mechanism sentence",
        cell="toy-alpha",
        kill_criteria="mock kill criteria",
        params={"dead_end_citations": ["mock:L1"]},
    )
    return ProposeResult(card, str(path), cost_usd=0.01, tokens=100)


def default_mock_judge(*, card, neighbours, **_):
    # Bare e2e should proceed: default to DISTINCT (the registry's exact layer blocks real dead-ends).
    return JudgeResult("DISTINCT", 0.9, "mock: distinct mechanism", cost_usd=0.0, tokens=20)


def default_mock_check(*, variant_id, experiment_path, gate_result, **_):
    return CheckResult(
        judgement={
            "variant_id": variant_id,
            "soft_passes": {"mechanism_lead": True, "robustness": True},
            "gaming_clean": True,
            "mechanism_sentence": "mock mechanism holds in the claimed regime",
            "structurally_promising": True,
            "exhausted": False,
            "notes": "mock",
        },
        cost_usd=0.01,
        tokens=80,
    )
