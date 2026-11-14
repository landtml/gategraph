"""Public kernel API: one import surface, resolved lazily.

``import core.kernel.api`` loads no other kernel module. Each attribute is
imported from its owning module the first time it is read, and cached on this
module. Unknown names raise AttributeError.

Type names are re-exported from core.kernel.types. Names owned by modules that
later tasks write are listed here now, so callers can code against the final
surface.
"""
import importlib as _importlib

_MODULE_EXPORTS = {
    "paths": ("find_root", "unit_path", "state_dir", "valid_slug", "atomic_write"),
    "locks": ("file_lock", "LockHeld"),
    "config": ("load", "Config", "ConfigError", "config_digest"),
    "policy": ("compile_policies", "scan_text", "scan_path", "normalize"),
    "globs": ("glob_match",),
    "extract": ("read_artifact",),
    "surface": ("hash_surface", "effective_gates", "stale_gates", "store_surface",
                "load_surface", "SurfaceMissing"),
    "evidence": ("make_record", "verify_chain", "ChainError"),
    "checks": ("run_check", "select_checks"),
    "guard": ("decide", "denial_message"),
    "budget": ("should_stop", "refuse_unbounded"),
    "faults": ("classify", "is_retryable"),
    "route": ("next_step",),
    "memory": ("read_now", "write_now", "append_decision", "read_facts", "commit_fact"),
    "ledger": ("record", "read_ledger"),
    "projection": ("write_projections", "read_manifest"),
    "verify": ("verify_unit", "VerifyReport"),
    "selflint": ("run_lint",),
    "gen": ("regenerate",),
    "doctor": ("run_doctor",),
    "reap": ("reap",),
    "packs": ("load_packs",),
}

_TYPE_NAMES = (
    "Action", "ActionKind", "Actor", "ActorKind", "Approval", "Budget",
    "CheckResult", "CheckSpec", "CheckStatus", "Decision", "EvidenceRecord",
    "FaultKind", "FaultRoute", "Finding", "GateSpec", "HumanAnswer", "HumanDecision",
    "HumanKind", "HumanRequest", "ProducerKind", "ProducerRecord", "RecordKind",
    "RepairSpec", "Severity", "Spend", "Surface", "SurfaceSpec", "Verdict",
    "VerifierSpec", "VerifierVerdict",
)

# Name to owning module (relative to core.kernel).
_LOOKUP = {}
for _module, _names in _MODULE_EXPORTS.items():
    for _name in _names:
        _LOOKUP[_name] = _module
for _name in _TYPE_NAMES:
    _LOOKUP[_name] = "types"
del _module, _names, _name


def __getattr__(name):
    module = _LOOKUP.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    owner = _importlib.import_module(f"{__package__}.{module}")
    value = getattr(owner, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LOOKUP))
