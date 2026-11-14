"""Checkpointer factory (plan sections 1.8 and 1.15, step 2; brief G1).

``make_checkpointer(cfg)`` returns the saver a graph is compiled with:

- SQLite at ``paths.state_dir(root) / "checkpoints.sqlite"`` (WAL, tables created
  here), the one project state directory that the guard's state-dir rule protects.
  Unless ``cfg["in_memory"]`` is true, which gives an InMemorySaver.
- The serializer is strict: ``LANGGRAPH_STRICT_MSGPACK=true`` is set before
  langgraph reads it, and the serializer is built with pickle fallback off and
  an empty msgpack allow-list (RESULTS Q14). Passing the allow-list explicitly
  means strictness does not depend on import order.
- ``cfg["privacy"] == "strict"`` wraps the strict serializer in
  ``EncryptedSerializer``. Its key is read from ``LANGGRAPH_AES_KEY`` by
  langgraph. It needs pycryptodome, which is not in core/requirements.lock; if
  it is missing the factory raises ImportError and does not fall back.

``cfg`` is a plain mapping: ``root`` (project root), ``in_memory`` (bool),
``privacy`` ("standard" or "strict"). Other keys are ignored.

Standard library and LangGraph checkpoint packages only.
"""
import os

# Read by langgraph at import time of its msgpack module; set before that import.
os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

import pathlib  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
from typing import Any, Mapping  # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer  # noqa: E402
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from core.kernel import paths  # noqa: E402

DB_NAME = "checkpoints.sqlite"
# Schema setup switches the file to WAL, which SQLite refuses to do while
# another connection in this process is inside its own setup. One at a time.
_SETUP_LOCK = threading.Lock()
PRIVACY_LEVELS = ("standard", "strict")


def strict_serializer() -> JsonPlusSerializer:
    """Built-in safe types only: no pickle, empty msgpack allow-list (Q14)."""
    return JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)


def checkpoint_path(root) -> pathlib.Path:
    """Where the SQLite checkpoints of a project live: the kernel state directory,
    paths.state_dir(root). The runner's locks/ directory sits beside this file."""
    return paths.state_dir(root) / DB_NAME


def make_serializer(privacy: str = "standard"):
    """The serializer for a privacy level. Unknown levels are refused."""
    if privacy not in PRIVACY_LEVELS:
        raise ValueError(f"unknown privacy level {privacy!r}; expected one of {PRIVACY_LEVELS}")
    serde = strict_serializer()
    if privacy == "strict":
        return EncryptedSerializer.from_pycryptodome_aes(serde=serde)
    return serde


def make_checkpointer(cfg: Mapping[str, Any]):
    """Build the checkpointer for one project. See the module docstring."""
    serde = make_serializer(cfg.get("privacy", "standard"))
    if cfg.get("in_memory"):
        return InMemorySaver(serde=serde)
    if not cfg.get("root"):
        raise ValueError("make_checkpointer needs cfg['root'] unless in_memory is set")
    path = checkpoint_path(cfg["root"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SETUP_LOCK:
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        saver = SqliteSaver(conn, serde=serde)
        saver.setup()
    return saver
