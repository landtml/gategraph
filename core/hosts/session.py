"""Session discipline on the host side: the stop rule, the session start context and the
handoff file (plan 1.16; bugs B07, B32, B47, B48).

Stop rule. A session is held to it only when it is armed: its touched-<session_id> marker
exists (``arm``, the single arming helper that every unit-changing command calls, B32). An
armed session is blocked from stopping while the active unit is ``in_progress`` with a gate
still ungated, or while HANDOFF.md is older than the marker. A unit waiting at a work request
projects ``in_progress``, so it blocks, and the reason names its next gate command. A unit waiting
for a person (an approval, an escalation or an ask) projects ``paused`` and does not block, and
neither does a unit that ``gate pause`` left stopped. An escalated, abandoned or complete unit
never blocks. The unit state comes from the projection (``projection.read_manifest``), never from
the graph layer.

After ``BLOCK_CAP`` blocks in one session the stop is allowed, and one automatic handoff stub
is written. A stub replaces any previous automatic stub, so the file never holds more than
one (B07).

Handoff writes are atomic (``paths.atomic_write``) and required sections are matched as whole
lines (B47). Session start shows the previous working note and then archives it (B48).

Failure policy: the shims fail open (exit 0). Functions here raise on real I/O errors; the
shims decide what that means.

Kernel layer: standard library, PyYAML, core.kernel and core.brand only.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import datetime
from pathlib import Path

import yaml

from core import brand
from core.hosts.bridge import HostEvent, HostReply
from core.kernel import config, doctor, memory, paths, projection
from core.kernel.types import Verdict

BLOCK_CAP = 3
HANDOFF_NAME = "HANDOFF.md"
REQUIRED_SECTIONS = ("## Now", "## Next")
AUTO_TAG = f"<!-- {brand.SLUG}:auto-handoff -->"
AUTO_TAGS = frozenset({AUTO_TAG, *(f"<!-- {m} -->" for m in brand.LEGACY_GEN_MARKERS
                                   if m.endswith("auto-handoff"))})
AUTO_HEADING = "## Auto-handoff"
_AUTO_HEADING_RE = re.compile(r"## Auto-handoff(?: .*)?")
ACTIVE_NAME = ".active"
NOW_ARCHIVE = "now-archive"
MAX_NOW_LINES = 10
MAX_FACTS = 8
RULE = "-" * 60


# --------------------------------------------------------------------------
# Session markers and arming (B32)
# --------------------------------------------------------------------------

def arm(root, session_id) -> bool:
    """Arm the stop rule for ``session_id``. Returns False, and arms nothing, for an id that
    fails validation (B45). Every command that changes a unit calls this."""
    try:
        marker = paths.marker_path(root, session_id)
    except ValueError:
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return True


def is_armed(root, session_id) -> bool:
    try:
        return paths.marker_path(root, session_id).exists()
    except ValueError:
        return False


def project_root(raw) -> Path:
    """The project root for a hook event: the payload's ``cwd`` when it holds an adapter,
    else the environment or the working directory. Raises RootNotFound."""
    cwd = raw.get("cwd") if isinstance(raw, dict) else None
    if isinstance(cwd, str) and cwd:
        try:
            return paths.find_root(cwd)
        except paths.RootNotFound:
            pass
    return paths.find_root()


# --------------------------------------------------------------------------
# Active unit, read from the projection
# --------------------------------------------------------------------------

def active_path(root, cfg) -> Path:
    return Path(root) / cfg.unit_dir / ACTIVE_NAME


def read_active(root, cfg) -> dict | None:
    """The ``.active`` pointer ({slug, session_id}), or None when there is no usable one."""
    try:
        data = yaml.safe_load(active_path(root, cfg).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or not paths.valid_slug(data.get("slug")):
        return None
    return data


def _next_gate(cfg, effective) -> str | None:
    return next((g.id for g in cfg.gates if g.id not in effective), None)


def unit_state(root, cfg) -> dict | None:
    """The active unit as the projection states it, or None when no unit is active.

    Keys: slug, and either ``error`` (manifest unusable), or ``legacy`` (schema-1 manifest,
    no evidence chain), or status, effective (gate ids), next (first ungated gate id or
    None), stale. A legacy unit also carries effective and next, worked out from its gates
    mapping the way the legacy manifest worked them out.
    """
    active = read_active(root, cfg)
    if active is None:
        return None
    slug = active["slug"]
    try:
        proj = projection.read_manifest(paths.unit_path(root, cfg.unit_dir, slug))
    except projection.ManifestError as exc:
        return {"slug": slug, "error": str(exc)}
    data = proj.data
    if proj.kind != "current":
        effective = _legacy_effective(root, cfg, slug, data)
        return {"slug": slug, "legacy": True, "status": data.get("status"),
                "effective": effective, "next": _next_gate(cfg, effective)}
    effective = [str(g) for g in (data.get("effective") or [])]
    return {"slug": slug, "legacy": False, "status": data.get("status"),
            "effective": effective, "next": _next_gate(cfg, effective),
            "stale": [str(g) for g in (data.get("stale") or [])]}


# --------------------------------------------------------------------------
# Legacy (schema-1) unit progress and the adapter command table
# --------------------------------------------------------------------------

LEGACY_MANIFEST = "manifest.yaml"


def raw_adapter(root) -> dict:
    """The adapter document as written, or {} when it cannot be read. Used for the command
    table, which the schema-1 translation keeps only as a check."""
    try:
        data = yaml.safe_load((Path(root) / config.ADAPTER_REL).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _workflow(raw: dict) -> dict:
    wf = raw.get("workflow")
    return wf if isinstance(wf, dict) else {}


def next_command(raw: dict, gate: str, slug: str) -> str:
    """The next-step command for ``gate`` from the adapter's gate_commands table, with
    ``{slug}`` filled in. Without one, the generic gate attempt."""
    table = _workflow(raw).get("gate_commands")
    command = table.get(gate) if isinstance(table, dict) else None
    if isinstance(command, str) and command.strip():
        return command.strip().replace("{slug}", slug)
    return f"gate attempt {gate} --unit {slug}"


def _legacy_hash_ok(unit: Path, rec: dict, final: bool, evidence: str | None) -> bool:
    """A hashed legacy gate still holds: every recorded file matches, and no file in the unit
    was left unrecorded. Mid-flow the declared reviewer evidence is exempt; the final gate
    exempts only the manifest (the legacy staleness rule, F36)."""
    recorded = rec.get("sha256")
    if recorded is None:
        return True  # no hash evidence (approval-set)
    if not isinstance(recorded, dict):
        return False
    for fname, expected in recorded.items():
        fpath = unit / str(fname)
        if not fpath.exists():
            return False
        if hashlib.sha256(fpath.read_bytes()).hexdigest() != expected:
            return False
    exclude = {LEGACY_MANIFEST} | (set() if final or not evidence else {evidence})
    if not unit.exists():
        return True
    names = {p.relative_to(unit).as_posix() for p in unit.rglob("*")
             if p.is_file() and p.name not in exclude}
    return names <= {str(name) for name in recorded}


def _legacy_effective(root, cfg, slug, data) -> list[str]:
    """The gates a legacy manifest effectively passes, in order: a gate needs a record, a
    hashed gate needs its hash to hold, and nothing after a gap or a stale gate counts."""
    order = [g.id for g in cfg.gates]
    recs = data.get("gates") if isinstance(data.get("gates"), dict) else {}
    raw = raw_adapter(root)
    hashed = {str(g) for g in (_workflow(raw).get("hashed_gates") or [])}
    reviewer = raw.get("reviewer")
    evidence = reviewer.get("evidence_file") if isinstance(reviewer, dict) else None
    evidence = str(evidence) if evidence else None
    unit = paths.unit_path(root, cfg.unit_dir, slug)
    effective: list[str] = []
    for index, gate in enumerate(order):
        rec = recs.get(gate)
        if rec is None:
            break
        rec = rec if isinstance(rec, dict) else {}
        if gate in hashed and not _legacy_hash_ok(
                unit, rec, index == len(order) - 1, evidence):
            break
        effective.append(gate)
    return effective


def _unit_reasons(root, cfg) -> list[str]:
    state = unit_state(root, cfg)
    if state is None or "error" in state:
        return []
    if state["status"] != "in_progress" or state["next"] is None:
        return []  # paused (a person is needed), terminal, or every gate effective
    command = next_command(raw_adapter(root), state["next"], state["slug"])
    return [f"unit '{state['slug']}': next gate is '{state['next']}': {command}. "
            f"Attempt it, or pause the unit, before stopping."]


# --------------------------------------------------------------------------
# Handoff file (B47)
# --------------------------------------------------------------------------

def handoff_path(root) -> Path:
    return Path(root) / HANDOFF_NAME


def is_stale(root, session_id) -> bool:
    """True iff the session is armed and touched the unit after HANDOFF.md was written."""
    if not is_armed(root, session_id):
        return False
    h = handoff_path(root)
    if not h.exists():
        return True
    return h.stat().st_mtime < paths.marker_path(root, session_id).stat().st_mtime


def validate_handoff(root) -> list[str]:
    """Structural problems with the handoff. Required sections match whole heading lines."""
    h = handoff_path(root)
    if not h.exists():
        return [f"{HANDOFF_NAME} is missing"]
    text = h.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return [f"{HANDOFF_NAME} is empty"]
    headings = {line.rstrip() for line in text.splitlines()}
    return [f"{HANDOFF_NAME} missing required section {s!r}"
            for s in REQUIRED_SECTIONS if s not in headings]


def _is_auto_heading(line: str) -> bool:
    return _AUTO_HEADING_RE.fullmatch(line) is not None


def trailing_stub_count(root) -> int:
    """Consecutive automatic sections at the tail of the handoff (the decay signal)."""
    h = handoff_path(root)
    if not h.exists():
        return 0
    count = 0
    for line in h.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            count = count + 1 if _is_auto_heading(line.rstrip()) else 0
    return count


def write_handoff(root, text: str) -> None:
    """Replace HANDOFF.md atomically: the old file stays whole until the new one is complete."""
    paths.atomic_write(handoff_path(root), text)


def _strip_auto_stubs(text: str) -> str:
    """``text`` without its automatic stubs. A stub starts at an auto tag or an auto heading
    and ends at the next ``## `` heading that is not an auto heading."""
    out = []
    in_stub = False
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if bare in AUTO_TAGS:
            in_stub = True
            continue
        if bare.startswith("## "):
            if _is_auto_heading(bare.rstrip()):
                in_stub = True
                continue
            in_stub = False
        if not in_stub:
            out.append(line)
    return "".join(out)


