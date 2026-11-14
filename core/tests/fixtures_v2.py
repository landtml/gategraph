"""Fixture helpers for schema-2 projects (brief C2, step 3).

``make_project`` builds a throwaway project root from the example adapters and
packs in core/kernel/schema/examples. Layout (plan 1.7 and 1.13):

    <root>/harness/adapter/adapter.yaml     the chosen example adapter
    <root>/packs/<name>/pack.yaml           each pack the adapter names
    <root>/<unit_dir>/<UNIT_SLUG>/          one empty unit

Standard library and PyYAML only.
"""
import json
import shutil
from pathlib import Path

import yaml

CORE = Path(__file__).resolve().parents[1]          # the core/ package dir
EXAMPLES = CORE / "kernel" / "schema" / "examples"
ADAPTERS = ("minimal", "five_gate", "research")
UNIT_SLUG = "unit-001"


def make_project(tmpdir, adapter="minimal"):
    """Return the root of a new project using the named example adapter."""
    if adapter not in ADAPTERS:
        raise ValueError(f"adapter must be one of {ADAPTERS}, got {adapter!r}")
    root = Path(tmpdir)
    adapter_dir = root / "harness" / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    source = EXAMPLES / "adapters" / f"{adapter}.yaml"
    shutil.copyfile(source, adapter_dir / "adapter.yaml")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    for pack in data.get("packs", []):
        pack_dir = root / "packs" / pack
        pack_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / "packs" / pack / "pack.yaml", pack_dir / "pack.yaml")
    unit_path(root).mkdir(parents=True, exist_ok=True)
    return root


def unit_path(root):
    """The one empty unit that ``make_project`` creates."""
    root = Path(root)
    data = yaml.safe_load((root / "harness" / "adapter" / "adapter.yaml").read_text(encoding="utf-8"))
    return root / data["unit_dir"] / UNIT_SLUG


def write_unit_file(unit, name, text):
    """Write ``text`` to ``<unit>/<name>`` and return the path."""
    path = Path(unit) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read_manifest(unit):
    """The parsed ``<unit>/manifest.yaml``, or None when it is absent."""
    path = Path(unit) / "manifest.yaml"
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def read_evidence(unit):
    """The records of ``<unit>/evidence.jsonl`` in file order, or [] when absent."""
    path = Path(unit) / "evidence.jsonl"
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
