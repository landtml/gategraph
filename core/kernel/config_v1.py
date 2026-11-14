"""Schema-1 translator (plan section 1.7 mapping table; brief K2; bug B04).

``translate(raw, root)`` reads an adapter.yaml with ``fh_schema: 1`` and returns
the same ``Config`` that schema 2 produces. The mapping:

- ``project``, ``unit_dir`` and ``artifact_noun`` (default ``unit``) carry over.
- Each gate in ``workflow.gates`` becomes a ``GateSpec``. A gate whose command is
  ``gate.py approve <gate>`` is a human approval (channel ``tty``).
- A gate whose command is ``lint.py ... --set-gate <gate>`` (the legacy tool-set gate)
  is not a check named after the gate: ``lint.py --unit`` is retired and refuses. It
  becomes a gate that runs the ``policy`` check (the built-in policy scan, under the
  name the seed uses in schema 2) and every top-level adapter check, which is what the
  legacy lint ran over the unit. The ``policy`` check is added once, first in the list.
- Any other gate becomes a check named after the gate, with the command from
  ``workflow.gate_commands``; the schema-1 ``--set-gate <gate>`` flag is removed,
  because a gate passes only through the seal node (bug B01).
- ``needs`` defaults to the previous gate. ``seals_unit`` is set on the last gate,
  which is the old positional seal.
- ``hashed_gates`` must be a subset of ``tool_set_gates`` (bug B04). Schema 2
  records every gate's surface, so no separate hashed list survives.
- Surfaces reproduce the legacy staleness rules (core/manifest.py lines 129 to
  174, mirrored by core/kernel/surface.py):
  - an approval gate, and any gate not in ``hashed_gates``, gets the explicit
    empty surface ``SurfaceSpec(include=())``, so file edits never reopen it;
  - a hashed gate covers the whole unit (``include: ["**"]``) and excludes the
    declared reviewer evidence (``reviewer.evidence_file``) mid-flow, because the
    legacy code exempts it there;
  - the final (sealing) gate covers the whole unit with no exclusion, because
    the legacy code exempts only the manifest there.
- Top-level checks keep ``id``, ``cmd`` (as ``command``), ``on`` and ``severity``.
  ``severity`` defaults to fatal and must be ``fatal`` or ``warn`` (bug B12).

Policy (plan 1.9) maps to the guard's four attributes, with the same shapes as the
schema-2 ``policy:`` block: ``policies.deny`` to ``deny``, ``policies.ask`` to
``ask``, and the top-level ``protected_paths`` to ``protected_paths``. Schema 1 has
no blind-path key, so ``blind`` is empty.

Dropped with no schema-2 field: ``framework_dirs``, ``instance_token_patterns``,
``docs_lint``, ``doctor``, ``state_reap``, ``escalation``, ``loop`` and
``workflow.terminal_states``. (``reviewer`` survives only as the ``evidence_file``
exclusion above.)
Schema-1 packs do not exist, so ``packs`` is empty.

Standard library and PyYAML only.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import Config, ConfigError, read_policy
from .types import Approval, CheckSpec, GateSpec, Severity, SurfaceSpec

_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_APPROVE = re.compile(r"\bgate\.py\s+approve\b")
# The legacy tool-set gate: ``lint.py ... --set-gate <gate>`` (item 9).
_LINT_SET_GATE = re.compile(r"\blint\.py\b[^\n]*\s--set-gate\b")
# The built-in policy scan's check id (seed/adapter.yaml names it ``policy``) and its command.
POLICY_CHECK = "policy"
POLICY_COMMAND = "python3 harness/adapter/checks/policy_scan.py --unit {unit}"
_SEVERITIES = ("fatal", "warn")
_EMPTY_SURFACE = SurfaceSpec(include=())
_WHOLE_UNIT = ("**",)


def _surface(hashed: bool, final: bool, evidence: str | None) -> SurfaceSpec:
    """The legacy surface rule for one gate (see the module docstring)."""
    if not hashed:
        return _EMPTY_SURFACE
    if final:
        return SurfaceSpec(include=_WHOLE_UNIT)
    return SurfaceSpec(include=_WHOLE_UNIT, exclude=(evidence,) if evidence else ())


def translate(raw: dict, root) -> Config:
    """Return the Config of a schema-1 adapter document, or raise ConfigError."""
    problems: list[str] = []
    if raw.get("fh_schema") != 1:
        problems.append(f"fh_schema: must be 1, got {raw.get('fh_schema')!r}")
    for key in ("project", "unit_dir", "workflow"):
        if not raw.get(key):
            problems.append(f"{key}: required key is missing")
    for key in ("project", "unit_dir"):
        if raw.get(key) and not isinstance(raw[key], str):
            problems.append(f"{key}: must be a string")

    wf = raw.get("workflow")
    if not isinstance(wf, dict):
        raise ConfigError(problems + ["workflow: must be a mapping"])
    gates = wf.get("gates")
    if not isinstance(gates, list) or not gates or not all(isinstance(g, str) for g in gates):
        raise ConfigError(problems + ["workflow.gates: must be a non-empty list of gate names"])

    commands = wf.get("gate_commands") or {}
    if not isinstance(commands, dict):
        problems.append("workflow.gate_commands: must be a mapping")
        commands = {}

    seen: set[str] = set()
    unique_gates: list[str] = []
    for index, gate in enumerate(gates):
        if gate in seen:
            problems.append(f"workflow.gates[{index}]: duplicate gate id '{gate}'")
            continue
        seen.add(gate)
        if not _NAME.fullmatch(gate):
            problems.append(f"workflow.gates[{index}]: gate name '{gate}' is not a slug")
        if gate not in commands:
            problems.append(f"workflow.gate_commands: missing entry for gate '{gate}'")
        unique_gates.append(gate)

    lists: dict[str, list] = {}
    for field in ("tool_set_gates", "hashed_gates"):
        names = wf.get(field) or []
        if not isinstance(names, list):
            problems.append(f"workflow.{field}: must be a list of gate names")
            names = []
        lists[field] = names
        for name in names:
            if name not in gates:
                problems.append(f"workflow.{field}: gate '{name}' is not in workflow.gates")
    for name in lists["hashed_gates"]:
        if name not in lists["tool_set_gates"]:
            problems.append(
                f"workflow.hashed_gates: gate '{name}' is hashed but not tool-set "
                f"(bug B04); schema 2 records every gate's surface, so this is rejected"
            )

    reviewer = raw.get("reviewer") or {}
    if not isinstance(reviewer, dict):
        problems.append("reviewer: must be a mapping")
        reviewer = {}
    evidence = reviewer.get("evidence_file") or None
    if evidence is not None and not isinstance(evidence, str):
        problems.append("reviewer.evidence_file: must be a string")
        evidence = None

    checks: list[CheckSpec] = []
    ids: set[str] = set()
    for index, item in enumerate(raw.get("checks") or []):
        if not isinstance(item, dict) or not item.get("id"):
            problems.append(f"checks[{index}]: must be a mapping with an id")
            continue
        cid = item["id"]
        if not isinstance(cid, str) or not _NAME.fullmatch(cid):
            problems.append(f"checks[{index}].id: check id {cid!r} is not a slug")
        if cid in ids:
            problems.append(f"checks[{index}].id: duplicate check id '{cid}'")
        ids.add(cid)
        if not isinstance(item.get("cmd"), str) or not item["cmd"]:
            problems.append(f"checks[{index}].cmd: required string is missing")
        if True in item:
            problems.append(f"checks[{index}].on: parsed as a YAML boolean; quote it as 'on'")
        on = item.get("on")
        if not (isinstance(on, list) and on and all(isinstance(g, str) for g in on)):
            problems.append(f"checks[{index}].on: needs a non-empty glob list (without it the check never runs)")
        severity = item.get("severity", "fatal")
        if severity not in _SEVERITIES:
            problems.append(f"checks[{index}].severity: must be one of {list(_SEVERITIES)}, got {severity!r}")
        checks.append(CheckSpec(
            id=str(cid),
            command=str(item.get("cmd") or ""),
            severity=Severity(severity) if severity in _SEVERITIES else Severity.FATAL,
            on=tuple(on) if isinstance(on, list) else (),
        ))

    lint_gates = [g for g in unique_gates if _LINT_SET_GATE.search(str(commands.get(g) or ""))]
    if lint_gates:
        if POLICY_CHECK in ids:
            problems.append(f"checks: id '{POLICY_CHECK}' is reserved for the built-in policy scan")
        else:
            checks.insert(0, CheckSpec(id=POLICY_CHECK, command=POLICY_COMMAND, severity=Severity.FATAL))
            ids.add(POLICY_CHECK)
    lint_checks = tuple(c.id for c in checks if c.id != POLICY_CHECK)

    gate_specs: list[GateSpec] = []
    last = len(unique_gates) - 1
    hashed = set(lists["hashed_gates"])
    for index, gate in enumerate(unique_gates):
        command = commands.get(gate)
        if not isinstance(command, str) or not command.strip():
            continue  # reported above as a missing gate_commands entry
        needs = (unique_gates[index - 1],) if index > 0 else ()
        is_final = index == last
        if _APPROVE.search(command):
            gate_specs.append(GateSpec(
                id=gate, needs=needs, approval=Approval(required=True, channel="tty"),
                surface=_EMPTY_SURFACE, seals_unit=is_final,
            ))
            continue
        if _LINT_SET_GATE.search(command):
            gate_specs.append(GateSpec(
                id=gate, needs=needs, checks=(POLICY_CHECK, *lint_checks),
                surface=_surface(gate in hashed, is_final, evidence), seals_unit=is_final,
            ))
            continue
        if gate in ids:
            problems.append(f"checks: duplicate check id '{gate}' (a gate command has the same name)")
        ids.add(gate)
        command = re.sub(r"\s+--set-gate\s+" + re.escape(gate) + r"\b", "", command).strip()
        command = command.replace("{slug}", "{unit}")  # the runner substitutes {unit}, not {slug}
        checks.append(CheckSpec(id=gate, command=command, severity=Severity.FATAL))
        gate_specs.append(GateSpec(
            id=gate, needs=needs, checks=(gate,),
            surface=_surface(gate in hashed, is_final, evidence), seals_unit=is_final,
        ))

    legacy = raw.get("policies")
    if legacy is not None and not isinstance(legacy, dict):
        problems.append("policies: must be a mapping")
        legacy = {}
    legacy = legacy or {}
    (deny, ask, protected, blind), policy_problems = read_policy(
        legacy.get("deny"), legacy.get("ask"), raw.get("protected_paths"), None,
        names={"deny": "policies.deny", "ask": "policies.ask",
               "protected_paths": "protected_paths", "blind": "blind"})
    problems.extend(policy_problems)

    if problems:
        raise ConfigError(problems)
    return Config(
        root=Path(root),
        schema=1,
        project=raw["project"],
        unit_noun=str(raw.get("artifact_noun") or "unit"),
        unit_dir=raw["unit_dir"],
        packs=(),
        checks=tuple(checks),
        gates=tuple(gate_specs),
        deny=deny,
        ask=ask,
        protected_paths=protected,
        blind=blind,
    )