def _has_auto_stub(text: str) -> bool:
    return any(line.rstrip() in AUTO_TAGS or _is_auto_heading(line.rstrip())
               for line in text.splitlines())


def _git_status(root) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "  (git unavailable)"
    lines = out.stdout.strip().splitlines()[:15]
    return "\n".join(f"  {line}" for line in lines) if lines else "  (clean)"


def write_fallback_stub(root, cfg, now: datetime | None = None) -> None:
    """Write the deterministic automatic handoff section. It replaces any previous automatic
    section, so HANDOFF.md holds at most one (B07). Everything else is kept as it was."""
    ts = (now or datetime.now()).replace(microsecond=0).isoformat()
    lines = ["", AUTO_TAG, f"{AUTO_HEADING} {ts}", ""]

    state = unit_state(root, cfg)
    if state is None:
        lines.append("- no active unit")
    elif "error" in state:
        lines.append(f"- active unit: {state['slug']} (manifest unusable: {state['error']})")
    elif state["legacy"]:
        lines.append(f"- active unit: **{state['slug']}** [{state['status']}] (legacy manifest)")
    else:
        passed = ", ".join(state["effective"]) or "(none)"
        lines.append(f"- active unit: **{state['slug']}** [{state['status']}], "
                     f"gates passed: {passed}")
        if state["next"]:
            lines.append(f"- next gate: `{state['next']}`")
        else:
            lines.append("- all gates passed: unit complete")

    working = memory.read_now(root).strip()
    if working:
        lines += ["", "### Working state (now.md)", "",
                  "\n".join(working.splitlines()[:15])]
    lines += ["", "### git status", "", _git_status(root)]

    decisions = memory.read_decisions(root, last=3)
    if decisions:
        lines += ["", "### Recent decisions", ""]
        lines += [f"- [{d.get('type', '?')}] {d.get('decision', '')}" for d in decisions]
    lines.append("")

    h = handoff_path(root)
    existing = h.read_text(encoding="utf-8") if h.exists() else ""
    first = not _has_auto_stub(existing)
    write_handoff(root, _strip_auto_stubs(existing).rstrip("\n") + "\n"
                  + "\n".join(lines))
    if first:
        memory.append_decision(
            root, "auto-handoff",
            "stop-gate cap reached; deterministic fallback stub written to HANDOFF.md",
            rationale="agent did not consolidate; the stub preserves state for the next session")


