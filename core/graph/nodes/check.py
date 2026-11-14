"""Check nodes of the unit graph: check_fanout, fan_out_checks, run_check, judge_checks.

Flow: ``check_fanout`` (a node) hashes the target gate's surface, stores the
surface map under ``<state_dir>/surfaces`` and records ``surface_digest``. It
clears the per-attempt lists. The routing function ``fan_out_checks`` on its
outgoing conditional edge returns one ``Send("run_check", payload)`` per check
the gate names, plus one for the built-in policy scan. ``run_check`` runs one
check as a subprocess (kernel checks.run_check), or runs the policy scan over
the stored surface, and returns one CheckResult. ``judge_checks`` folds the
results; any finding of severity fatal is a fatal report.

Dependencies come from the run config, ``config["configurable"]``:
  config     object with ``root`` (Path), ``checks`` (CheckSpec sequence) and
             ``gates`` (GateSpec sequence), e.g. core.kernel.config.Config
  unit_dir   Path of the unit directory
  state_dir  Path of the state directory (surface maps live under it)
  policies   optional: deny policies compiled by core.kernel.policy.compile_policies
  pdf_command optional: PDF extractor argv template ({path} placeholder); default pdftotext

Standard library and LangGraph only. Core nodes never spell the product name.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Mapping

from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from core.kernel import checks as kchecks
from core.kernel import policy as kpolicy
from core.kernel.surface import SurfaceMissing, hash_surface, load_surface, store_surface
from core.kernel.types import CheckResult, CheckStatus, Finding, Severity, canonical_json

# The id the built-in policy scan reports under. Not a configured check id.
POLICY_CHECK_ID = "policy-scan"

_FAILING = (CheckStatus.FATAL, CheckStatus.DID_NOT_RUN)


def _conf(config: RunnableConfig) -> Mapping[str, Any]:
    return config["configurable"]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _gate(conf: Mapping[str, Any], gate_id: str):
    for gate in conf["config"].gates:
        if gate.id == gate_id:
            return gate
    raise ValueError(f"unknown gate {gate_id!r}")


def check_fanout(state: Mapping[str, Any], config: RunnableConfig) -> dict:
    """Hash the target gate's surface, store it, record the digest, clear the attempt lists."""
    conf = _conf(config)
    gate_id = state.get("target_gate")
    if not gate_id:
        raise ValueError("check_fanout needs a target_gate")
    surface = hash_surface(conf["unit_dir"], gate_id, conf["config"].gates)
    digest = store_surface(conf["state_dir"], surface)
    return {"surface_digest": digest, "check_results": None, "verdicts": None}


def fan_out_checks(state: Mapping[str, Any], config: RunnableConfig) -> list[Send]:
    """Routing function: one Send per check the gate names, then one for the policy scan.

    A gate run selects every check the gate lists (kernel select_checks with no
    write paths). Each payload carries what run_check needs; the surface digest
    tells the policy scan which stored surface to read.
    """
    conf = _conf(config)
    gate = _gate(conf, state["target_gate"])
    digest = state.get("surface_digest")
    if not digest:
        raise ValueError("fan_out_checks needs surface_digest from check_fanout")
    by_id = {spec.id: spec for spec in conf["config"].checks}
    missing = [name for name in gate.checks if name not in by_id]
    if missing:
        raise ValueError(f"gate {gate.id!r} names unknown check(s) {missing}")
    selected = kchecks.select_checks(by_id[name] for name in gate.checks)
    common = {"unit": state.get("unit", ""), "gate": gate.id, "surface_digest": digest}
    sends = [Send("run_check", {**common, "kind": "check", "check_id": spec.id})
             for spec in selected]
    sends.append(Send("run_check", {**common, "kind": "policy"}))
    return sends


def run_check(payload: Mapping[str, Any], config: RunnableConfig) -> dict:
    """Run one check or the policy scan (payload in the state argument); return one CheckResult."""
    conf = _conf(config)
    kind = payload.get("kind")
    if kind == "check":
        result = _run_gate_check(payload, conf)
    elif kind == "policy":
        result = _policy_scan(payload, conf)
    else:
        raise ValueError(f"run_check payload has unknown kind {kind!r}")
    return {"check_results": [result.to_dict()]}


def _surface_files(conf: Mapping[str, Any], digest: str) -> list[str]:
    return sorted(load_surface(conf["state_dir"], digest))


