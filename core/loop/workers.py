"""The ``claude -p`` seam, spawn a fresh headless worker at one judgment point, get a card back.

This is the *only* place the loop shells out to an LLM. Everything above it (driver, dedup,
decide) is deterministic Python; a worker is invoked at PROPOSE / CHECK / DEDUP and returns a
**validated, structured card** that the driver, not the worker, writes to the registry.

Design (all empirically grounded in FLAGS.md, CLI 2.1.190, subscription OAuth):
- **Subscription auth, never an API key.** A subprocess ``claude -p`` reuses ``~/.claude`` creds;
  we deliberately do *not* set ``ANTHROPIC_API_KEY`` (§1). ``build_env`` scrubs it if present so a
  stray shell export can't silently flip a worker onto metered API billing.
- **Blunt tool-allow + hook scope.** ``--allowedTools`` path globs are unreliable in ``-p`` (§3), so
  makers get bare ``Write``/``Edit`` and the real path scope is enforced by the ``write_guard``
  PreToolUse hook keyed on ``$HARNESS_WRITE_SCOPE`` (§4, §5).
- **Bounded.** No ``--max-turns`` in this CLI (§8); we bound with a subprocess ``timeout`` **and**
  ``--max-budget-usd`` (→ ``subtype == error_max_budget_usd``).
- **Injectable ``Runner``.** ``invoke``/``run_contract`` take a runner, so the whole seam is
  unit-testable with a mock, no subprocess, no network, no spend.
- **Driver re-validates.** We never trust ``--json-schema`` conformance alone; ``run_contract``
  re-checks against ``contracts.py`` and turns failures into feedback-carrying strikes.

Outcome classification is the driver's contract: infra failures (auth, spawn, timeout, budget)
raise typed exceptions to be routed by ``errors.py``, they are **not** variant strikes; only a
persistently malformed / schema-violating payload is a strike (``ContractError``).
Isolating the subprocess seam here, behind an injectable ``Runner``, is what keeps the
rest of the loop testable without a live Claude Code install.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from core.loop.contracts import validate

# --- outcome taxonomy -------------------------------------------------------------------------
# Infra vs. variant is the load-bearing distinction: infra faults must not be charged to the
# variant as a strike, or a flaky spawn looks like a bad hypothesis.


class WorkerError(Exception):
    """Base, something went wrong invoking a worker (default: infra, not a variant strike)."""


class AuthError(WorkerError):
    """Subscription/login problem (e.g. creds missing/expired). Escalate; do not strike."""


class RateLimited(WorkerError):
    """Subscription 5-hour window / overloaded upstream. Caller persists state and backs off."""


class BudgetExceeded(WorkerError):
    """``--max-budget-usd`` tripped (``subtype == error_max_budget_usd``). A runaway worker."""


class WorkerTimeout(WorkerError):
    """Subprocess exceeded its wall-clock ``timeout_s``."""


class SpawnError(WorkerError):
    """Could not launch ``claude`` or it exited without usable JSON (infra)."""


class ContractError(WorkerError):
    """Output stayed malformed / schema-violating after all strikes, a *variant* failure.

    Carries the strikes' accumulated real spend (``cost_usd``/``tokens``) so the driver can
    bill it onto a receipt, the budget backstop must see what the strikes actually burned."""

    def __init__(self, message: str, errors: "list[str]", attempts: int,
                 cost_usd: float = 0.0, tokens: int = 0) -> None:
        super().__init__(message)
        self.errors = errors
        self.attempts = attempts
        self.cost_usd = cost_usd
        self.tokens = tokens


# heuristic markers for classifying an errored envelope. These are best-effort string sniffs; the
# only signal FLAGS.md proved live is the budget subtype (§8). Kept as constants so the day we see a
# real rate-limit envelope we tighten *one* place. Order of checks in _classify matters.
_AUTH_MARKERS = ("invalid api key", "authentication", "oauth", "unauthorized", "please run /login", "login")
_RATE_MARKERS = ("rate limit", "rate_limit", "overloaded", "429", "usage limit", "too many requests",
                 "session limit", "session_limit")
# Unambiguous control-plane limit banners the CLI can emit even on a *success*-labelled envelope.
# These are specific enough that they never appear in a valid worker card, so we detect them
# regardless of ``is_error``, a limit misread as success would otherwise be parsed as a bad
# variant result and struck (errors.py: the most damaging fault).
_HARD_LIMIT_MARKERS = ("hit your session limit", "session limit ·", "usage limit reached", "5-hour limit")
_BUDGET_SUBTYPE = "error_max_budget_usd"


# --- worker spec + result ---------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerSpec:
    """A fully-resolved single worker invocation. Frozen: build once, ``replace`` for retries."""

    prompt: str
    model: str = "opus"
    json_schema: "dict | None" = None
    allowed_tools: "tuple[str, ...]" = ()
    disallowed_tools: "tuple[str, ...]" = (
        "Bash",
        "Task",
        "WebSearch",
        "WebFetch",
        "NotebookEdit",
        "TodoWrite",
    )
    permission_mode: str = "default"
    write_scope: "str | None" = None  # abs dir → HARNESS_WRITE_SCOPE for the write_guard hook
    settings_path: "str | None" = None  # driver-written settings JSON carrying the hook
    isolate_settings: bool = True  # --setting-sources "" → ignore the user's personal config
    budget_usd: float = 1.0
    timeout_s: int = 1200
    extra_args: "tuple[str, ...]" = ()


@dataclass
class WorkerResult:
    """Parsed outer envelope of a completed (non-infra-failed) worker run."""

    data: "dict | None"  # inner `result` parsed as JSON (when json_schema set & it parsed), else None
    raw_result: str  # the raw `result` text as returned
    is_error: bool
    subtype: "str | None"
    cost_usd: float
    usage: dict = field(default_factory=dict)
    permission_denials: list = field(default_factory=list)
    session_id: "str | None" = None
    num_turns: "int | None" = None
    returncode: int = 0

    @property
    def denied_tools(self) -> "list[str]":
        """Tool names the CLI silently denied, the truth about scope violations (FLAGS §2)."""
        return [d.get("tool_name", "") for d in self.permission_denials]


# --- runner seam ------------------------------------------------------------------------------


@dataclass
class RunOutput:
    returncode: int
    stdout: str
    stderr: str


# A Runner turns an argv+env into a RunOutput. Real one shells out; tests inject a mock.
Runner = Callable[["list[str]", "dict[str, str]", int], RunOutput]


def subprocess_runner(argv: "list[str]", env: "dict[str, str]", timeout_s: int) -> RunOutput:
    """Default runner: spawn ``claude`` with stdin closed (§9) and a hard wall-clock timeout.

    The worker gets its own session (process group) so a timeout kills EVERYTHING it spawned.
    a lone ``proc.kill()`` would leave grandchildren writing into the variant dir while the
    driver has already moved on to a repair attempt in the same directory."""
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,  # §9: never let -p stall waiting on piped stdin
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # pgid == proc.pid → killpg reaps the whole tree
        )
    except FileNotFoundError as exc:
        raise SpawnError("`claude` executable not found on PATH") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()  # group already gone / unkillable: fall back to the child
        proc.communicate()  # drain + reap; everything holding the pipes is dead
        raise WorkerTimeout(f"worker exceeded {timeout_s}s") from exc
    return RunOutput(proc.returncode, stdout or "", stderr or "")


# --- argv / env construction ------------------------------------------------------------------


def build_env(spec: WorkerSpec, base: "dict[str, str] | None" = None) -> "dict[str, str]":
    """Inherit the environment, set the hook's write scope, and **scrub any API key** so a worker
    can only ever run on the subscription (FLAGS §1)."""
    env = dict(os.environ if base is None else base)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    if spec.write_scope is not None:
        env["HARNESS_WRITE_SCOPE"] = os.path.realpath(spec.write_scope)
    return env


def build_argv(spec: WorkerSpec) -> "list[str]":
    """Assemble the canonical ``claude -p`` argv (FLAGS.md "Canonical worker invocation")."""
    argv: "list[str]" = ["claude", "-p", spec.prompt, "--model", spec.model, "--output-format", "json"]
    if spec.json_schema is not None:
        argv += ["--json-schema", json.dumps(spec.json_schema)]
    argv += ["--permission-mode", spec.permission_mode]
    if spec.isolate_settings:
        argv += ["--setting-sources", ""]
    if spec.settings_path is not None:
        argv += ["--settings", spec.settings_path]
    if spec.allowed_tools:
        argv += ["--allowedTools", *spec.allowed_tools]
    if spec.disallowed_tools:
        argv += ["--disallowedTools", *spec.disallowed_tools]
    argv += [
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-budget-usd",
        f"{spec.budget_usd:.2f}",
    ]
    argv += list(spec.extra_args)
    return argv


# --- invocation -------------------------------------------------------------------------------


def _classify(envelope: dict, stderr: str) -> None:
    """Raise a typed infra error if the envelope/stderr signals one; else return (caller proceeds).

    Only the budget subtype is a proven-live signal; auth/rate are heuristic sniffs over the
    envelope's ``result`` text plus stderr, deliberately centralised for the day we observe one.
    """
    def _raise(exc: WorkerError) -> None:
        # Even a failed attempt was billed, carry its spend so callers can account for it.
        exc.cost_usd = float(envelope.get("total_cost_usd") or 0.0)
        raise exc

    subtype = envelope.get("subtype")
    if subtype == _BUDGET_SUBTYPE:
        _raise(BudgetExceeded("worker hit --max-budget-usd"))
    haystack = f"{envelope.get('result', '')}\n{stderr}".lower()
    # A hard limit banner is control-plane, catch it even on a success-labelled envelope (the CLI
    # mislabels the 5h-window message as is_error=False), before it can be struck as a variant fault.
    if any(m in haystack for m in _HARD_LIMIT_MARKERS):
        _raise(RateLimited(f"worker rate-limited: {envelope.get('result', '')[:200]}"))
    if not envelope.get("is_error"):
        return
    if any(m in haystack for m in _AUTH_MARKERS):
        _raise(AuthError(f"worker auth failure: {envelope.get('result', '')[:200]}"))
    if any(m in haystack for m in _RATE_MARKERS):
        _raise(RateLimited(f"worker rate-limited: {envelope.get('result', '')[:200]}"))
    # errored but unclassified → treat as infra spawn/exec fault, not a variant strike
    _raise(SpawnError(f"worker errored (subtype={subtype}): {envelope.get('result', '')[:200]}"))


def invoke(spec: WorkerSpec, runner: Runner = subprocess_runner) -> WorkerResult:
    """Run one worker and return its parsed envelope, or raise a typed :class:`WorkerError`."""
    out = runner(build_argv(spec), build_env(spec), spec.timeout_s)

    if not out.stdout.strip():
        raise SpawnError(
            f"worker produced no stdout (rc={out.returncode}): {out.stderr.strip()[:300]}"
        )
    try:
        envelope = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise SpawnError(f"worker stdout was not JSON: {out.stdout.strip()[:300]}") from exc

    _classify(envelope, out.stderr)  # raises on any infra fault

    raw_result = envelope.get("result", "")
    data: "dict | None" = None
    if spec.json_schema is not None and isinstance(raw_result, str) and raw_result.strip():
        try:
            parsed = json.loads(raw_result)
            data = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            data = None  # a schema was requested but the payload isn't JSON → strike in run_contract

    usage = envelope.get("usage") or {}
    return WorkerResult(
        data=data,
        raw_result=raw_result if isinstance(raw_result, str) else json.dumps(raw_result),
        is_error=bool(envelope.get("is_error")),
        subtype=envelope.get("subtype"),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        usage=usage,
        permission_denials=list(envelope.get("permission_denials") or []),
        session_id=envelope.get("session_id"),
        num_turns=envelope.get("num_turns"),
        returncode=out.returncode,
    )


def _feedback_prompt(base_prompt: str, errors: "list[str]", attempt: int) -> str:
    """Append the concrete schema errors so the next fresh worker can self-correct (strikes)."""
    bullets = "\n".join(f"  - {e}" for e in errors)
    return (
        f"{base_prompt}\n\n"
        f"[driver feedback: attempt {attempt} rejected] Your previous JSON output failed the "
        f"driver's schema re-validation with these errors:\n{bullets}\n"
        f"Return ONLY a single corrected JSON object that satisfies the schema exactly."
    )


def run_contract(
    spec: WorkerSpec,
    schema: dict,
    runner: Runner = subprocess_runner,
    max_strikes: int = 3,
    on_strike: "Callable[[int, list[str], WorkerResult], None] | None" = None,
) -> "tuple[dict, WorkerResult]":
    """Invoke, **re-validate against ``schema``**, and retry-with-feedback up to ``max_strikes``.

    Returns ``(validated_payload, result)`` on success. Raises :class:`ContractError` (a *variant*
    failure) if every attempt stays malformed. Infra errors from :func:`invoke` propagate untouched
   , they are the driver's to route, and a rate-limit mid-strike must not be miscounted as a strike.

    **Spend accounting (never undercount the budget backstop):** every attempt is real spend, so
    cost/tokens accumulate across strikes, summed onto the returned :class:`WorkerResult`, onto a
    raised :class:`ContractError`, and onto any infra error's ``cost_usd`` (its own attempt's cost
    was already attached by :func:`invoke`).
    """
    attempt_spec = replace(spec, json_schema=schema)
    last_errors: "list[str]" = ["no attempts made"]
    spent_usd, spent_tokens = 0.0, {"input_tokens": 0, "output_tokens": 0}

    def _absorb(usage: dict) -> None:
        for k in spent_tokens:
            spent_tokens[k] += int(usage.get(k, 0) or 0)

    for attempt in range(1, max_strikes + 1):
        try:
            result = invoke(attempt_spec, runner)
        except WorkerError as exc:
            exc.cost_usd = getattr(exc, "cost_usd", 0.0) + spent_usd
            exc.tokens = getattr(exc, "tokens", 0) + sum(spent_tokens.values())
            raise
        spent_usd += result.cost_usd
        _absorb(result.usage)
        if result.data is None:
            last_errors = [f"output was not a JSON object: {result.raw_result[:200]}"]
        else:
            last_errors = validate(result.data, schema)
            if not last_errors:
                result.cost_usd = spent_usd
                result.usage = dict(result.usage, **spent_tokens)
                return result.data, result
        if on_strike is not None:
            on_strike(attempt, last_errors, result)
        if attempt < max_strikes:
            attempt_spec = replace(
                attempt_spec, prompt=_feedback_prompt(spec.prompt, last_errors, attempt)
            )
    raise ContractError(
        f"worker output failed schema after {max_strikes} strikes", last_errors, max_strikes,
        cost_usd=spent_usd, tokens=sum(spent_tokens.values()),
    )


# --- settings-file helper (the write_guard hook injection, FLAGS §5/§7) -----------------------


def write_settings_file(path: str, guard_script: "str | None" = None) -> str:
    """Write the ``--settings`` JSON that wires the ``write_guard`` PreToolUse hook, return ``path``.

    Isolating with ``--setting-sources ""`` drops the user's personal hooks, so the driver must
    re-supply the guard here for it to fire (FLAGS §7).
    """
    if guard_script is None:
        guard_script = str(Path(__file__).resolve().parents[1] / "hooks" / "write_guard.py")
    python = shutil.which("python3") or "python3"
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                    "hooks": [{"type": "command", "command": f"{python} {guard_script}"}],
                }
            ]
        }
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    return path


# --- convenience spec builders (encode the two FLAGS.md invocation profiles) ------------------

# Maker: authors a variant; blunt write-allow, path scoped by the hook.
MAKER_TOOLS = ("Read", "Grep", "Glob", "Write", "Edit")
# Reasoners (checker/judge): no tools; reason over driver-inlined context only.
NO_TOOLS: "tuple[str, ...]" = ()


def maker_spec(prompt: str, write_scope: str, settings_path: str, **kw) -> WorkerSpec:
    """A maker worker: acceptEdits (no hang) + hook scope + authoring tools."""
    return WorkerSpec(
        prompt=prompt,
        allowed_tools=MAKER_TOOLS,
        permission_mode="acceptEdits",
        write_scope=write_scope,
        settings_path=settings_path,
        budget_usd=kw.pop("budget_usd", 2.00),
        **kw,
    )


def reasoner_spec(prompt: str, **kw) -> WorkerSpec:
    """A checker/judge worker: no tools, no writes, tight budget."""
    return WorkerSpec(
        prompt=prompt,
        allowed_tools=NO_TOOLS,
        disallowed_tools=(
            "Bash",
            "Task",
            "WebSearch",
            "WebFetch",
            "Write",
            "Edit",
            "NotebookEdit",
            "TodoWrite",
        ),
        permission_mode="default",
        budget_usd=kw.pop("budget_usd", 1.00),
        **kw,
    )


__all__ = [
    "WorkerSpec",
    "WorkerResult",
    "RunOutput",
    "Runner",
    "subprocess_runner",
    "invoke",
    "run_contract",
    "build_argv",
    "build_env",
    "write_settings_file",
    "maker_spec",
    "reasoner_spec",
    "WorkerError",
    "AuthError",
    "RateLimited",
    "BudgetExceeded",
    "WorkerTimeout",
    "SpawnError",
    "ContractError",
]