# --------------------------------------------------------------------------
# Stop rule
# --------------------------------------------------------------------------

def _count_path(root, session_id) -> Path:
    return paths.state_dir(root) / f"stopcount-{session_id}"


def _read_count(root, session_id) -> int:
    try:
        return int(_count_path(root, session_id).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_count(root, session_id, n: int) -> None:
    p = _count_path(root, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(n), encoding="utf-8")


def stop_reply(root, event: HostEvent, cfg=None) -> HostReply:
    """The stop decision for one Stop event. ``cfg`` defaults to the project configuration."""
    sid = event.session
    if not is_armed(root, sid):
        return HostReply(Verdict.ALLOW)  # this session did no tracked work
    if cfg is None:
        try:
            cfg = config.load(root)
        except config.ConfigError:
            return HostReply(Verdict.ALLOW, reason="stop allowed: configuration unreadable")

    reasons = _unit_reasons(root, cfg)
    if is_stale(root, sid):
        reasons.append("HANDOFF.md is stale: this session did work it does not reflect. "
                       "Consolidate before stopping: update Now, Next, Decisions and "
                       "Gotchas, and promote durable facts to memory.")
    if not reasons:
        return HostReply(Verdict.ALLOW)

    count = _read_count(root, sid)
    if count >= BLOCK_CAP:
        try:
            write_fallback_stub(root, cfg)
            stubbed = "one automatic handoff stub is in HANDOFF.md"
        except OSError as exc:
            stubbed = f"automatic handoff stub FAILED ({exc})"
        return HostReply(Verdict.ALLOW, reason=(
            f"stop allowed after {count} blocks this session; {stubbed}. "
            f"Still unresolved: {' | '.join(reasons)}"))
    _write_count(root, sid, count + 1)
    return HostReply(Verdict.DENY, reason=" ALSO: ".join(reasons), block=True)


# --------------------------------------------------------------------------
# Session start (B48)
# --------------------------------------------------------------------------

def _doctor_lines(root) -> list[str]:
    cache, _note = doctor.read_cache(root)
    if not cache or cache.get("ok"):
        return []
    failing = [c["name"] for c in cache.get("checks", []) if c.get("required") and not c.get("ok")]
    if not failing:
        return []
    return [f"  toolchain failing: {', '.join(failing)}; run the doctor"]


def start_context(root, cfg, working: str) -> str:
    """The bounded context block for a session start, or "" on a truly fresh project."""
    state = unit_state(root, cfg) if cfg is not None else None
    try:
        facts = memory.read_facts(root)
        facts_note = None
    except memory.FactsCorrupt:
        facts, facts_note = [], "  facts: facts.yaml is unreadable"
    decisions = memory.read_decisions(root, last=3)
    if not (state or working or facts or decisions):
        return ""

    name = cfg.project if cfg is not None else brand.NAME
    lines = [f"-- {name}: session context", "  router: MEMORY.md (open it first; read at "
             "most 2-5 routed files)"]
    if state is not None:
        slug = state["slug"]
        if "error" in state:
            lines.append(f"  {slug}: manifest unusable ({state['error']})")
        elif state["legacy"]:
            lines.append(f"  active unit: {slug} [{state['status']}] (legacy manifest)")
        else:
            lines.append(f"  active unit: {slug} [{state['status']}]")
            lines.append(f"  gates passed: {', '.join(state['effective']) or '(none yet)'}")
            if state["next"]:
                lines.append(f"  next gate: '{state['next']}'")
            else:
                lines.append("  all gates passed: unit complete.")
            if state["stale"]:
                lines.append(f"  STALE: {', '.join(state['stale'])}")
    if working:
        lines.append("  working memory (now.md):")
        lines += [f"    {line}" for line in working.splitlines()[:MAX_NOW_LINES]]
    if facts_note:
        lines.append(facts_note)
    elif facts:
        lines.append(f"  facts ({len(facts)}):")
        lines += [f"    - {f.get('text', '')}" for f in facts[-MAX_FACTS:]]
    if decisions:
        lines.append("  recent decisions:")
        lines += [f"    - [{d.get('type', '?')}] {d.get('decision', '')}" for d in decisions]
    lines += _doctor_lines(root)
    lines.append(RULE)
    return "\n".join(lines)


def archive_working_note(root, note: str) -> Path:
    """Move the working note to the archive and clear it. Returns the archive file."""
    base = paths.state_dir(root) / NOW_ARCHIVE
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    target, n = base / f"{stamp}.md", 1
    while target.exists():
        target = base / f"{stamp}-{n}.md"
        n += 1
    paths.atomic_write(target, note + "\n")
    memory.clear_now(root)
    return target


def start_reply(root, event: HostEvent, cfg=None) -> HostReply:
    """The session start context. Shows the previous working note, then archives it."""
    if cfg is None:
        try:
            cfg = config.load(root)
        except config.ConfigError:
            cfg = None
    working = memory.read_now(root).strip()
    text = start_context(root, cfg, working)
    if working:
        try:
            archive_working_note(root, working)
        except OSError as exc:
            text += f"\n  working note NOT archived ({exc})"
    return HostReply(Verdict.ALLOW, context=text or None)