def _run_gate_check(payload: Mapping[str, Any], conf: Mapping[str, Any]) -> CheckResult:
    cfg = conf["config"]
    by_id = {spec.id: spec for spec in cfg.checks}
    spec = by_id[payload["check_id"]]
    try:
        names = _surface_files(conf, payload["surface_digest"])
    except SurfaceMissing as exc:
        return _did_not_run(spec.id, f"surface map unavailable: {exc}", _sha256(spec.id))
    unit_dir = Path(conf["unit_dir"])
    files = [Path(os.path.relpath(unit_dir / name, cfg.root)).as_posix() for name in names]
    return kchecks.run_check(spec, cfg.root, files=files, unit=payload.get("unit", ""))


def _policy_scan(payload: Mapping[str, Any], conf: Mapping[str, Any]) -> CheckResult:
    started = time.monotonic()
    policies = list(conf.get("policies") or [])
    # The producer digest names the policies that ran (id, pattern, severity, scope).
    digest = _sha256(canonical_json([
        {"id": p["id"], "regex": p["regex"].pattern, "severity": p["severity"],
         "scope": p["scope"]} for p in policies]))
    try:
        names = _surface_files(conf, payload["surface_digest"])
    except SurfaceMissing as exc:
        return _did_not_run(POLICY_CHECK_ID, f"surface map unavailable: {exc}", digest)
    unit_dir = Path(conf["unit_dir"])
    pdf_command = conf.get("pdf_command")
    findings: list[Finding] = []
    for name in names:
        scan = kpolicy.scan_path(unit_dir / name, unit_dir, policies, pdf_command=pdf_command)
        findings.extend(scan.findings)
        for note in scan.notes:
            finding = _unscanned_finding(note)
            if finding is not None:
                findings.append(finding)
    if any(f.severity == Severity.FATAL for f in findings):
        status = CheckStatus.FATAL
    elif findings:
        status = CheckStatus.WARN
    else:
        status = CheckStatus.PASS
    return CheckResult(id=POLICY_CHECK_ID, exit=None, status=status,
                       findings=tuple(findings),
                       duration_s=round(time.monotonic() - started, 3), digest=digest)


# Skip notes that are not failures: binaries are hashed, not scanned (section 1.9).
_NOT_A_FAILURE_NOTES = frozenset({"skipped_binary"})


def _unscanned_finding(note: Mapping[str, Any]) -> Finding | None:
    """A skipped artifact is a fatal finding, so an unscanned PDF cannot pass silently (B11).

    Binaries are the designed exception: hashed, not scanned, no finding.
    Any other skip note is fatal too, so an unknown skip is never silent.
    """
    if note.get("note") in _NOT_A_FAILURE_NOTES:
        return None
    path = note.get("path")
    reason = note.get("reason") or note.get("note")
    return Finding(rule="policy.unscanned_artifact", severity=Severity.FATAL, path=path,
                   line=None, message=f"{path} was not scanned: {reason}",
                   remedy=("Install pdftotext (poppler-utils) so the PDF text can be scanned, "
                           "or exclude this file from the surface."
                           if note.get("note") == "skipped_pdf" else
                           "Make the artifact readable by the scanner, or exclude it from the surface."))


def _did_not_run(check_id: str, reason: str, digest: str) -> CheckResult:
    finding = Finding(rule="check.did_not_run", severity=Severity.FATAL, path=None,
                      line=None, message=f"[{check_id}] check did not run: {reason}")
    return CheckResult(id=check_id, exit=None, status=CheckStatus.DID_NOT_RUN,
                       findings=(finding,), duration_s=0.0, digest=digest)


def judge_checks(state: Mapping[str, Any], config: RunnableConfig) -> dict:
    """Fold the attempt's results into findings. Fatal findings are the repair feedback.

    A fatal or did-not-run result with no fatal finding of its own gets one
    synthetic fatal finding, so a failing check can never be judged as passing.
    Warn findings are kept in ``findings`` too; they do not make the attempt fail.
    """
    findings: list[Finding] = []
    for item in state.get("check_results") or []:
        result = CheckResult.from_dict(item)
        found = list(result.findings)
        if result.status in _FAILING and not any(f.severity == Severity.FATAL for f in found):
            found.append(Finding(rule=f"check.{result.status.value}", severity=Severity.FATAL,
                                 path=None, line=None,
                                 message=f"[{result.id}] {result.status.value} with no finding"))
        findings.extend(found)
    return {"findings": [f.to_dict() for f in findings]}


def has_fatal_finding(findings: Any) -> bool:
    """True when any finding (dict form, as in state) has severity fatal."""
    return any(isinstance(f, Mapping) and f.get("severity") == Severity.FATAL.value
               for f in (findings or []))
