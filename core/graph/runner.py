"""The runner: the only code that invokes a graph (plan section 1.15; brief G1).

``run(graph, thread_id, input_or_command, on_return)`` does, in order:

1. Takes the exclusive, non-blocking per-thread lock from ``kernel.locks``
   (file ``<lock_dir>/<quoted thread id>.lock``). A second runner on the same
   thread, in this process or another, gets ``LockHeld`` naming the holder and
   nothing runs. ``on_return`` is not called in that case: the holder projects.
2. Invokes with ``durability="sync"`` and an explicit ``recursion_limit``.
3. Calls ``on_return(snapshot)`` after the invoke, whether it returned (also
   after an interrupt) or raised, then releases the lock. The snapshot is the
   ``get_state`` result for the thread. The exception, if any, then propagates.

The lock directory defaults to ``locks/`` beside the SQLite file of the graph's
checkpointer, that is ``paths.state_dir(root) / "locks"`` for a checkpointer made
by ``store.make_checkpointer``. In-memory checkpointers have no file, so they need
``lock_dir``.

``pending`` and ``checkpoint_reader`` only read. They take no lock.

Standard library, LangGraph and core.kernel.locks only.
"""
import pathlib
import sqlite3
import urllib.parse
from typing import Any, Callable, Mapping

from core.kernel.locks import file_lock

# Explicit bound for one run. Plan 1.15 derives it from the gate count and the
# repair budgets; callers pass that value. This is the fallback when they do not.
DEFAULT_RECURSION_LIMIT = 500
LOCK_SUBDIR = "locks"  # beside the checkpoint database, inside paths.state_dir(root)


def _config(thread_id: str, recursion_limit: int | None = None) -> dict:
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    return config


def lock_path(lock_dir, thread_id: str) -> pathlib.Path:
    """The lock file for one thread. Quoting keeps "project/unit" to one file name."""
    return pathlib.Path(lock_dir) / (urllib.parse.quote(thread_id, safe="") + ".lock")


def _default_lock_dir(graph) -> pathlib.Path:
    conn = getattr(getattr(graph, "checkpointer", None), "conn", None)
    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("no SQLite file behind this graph; pass lock_dir explicitly")
    for _seq, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main" and filename:
            return pathlib.Path(filename).parent / LOCK_SUBDIR
    raise ValueError("the SQLite checkpointer is in memory; pass lock_dir explicitly")


def run(graph, thread_id: str, input_or_command, on_return: Callable[[Any], Any], *,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT, lock_dir=None,
        owner: str | None = None):
    """Run or proceed one thread under its lock. Returns the invoke result.

    ``input_or_command`` is the first input, a ``Command`` (for a proceed), or
    ``None`` to continue from the last checkpoint.
    """
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("thread_id must be a non-empty string")
    directory = pathlib.Path(lock_dir) if lock_dir is not None else _default_lock_dir(graph)
    config = _config(thread_id, recursion_limit)
    with file_lock(lock_path(directory, thread_id), owner=owner or f"runner {thread_id}"):
        try:
            result = graph.invoke(input_or_command, config, durability="sync")
        finally:
            on_return(graph.get_state(config))
    return result


def pending(graph, thread_id: str) -> list:
    """The payloads of the interrupts waiting on this thread, in order. [] if none."""
    snapshot = graph.get_state(_config(thread_id))
    return [item.value for task in snapshot.tasks for item in task.interrupts]


def checkpoint_reader(graph) -> Callable[[str], Mapping[str, Any] | None]:
    """A callable for kernel.verify: ``reader(thread_id)`` returns the latest
    checkpoint as ``{"values", "checkpoint_id", "next"}``, or None if the thread
    has no checkpoint. Read-only."""

    def read(thread_id: str):
        snapshot = graph.get_state(_config(thread_id))
        checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id:
            return None
        return {"values": dict(snapshot.values), "checkpoint_id": checkpoint_id,
                "next": list(snapshot.next)}

    return read
