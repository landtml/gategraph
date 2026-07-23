"""config.py, loader + schema validator for harness/adapter/adapter.yaml.

This module is the ONLY reader of adapter.yaml: every other core module takes
a Config object, so the schema is enforced in exactly one place. Validation
collects all problems and raises one ConfigError naming them.
"""
from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required (pip install pyyaml)") from exc

ADAPTER_REL = Path("harness") / "adapter" / "adapter.yaml"
SCHEMA_VERSION = 1


class ConfigError(Exception):
    """Raised when adapter.yaml is missing, unparsable, or schema-invalid."""


class Config:
    """Validated, attribute-shaped view of adapter.yaml."""

    def __init__(self, raw: dict, root: Path):
        self.raw = raw
        self.root = Path(root)
        self.project: str = raw["project"]
        self.artifact_noun: str = raw.get("artifact_noun", "unit")
        self.unit_dir: str = raw["unit_dir"]
        self.framework_dirs: list = raw.get("framework_dirs", [])
        self.instance_token_patterns: list = raw.get("instance_token_patterns", [])

        wf = raw["workflow"]
        self.gates: list = wf["gates"]
        self.hashed_gates: list = wf.get("hashed_gates", [])
        self.tool_set_gates: list = wf.get("tool_set_gates", [])
        self.gate_commands: dict = wf["gate_commands"]

        pol = raw.get("policies") or {}
        self.deny: list = pol.get("deny") or []
        self.ask: list = pol.get("ask") or []
        self.protected_paths: list = raw.get("protected_paths") or []

        self.checks: list = raw.get("checks") or []
        self.docs_lint: dict = raw.get("docs_lint") or {}
        self.reviewer: dict = raw.get("reviewer") or {}
        doctor = raw.get("doctor") or {}
        self.doctor_required: list = doctor.get("required") or []
        self.doctor_advisory: list = doctor.get("advisory") or []
        self.state_reap: dict = raw.get("state_reap") or {}
        self.escalation: dict = raw.get("escalation") or {}
        self.loop: dict = raw.get("loop") or {}

    def gate_command(self, gate: str, slug: str) -> str:
        """The next-step command for a gate, the ONLY source of that text."""
        return str(self.gate_commands.get(gate, "")).replace("{slug}", slug)


def adapter_path(root: Path) -> Path:
    return Path(root) / ADAPTER_REL


def load(root: Path) -> Config:
    path = adapter_path(root)
    if not path.is_file():
        raise ConfigError(f"missing adapter config: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"adapter.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("adapter.yaml root is not a mapping")
    # The contract of load(): return a Config or raise ConfigError, nothing
    # else. The validator enumerates known mistakes with good messages; any
    # OTHER structural surprise must still surface as ConfigError, because
    # pretool_guard's fail-closed path keys on that type.
    try:
        problems = _validate(raw)
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        raise ConfigError(f"adapter.yaml malformed: {exc!r}") from exc
    if problems:
        raise ConfigError("adapter.yaml schema invalid:\n  - " + "\n  - ".join(problems))
    try:
        return Config(raw, Path(root))
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        raise ConfigError(f"adapter.yaml malformed: {exc!r}") from exc


def _validate(raw: dict) -> list[str]:
    p: list[str] = []
    if raw.get("fh_schema") != SCHEMA_VERSION:
        p.append(f"fh_schema must be {SCHEMA_VERSION}, got {raw.get('fh_schema')!r}")
    for key in ("project", "unit_dir", "workflow"):
        if not raw.get(key):
            p.append(f"missing required key: {key!r}")
    wf = raw.get("workflow") or {}
    gates = wf.get("gates")
    if not isinstance(gates, list) or not gates:
        p.append("workflow.gates must be a non-empty list")
        return p  # everything below depends on gates
    commands = wf.get("gate_commands") or {}
    for g in gates:
        if g not in commands:
            p.append(f"workflow.gate_commands missing entry for gate {g!r}")
    for field in ("hashed_gates", "tool_set_gates"):
        unknown = set(wf.get(field) or []) - set(gates)
        if unknown:
            p.append(f"workflow.{field} not in gates: {sorted(unknown)}")
    dl = raw.get("docs_lint") or {}
    for key in ("extra_globs", "exclude_globs"):
        val = dl.get(key)
        if val is not None and (not isinstance(val, list)
                                or not all(isinstance(s, str) for s in val)):
            p.append(f"docs_lint.{key} must be a list of glob strings")
    for i, chk in enumerate(raw.get("checks") or []):
        if not isinstance(chk, dict) or not chk.get("id"):
            p.append(f"checks[{i}] must be a mapping with an 'id'")
            continue
        cid = chk["id"]
        if not chk.get("cmd"):
            p.append(f"check {cid!r} has no cmd")
        if True in chk:
            # bare `on:` is a YAML 1.1 boolean literal, the key loads as True
            # and the check would silently never match a file
            p.append(f"check {cid!r}: key `on` was parsed as a YAML boolean, "
                     f"quote it in adapter.yaml ('on':)")
        on = chk.get("on")
        if not (isinstance(on, list) and on
                and all(isinstance(g, str) for g in on)):
            p.append(f"check {cid!r} needs a non-empty 'on' glob list "
                     f"(without it the check never runs)")
    for pol in (raw.get("policies") or {}).get("deny") or []:
        if not isinstance(pol, dict):
            p.append(f"deny policy {pol!r} must be a mapping")
            continue
        pid = pol.get("id", "?")
        if not pol.get("id"):
            # without an id the policy cannot compile and every enforcement
            # surface would fail open, refuse at load time instead
            p.append("a deny policy has no 'id' (policies are unenforceable "
                     "without one)")
        if not pol.get("regex"):
            p.append(f"deny policy {pid!r} has no regex")
            continue
        try:
            re.compile(pol["regex"])
        except re.error as exc:
            p.append(f"deny policy {pid!r} regex does not compile: {exc}")
    esc = raw.get("escalation") or {}
    streak = esc.get("fail_streak_to_escalate")
    if streak is not None and (not isinstance(streak, int) or streak < 1):
        p.append("escalation.fail_streak_to_escalate must be a positive integer")
    return p
