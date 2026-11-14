"""Version-1 ``config``: ``load(root)``, ``Config`` and ``ConfigError`` (plan 1.18; brief M3).

``load`` reads the project through the kernel (schema 2, or schema 1 through the translator) and
returns a view with the attribute names version-1 scripts read: ``gates`` (ids), ``hashed_gates``,
``tool_set_gates``, ``gate_commands``, ``checks`` (mappings with ``id``, ``cmd``, ``on``,
``severity``), ``unit_dir``, ``project``, ``deny``, ``ask``, ``protected_paths`` and ``reviewer``.
``ConfigError`` is the kernel's class, so one ``except`` catches both.

Version-1 names that have no schema-2 counterpart come back empty: ``reviewer`` is ``{}`` (its
only effect, the evidence exclusion, lives in the gate surfaces), ``doctor_*``, ``docs_lint``,
``state_reap``, ``escalation`` and ``loop``.
"""
from __future__ import annotations

from pathlib import Path

from core.compat.shims import deprecated
from core.kernel import config as _kconfig

ConfigError = _kconfig.ConfigError
_SHIM = "config.load"
_REPLACEMENT = "core.kernel.config.load"
_APPROVAL_NEXT = "gate status --unit {slug}"


class Config:
    """The version-1 view of one project. ``kernel`` is the schema-2 ``Config`` underneath."""

    def __init__(self, kernel_config):
        self.kernel = kernel_config
        self.root = Path(kernel_config.root)
        self.project = kernel_config.project
        self.artifact_noun = kernel_config.unit_noun
        self.unit_dir = kernel_config.unit_dir
        self.gates = [gate.id for gate in kernel_config.gates]
        self.tool_set_gates = [g.id for g in kernel_config.gates if g.checks]
        self.hashed_gates = [g.id for g in kernel_config.gates
                             if g.surface is not None and g.surface.include]
        self.checks = [{"id": c.id, "cmd": c.command, "on": list(c.on),
                        "severity": c.severity.value} for c in kernel_config.checks]
        self.gate_commands = {}
        by_id = {c.id: c for c in kernel_config.checks}
        for gate in kernel_config.gates:
            if gate.checks and gate.checks[0] in by_id:
                # The legacy producer of a gate is the command its check runs (plan 1.18).
                self.gate_commands[gate.id] = by_id[gate.checks[0]].command
            else:
                self.gate_commands[gate.id] = _APPROVAL_NEXT
        self.deny = list(kernel_config.deny)
        self.ask = list(kernel_config.ask)
        self.protected_paths = list(kernel_config.protected_paths)
        self.reviewer = {}
        self.doctor_required = []
        self.doctor_advisory = []
        self.docs_lint = {}
        self.state_reap = {}
        self.escalation = {}
        self.loop = {}

    def gate_command(self, gate: str, slug: str) -> str:
        """The next-step command for a gate, the version-1 text with ``{slug}`` filled in."""
        return str(self.gate_commands.get(gate, "")).replace("{slug}", slug)


def load(root) -> Config:
    """Return the version-1 view of the project at ``root``, or raise ``ConfigError``."""
    deprecated(_SHIM, _REPLACEMENT, root)
    return Config(_kconfig.load(Path(root)))
