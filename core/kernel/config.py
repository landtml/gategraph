"""Configuration loader (plan sections 1.6, 1.7, 1.8, 1.13; brief K2).

``load(root)`` reads ``harness/adapter/adapter.yaml`` under ``root`` and returns
a ``Config``, or raises ``ConfigError`` naming every problem. Nothing else
escapes ``load``.

Schema 2 is checked by a small interpreter for the language described in
``core/kernel/schema/adapter.v2.schema.yaml``: type, required, properties,
items, enum, pattern (a full match), additional: false, and the named cross-field
``rules``. Pack files (``packs/<name>/pack.yaml``) are checked the same way
against ``pack.schema.yaml``. Schema 1 is read by ``config_v1.translate``.

Standard library and PyYAML only.
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from .types import Approval, CheckSpec, GateSpec, RepairSpec, Severity
from .types import SurfaceSpec, VerifierSpec

ADAPTER_REL = Path("harness") / "adapter" / "adapter.yaml"
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
ADAPTER_SCHEMA = "adapter.v2.schema.yaml"
PACK_SCHEMA = "pack.schema.yaml"


class ConfigError(Exception):
    """The configuration is missing, unreadable or invalid.

    ``problems`` lists one ``"path: message"`` entry per problem; each path names
    the offending key.
    """

    def __init__(self, problems):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__("invalid adapter configuration:\n  - " + "\n  - ".join(self.problems))


@dataclasses.dataclass(frozen=True)
class Config:
    """The parsed configuration of one project. Schema 1 and 2 produce the same shape.

    The last four fields are the policy inputs of ``guard.decide`` (plan 1.9). An
    adapter with no policy has empty ones: it is loaded, and it has no policies.
    """
    root: Path
    schema: int
    project: str
    unit_noun: str
    unit_dir: str
    packs: tuple[str, ...]
    checks: tuple[CheckSpec, ...]
    gates: tuple[GateSpec, ...]
    deny: tuple[dict, ...] = ()
    ask: tuple[dict, ...] = ()
    protected_paths: tuple[str, ...] = ()
    blind: tuple[str, ...] = ()

    def gate(self, gate_id: str) -> GateSpec:
        for gate in self.gates:
            if gate.id == gate_id:
                return gate
        raise KeyError(gate_id)

    def check(self, check_id: str) -> CheckSpec:
        for check in self.checks:
            if check.id == check_id:
                return check
        raise KeyError(check_id)


# --------------------------------------------------------------------------
# Schema interpreter
# --------------------------------------------------------------------------

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


class _Yaml12Loader(yaml.SafeLoader):
    """SafeLoader that reads only true/false as booleans (YAML 1.2 core).

    PyYAML follows YAML 1.1, where a bare ``on:`` key is the boolean True. The
    schema-2 keys ``on`` (check trigger) must stay strings, so schema-2 documents
    and the schema files are read with this loader.
    """


_BOOL_12 = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, _BOOL_12 if tag == "tag:yaml.org,2002:bool" else regex)
            for tag, regex in resolvers]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def parse_document(text: str) -> Any:
    """Parse a schema-2 document (adapter, pack, or schema file) as YAML 1.2."""
    return yaml.load(text, Loader=_Yaml12Loader)  # noqa: S506 (SafeLoader subclass)


@functools.lru_cache(maxsize=None)
def _schema(name: str) -> dict:
    return parse_document((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _join(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def _walk(value: Any, schema: dict, path: str, problems: list[str]) -> None:
    kind = schema.get("type")
    if kind is not None:
        ok = isinstance(value, _TYPES[kind])
        if kind in ("integer", "number") and isinstance(value, bool):
            ok = False
        if not ok:
            problems.append(f"{path or '<root>'}: expected {kind}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: must be one of {schema['enum']}, got {value!r}")
    if "pattern" in schema and isinstance(value, str):
        if not re.fullmatch(schema["pattern"], value):
            problems.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{_join(path, key)}: required key is missing")
        if schema.get("additional") is False:
            for key in value:
                if key not in props:
                    problems.append(f"{_join(path, key)}: unknown key")
        for key, sub in props.items():
            if key in value:
                _walk(value[key], sub, _join(path, key), problems)
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _walk(item, schema["items"], f"{path}[{index}]", problems)


def _escapes(glob: str) -> bool:
    """A glob escapes the unit when it is absolute or has a '..' segment."""
    return glob.startswith("/") or ".." in glob.split("/")


def _rules(data: dict, rules: list[dict]) -> list[str]:
    """The named cross-field rules of adapter.v2.schema.yaml, by name."""
    templates = {rule["name"]: rule["message"] for rule in rules}
    problems: list[str] = []

    def emit(name: str, **params: Any) -> None:
        if name not in templates:
            raise ValueError(f"schema rule {name!r} is not in the schema file")
        problems.append(templates[name].format(**params))

    checks = data.get("checks", [])
    if isinstance(checks, list):
        seen: set[str] = set()
        for index, check in enumerate(checks):
            cid = check.get("id") if isinstance(check, dict) else None
            if isinstance(cid, str):
                if cid in seen:
                    emit("duplicate-check-id", index=index, id=cid)
                seen.add(cid)

    gates = data.get("gates")
    if not isinstance(gates, list):
        return problems
    if not gates:
        emit("gates-required")
        return problems
    gates = [gate if isinstance(gate, dict) else {} for gate in gates]
    ids = [gate["id"] if isinstance(gate.get("id"), str) else None for gate in gates]
    known = {gid for gid in ids if gid is not None}

    seen_gates: set[str] = set()
    for index, gid in enumerate(ids):
        if gid is None:
            continue
        if gid in seen_gates:
            emit("duplicate-gate-id", index=index, id=gid)
        seen_gates.add(gid)

    # Effective needs: the explicit list, or the previous gate in the YAML.
    graph: dict[str, list[str]] = {}
    for index, gate in enumerate(gates):
        gid = ids[index]
        if gid is None:
            continue
        if "needs" in gate:
            needs = gate["needs"] if isinstance(gate["needs"], list) else []
        else:
            needs = [ids[index - 1]] if index > 0 and ids[index - 1] else []
        needs = [need for need in needs if isinstance(need, str)]
        for need in needs:
            if need not in known:
                emit("needs-unknown-gate", index=index, need=need)
        graph[gid] = [need for need in needs if need in known]

    def reaches_itself(start: str) -> bool:
        stack = list(graph.get(start, []))
        visited: set[str] = set()
        while stack:
            node = stack.pop()
            if node == start:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(graph.get(node, []))
        return False

    for index, gid in enumerate(ids):
        if gid is not None and reaches_itself(gid):
            emit("needs-cycle", index=index, id=gid)

    for index, gate in enumerate(gates):
        has_work = bool(gate.get("checks")) or bool(gate.get("verifiers")) or "approval" in gate
        if not has_work:
            emit("gate-without-work", index=index, id=ids[index] or "?")

    for index, gate in enumerate(gates):
        surface = gate.get("surface") if isinstance(gate.get("surface"), dict) else {}
        fields = (
            ("surface.include", surface.get("include")),
            ("surface.exclude", surface.get("exclude")),
            ("produces", gate.get("produces")),
        )
        for key, globs in fields:
            if not isinstance(globs, list):
                continue
            for glob in globs:
                if isinstance(glob, str) and _escapes(glob):
                    emit("surface-escapes-unit", index=index, key=key, glob=glob)
    return problems


def validate_schema(data: Any, schema: dict) -> list[str]:
    """Problems of ``data`` against a schema document; an empty list means valid."""
    problems: list[str] = []
    _walk(data, schema, "", problems)
    if isinstance(data, dict) and schema.get("rules"):
        problems.extend(_rules(data, schema["rules"]))
    return problems


_SEVERITY_VALUES = ("fatal", "warn")
_POLICY_NAMES_V2 = {"deny": "policy.deny", "ask": "policy.ask",
                    "protected_paths": "policy.protected_paths", "blind": "policy.blind"}


def read_policy(deny, ask, protected_paths, blind, names=_POLICY_NAMES_V2):
    """The guard's four policy inputs as tuples, and the problems found in them.

    Shared by schema 2 (``policy:``) and schema 1 (``policies:``, config_v1). ``names``
    gives the path each input is reported under. The schema language cannot test that
    a deny regex compiles or that a severity is known, so this checks both (B12: an
    unknown severity is a configuration error, not a silent demotion). ``None`` means
    the key is absent and reads as empty.
    """
    problems: list[str] = []

    def strings(value, path):
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"{path}: must be a list of strings")
            return ()
        return tuple(value)

    deny_out: list[dict] = []
    if deny is not None and not isinstance(deny, list):
        problems.append(f"{names['deny']}: must be a list")
    deny_items = deny if isinstance(deny, list) else []
    for index, pol in enumerate(deny_items):
        here = f"{names['deny']}[{index}]"
        if not (isinstance(pol, dict) and isinstance(pol.get("id"), str)
                and isinstance(pol.get("regex"), str)):
            problems.append(f"{here}: must be a mapping with a string id and a string regex")
            continue
        severity = pol.get("severity", "fatal")
        if severity not in _SEVERITY_VALUES:
            problems.append(f"{here}.severity: must be one of {list(_SEVERITY_VALUES)}, got {severity!r}")
        try:
            re.compile(pol["regex"])
        except re.error as exc:
            problems.append(f"{here}.regex: does not compile: {exc}")
        deny_out.append(dict(pol))

    ask_out: list[dict] = []
    if ask is not None and not isinstance(ask, list):
        problems.append(f"{names['ask']}: must be a list")
    ask_items = ask if isinstance(ask, list) else []
    for index, entry in enumerate(ask_items):
        here = f"{names['ask']}[{index}]"
        if not (isinstance(entry, dict) and isinstance(entry.get("paths"), list)):
            problems.append(f"{here}: must be a mapping with a list of globs in paths")
            continue
        strings(entry["paths"], f"{here}.paths")
        ask_out.append(dict(entry))

    fields = (tuple(deny_out), tuple(ask_out),
              strings(protected_paths, names["protected_paths"]),
              strings(blind, names["blind"]))
    return fields, problems


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load(root) -> Config:
    """Return the Config of the project at ``root``, or raise ConfigError."""
    root = Path(root)
    try:
        return _load(root)
    except ConfigError:
        raise
    except Exception as exc:  # the contract: nothing but ConfigError escapes
        raise ConfigError([f"{ADAPTER_REL}: malformed configuration: {exc!r}"]) from exc


def _load(root: Path) -> Config:
    path = root / ADAPTER_REL
    if not path.is_file():
        raise ConfigError([f"{ADAPTER_REL}: missing adapter configuration"])
    text = path.read_text(encoding="utf-8")
    try:
        # Routing reads the version key with YAML 1.1, as schema 1 was always read.
        probe = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"{ADAPTER_REL}: not valid YAML: {exc}"]) from exc
    if not isinstance(probe, dict):
        raise ConfigError([f"{ADAPTER_REL}: root must be a mapping"])
    if "schema" in probe:
        try:
            raw = parse_document(text)
        except yaml.YAMLError as exc:
            raise ConfigError([f"{ADAPTER_REL}: not valid YAML: {exc}"]) from exc
        return _load_v2(raw, root)
    if "fh_schema" in probe:
        from . import config_v1  # imported here: config_v1 imports this module
        return config_v1.translate(probe, root)
    raise ConfigError(["schema: required key is missing (schema 2), or fh_schema (schema 1)"])


def _load_v2(raw: dict, root: Path) -> Config:
    problems = validate_schema(raw, _schema(ADAPTER_SCHEMA))
    if problems:
        raise ConfigError(problems)

    pack_names: list[str] = []
    pack_checks: list[CheckSpec] = []
    for index, name in enumerate(raw.get("packs", [])):
        if name in pack_names:
            problems.append(f"packs[{index}]: duplicate pack '{name}'")
            continue
        pack_names.append(name)
        loaded, pack_problems = _load_pack(root, name, index)
        problems.extend(pack_problems)
        pack_checks.extend(loaded)

    checks = [CheckSpec.from_dict(dict(item)) for item in raw.get("checks", [])]
    check_ids = {check.id for check in checks}
    for check in pack_checks:
        if check.id in check_ids:
            problems.append(f"checks: duplicate check id '{check.id}' (also defined by a pack)")
        check_ids.add(check.id)
    checks.extend(pack_checks)

    gates = _build_gates(raw["gates"], check_ids, problems)
    # Packs carry no policy (plan 1.13; pack.schema.yaml has no policy key), so the
    # policy is the adapter's alone.
    policy = raw.get("policy") or {}
    (deny, ask, protected, blind), policy_problems = read_policy(
        policy.get("deny"), policy.get("ask"), policy.get("protected_paths"), policy.get("blind"))
    problems.extend(policy_problems)
    if problems:
        raise ConfigError(problems)
    return Config(
        root=root,
        schema=2,
        project=raw["project"],
        unit_noun=raw["unit_noun"],
        unit_dir=raw["unit_dir"],
        packs=tuple(pack_names),
        checks=tuple(checks),
        gates=tuple(gates),
        deny=deny,
        ask=ask,
        protected_paths=protected,
        blind=blind,
    )


def _build_gates(items: list, check_ids: set[str], problems: list[str]) -> list[GateSpec]:
    gates: list[GateSpec] = []
    previous = None
    for index, item in enumerate(items):
        gid = item["id"]
        needs = tuple(item["needs"]) if "needs" in item else ((previous,) if previous else ())
        checks = tuple(item.get("checks", []))
        for name in checks:
            if name not in check_ids:
                problems.append(f"gates[{index}].checks: unknown check '{name}' in gate '{gid}'")
        verifiers = []
        for vindex, verifier in enumerate(item.get("verifiers", [])):
            if verifier["n"] < 1:
                problems.append(f"gates[{index}].verifiers[{vindex}].n: must be at least 1, got {verifier['n']}")
            # The schema's optional verifier id is ignored: the id is derived (brief K2).
            verifiers.append(VerifierSpec(id=f"{gid}.v{vindex + 1}", rubric=verifier["rubric"],
                                          n=verifier["n"], model=verifier["model"]))
        approval = Approval(**item["approval"]) if "approval" in item else None
        surface = None
        if "surface" in item:
            surface = SurfaceSpec(include=tuple(item["surface"]["include"]),
                                  exclude=tuple(item["surface"].get("exclude", [])))
        repair = RepairSpec(**item["repair"]) if "repair" in item else None
        gates.append(GateSpec(
            id=gid,
            needs=needs,
            checks=checks,
            verifiers=tuple(verifiers),
            approval=approval,
            surface=surface,
            produces=tuple(item.get("produces", [])),
            repair=repair,
            seals_unit=bool(item.get("seals_unit", False)),
        ))
        previous = gid
    return gates


def _load_pack(root: Path, name: str, index: int) -> tuple[list[CheckSpec], list[str]]:
    """The checks of one pack, and the problems found in its pack.yaml."""
    pack_file = root / "packs" / name / "pack.yaml"
    if not pack_file.is_file():
        return [], [f"packs[{index}]: pack '{name}' not found at packs/{name}/pack.yaml"]
    where = f"packs/{name}/pack.yaml"
    try:
        doc = parse_document(pack_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [], [f"{where}: not valid YAML: {exc}"]
    problems = [f"{where} {p}" for p in validate_schema(doc, _schema(PACK_SCHEMA))]
    if problems or not isinstance(doc, dict):
        return [], problems
    if doc.get("pack") != name:
        return [], [f"{where} pack: name '{doc.get('pack')}' does not match directory '{name}'"]
    checks: list[CheckSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(doc.get("checks", [])):
        if item["id"] in seen:
            problems.append(f"{where} checks[{index}].id: duplicate check id '{item['id']}'")
        seen.add(item["id"])
        # A pack check names its command ``cmd`` (section 1.13); CheckSpec calls it ``command``.
        # {pack} is the pack's directory, relative to the project root.
        checks.append(CheckSpec(
            id=item["id"],
            command=item["cmd"].replace("{pack}", f"packs/{name}"),
            severity=Severity.FATAL,
            on=tuple(item.get("on", [])),
        ))
    return checks, problems


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

_SCRIPT_SUFFIXES = (".py", ".sh")


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return "absent"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _script_tokens(command: str) -> list[str]:
    """Tokens of a check command that name a script file (by suffix)."""
    tokens = []
    for raw in command.split():
        token = raw.strip("\"'")
        if token.endswith(_SCRIPT_SUFFIXES):
            tokens.append(token)
    return tokens


def config_digest(cfg: Config) -> str:
    """sha256 of the configuration: the adapter bytes, every pack file, and every
    script that a check command names (plan section 1.8). The adapter bytes carry
    the ``policy:`` block, so a policy change changes the digest.

    A script that does not exist is hashed as ``absent``, so creating it changes
    the digest. ``__pycache__`` and bytecode are not part of a pack.
    """
    entries: dict[str, str] = {"adapter": _file_sha256(cfg.root / ADAPTER_REL)}
    for name in cfg.packs:
        pack_dir = cfg.root / "packs" / name
        if not pack_dir.is_dir():
            continue
        for path in sorted(pack_dir.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(cfg.root).as_posix()
            entries[f"pack:{rel}"] = _file_sha256(path)
    for check in cfg.checks:
        for token in _script_tokens(check.command):
            entries[f"script:{token}"] = _file_sha256(cfg.root / token)
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
