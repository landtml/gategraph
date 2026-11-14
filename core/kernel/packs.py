"""Domain packs: the packs an adapter names, loaded and checked (plan section 1.13; brief D1).

``load_packs(cfg)`` returns one ``Pack`` per name in ``cfg.packs``, in that order,
or raises ``PackError`` naming every problem.

The adapter loader (config.py, K2) already reads each ``pack.yaml``, maps its
checks into ``cfg.checks`` (``cmd`` becomes ``command``, ``{pack}`` becomes the
pack directory) and rejects duplicate check ids. This module reads the rest of
each pack (readers, tools, gate presets, doctor advisories), checks the tool
names against the toolbox, and hands each reader to the caller's registration
callable.

Standard library and PyYAML only.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from . import config
from .types import CheckSpec

# The built-in tool names of plan section 1.10 (the MCP tools come from the
# adapter, not from a pack). A toolbox may pass its own set as ``known_tools``.
BUILTIN_TOOLS = frozenset({
    "read_file", "list_dir", "glob", "grep", "write_file", "edit_file",
    "run_command", "fetch_url", "web_search", "query_table", "profile_table",
    "render_document", "extract_text", "gate_status", "request_gate",
    "note_decision", "commit_fact", "escalate",
})


class PackError(Exception):
    """A pack is missing, unreadable, invalid, or clashes with the configuration.

    ``problems`` lists one ``"path: message"`` entry per problem.
    """

    def __init__(self, problems):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__("invalid pack configuration:\n  - " + "\n  - ".join(self.problems))


@dataclasses.dataclass(frozen=True)
class PackReader:
    """One reader declaration: files matching ``match`` are read by ``reader``."""
    pack: str
    match: tuple[str, ...]
    reader: str


@dataclasses.dataclass(frozen=True)
class Advisory:
    """A doctor check that warns but never fails the gate."""
    id: str
    cmd: str


@dataclasses.dataclass(frozen=True)
class Pack:
    name: str
    version: int
    root: Path                      # packs/<name> under the project root
    readers: tuple[PackReader, ...]
    tools: tuple[str, ...]
    checks: tuple[CheckSpec, ...]   # the same objects as cfg.checks
    gate_presets: Mapping[str, tuple[str, ...]]
    advisory: tuple[Advisory, ...]


def merge_settings(pack_settings: Mapping[str, Any], adapter_settings: Mapping[str, Any]) -> dict:
    """Adapter settings override pack settings, key by key. Neither input is changed."""
    merged = dict(pack_settings)
    merged.update(adapter_settings)
    return merged


def _schema_doc() -> dict:
    return config.parse_document((config.SCHEMA_DIR / config.PACK_SCHEMA).read_text(encoding="utf-8"))


def _read_pack(root: Path, name: str, known_tools: frozenset, problems: list[str]):
    """(Pack, check ids in file order), or (None, []) with the problems appended."""
    where = f"packs/{name}/pack.yaml"
    path = root / "packs" / name / "pack.yaml"
    if not path.is_file():
        problems.append(f"{where}: pack '{name}' not found")
        return None, []
    try:
        doc = config.parse_document(path.read_text(encoding="utf-8"))
    except Exception as exc:  # YAML errors; config.load has already validated the same file
        problems.append(f"{where}: not valid YAML: {exc}")
        return None, []
    schema_problems = [f"{where} {p}" for p in config.validate_schema(doc, _schema_doc())]
    if schema_problems or not isinstance(doc, dict):
        problems.extend(schema_problems or [f"{where}: root must be a mapping"])
        return None, []
    if doc["pack"] != name:
        problems.append(f"{where} pack: name '{doc['pack']}' does not match directory '{name}'")
        return None, []

    before = len(problems)
    for index, tool in enumerate(doc.get("tools", [])):
        if tool not in known_tools:
            problems.append(f"{where} tools[{index}]: tool '{tool}' is not known to the toolbox")
    presets: dict[str, tuple[str, ...]] = {}
    for key, gates in doc.get("gate_presets", {}).items():
        if isinstance(gates, list):
            presets[key] = tuple(str(g) for g in gates)
        else:
            problems.append(f"{where} gate_presets.{key}: expected a list of gate ids")
    if len(problems) > before:
        return None, []

    advisory = tuple(Advisory(id=a["id"], cmd=a["cmd"])
                     for a in doc.get("doctor", {}).get("advisory", []))
    readers = tuple(PackReader(pack=name, match=tuple(r["match"]), reader=r["reader"])
                    for r in doc.get("readers", []))
    ids = [item["id"] for item in doc.get("checks", [])]
    pack = Pack(
        name=name,
        version=doc["version"],
        root=root / "packs" / name,
        readers=readers,
        tools=tuple(doc.get("tools", [])),
        checks=(),  # filled in by load_packs from cfg.checks
        gate_presets=presets,
        advisory=advisory,
    )
    return pack, ids


def load_packs(cfg: config.Config, *,
               known_tools: Optional[Iterable[str]] = None,
               register_reader: Optional[Callable[[PackReader], None]] = None) -> list[Pack]:
    """Return the packs named by ``cfg``, in order, or raise PackError.

    ``known_tools`` is the toolbox's tool names (default: ``BUILTIN_TOOLS``).
    ``register_reader`` is called once per reader, in pack order, after every
    check has passed. kernel.extract has no registry yet, so the caller supplies
    the registration (see the report that accompanies this module).
    """
    tools = frozenset(BUILTIN_TOOLS if known_tools is None else known_tools)
    problems: list[str] = []
    packs: list[Pack] = []
    ids_of: dict[str, list[str]] = {}
    for name in cfg.packs:
        pack, ids = _read_pack(cfg.root, name, tools, problems)
        if pack is not None:
            packs.append(pack)
            ids_of[name] = ids

    # Check ids: each pack check must be defined once in the configuration, and
    # no two packs may define the same id.
    owner: dict[str, str] = {}
    finished: list[Pack] = []
    for pack in packs:
        where = f"packs/{pack.name}/pack.yaml"
        resolved: list[CheckSpec] = []
        for index, check in enumerate(ids_of[pack.name]):
            if check in owner:
                problems.append(f"{where} checks[{index}].id: check id '{check}' is "
                                f"also defined by pack '{owner[check]}'")
                continue
            owner[check] = pack.name
            occurrences = [c for c in cfg.checks if c.id == check]
            if not occurrences:
                problems.append(f"{where} checks[{index}].id: check '{check}' is missing "
                                f"from the configuration")
                continue
            if len(occurrences) > 1:
                problems.append(f"{where} checks[{index}].id: check id '{check}' collides "
                                f"with an adapter check: the adapter and pack '{pack.name}' "
                                f"both define it")
                continue
            resolved.append(occurrences[0])
        finished.append(dataclasses.replace(pack, checks=tuple(resolved)))

    if problems:
        raise PackError(problems)
    if register_reader is not None:
        for pack in finished:
            for reader in pack.readers:
                register_reader(reader)
    return finished

