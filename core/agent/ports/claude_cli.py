"""``claude_cli``: a ModelPort that runs ``claude -p`` as a subprocess, tool-less.

Ported from core/loop/workers.py (the legacy ``claude -p`` seam). It serves verifiers,
checkers and judges: one prompt in, one validated result out. It has no tools and no
write guard, so it never drives the agent's tool loop (``supports_tools`` is False).

Behaviour kept from the seam:
- Subscription auth. ``build_env`` removes ``ANTHROPIC_API_KEY`` and
  ``ANTHROPIC_AUTH_TOKEN`` from the child environment, so a worker cannot silently
  switch to metered API billing.
- Bounded by a subprocess timeout and ``--max-budget-usd``.
- Infra faults (auth, rate limit, budget, spawn, timeout) are typed exceptions and are
  never counted as a variant strike. Only a malformed or schema-violating payload is
  a strike, and it is retried with the schema errors as feedback.
- The runner is injected, so tests need neither a subprocess nor a network.

Fixes in this port: B23 (a non-string or null ``result`` is coerced, and a non-mapping
envelope is a typed fault), B24 (control-plane limit banners are read from stderr always,
and from the result only on an errored envelope), B25 (the child is run from an argv
list, never a shell string, and rendered commands are shell-quoted).
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core.agent.ports import (
    AuthFault, BudgetFault, ContractFault, ModelFault, RateLimitFault, SpawnFault,
    TimeoutFault,
)
from core.agent.models import ModelResult
from core.kernel.types import Spend

# Environment variables that would move a worker onto metered API billing.
SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# A tool-less worker: every built-in tool is denied, so the reply is text or JSON only.
DENIED_TOOLS = ("Bash", "Task", "WebSearch", "WebFetch", "Write", "Edit",
                "NotebookEdit", "TodoWrite")

# Heuristic markers. The only signal proven live is the budget subtype.
_AUTH_MARKERS = ("invalid api key", "authentication", "oauth", "unauthorized",
                 "please run /login", "login")
_RATE_MARKERS = ("rate limit", "rate_limit", "overloaded", "429", "usage limit",
                 "too many requests", "session limit", "session_limit")
# Unambiguous control-plane banners. Read from stderr always, and from an errored result.
_HARD_LIMIT_MARKERS = ("hit your session limit", "session limit ·", "usage limit reached",
                       "5-hour limit")
_BUDGET_SUBTYPE = "error_max_budget_usd"
_FEEDBACK_HEADER = "[driver feedback: attempt {attempt} rejected]"


@dataclass
class RunOutput:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list, Mapping[str, str], int], RunOutput]


def render_command(argv) -> str:
    """A shell-quoted, copy-pasteable rendering of ``argv``. Every element is quoted (B25)."""
    return shlex.join(str(part) for part in argv)


def subprocess_runner(argv: list, env: Mapping[str, str], timeout_s: int) -> RunOutput:
    """Spawn ``argv`` (a list, never a shell) with stdin closed and a wall-clock timeout.

    The child gets its own session, so a timeout kills everything it spawned.
    """
    try:
        proc = subprocess.Popen(
            list(argv), env=dict(env),
            stdin=subprocess.DEVNULL,  # never let ``-p`` wait on piped stdin
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except OSError as exc:
        raise SpawnFault(f"could not launch `claude` ({exc.__class__.__name__}); "
                         f"command: {render_command(argv)}") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()
        raise TimeoutFault(f"worker exceeded {timeout_s}s") from exc
    return RunOutput(proc.returncode, stdout or "", stderr or "")


def build_env(base: "Mapping[str, str] | None" = None) -> dict:
    """A copy of ``base`` (default: the process environment) with the API key variables removed."""
    env = dict(os.environ if base is None else base)
    for name in SCRUBBED_ENV:
        env.pop(name, None)
    return env


def build_argv(prompt: str, *, model: str, json_schema: "dict | None",
               budget_usd: float, extra_args=(), binary: str = "claude") -> list:
    """The tool-less ``claude -p`` argv (the reasoner profile).

    ``binary`` is the executable (argv[0]); the default is the ``claude`` on PATH.
    """
    argv = [str(binary), "-p", prompt, "--model", model, "--output-format", "json"]
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema, sort_keys=True)]
    argv += ["--permission-mode", "default", "--setting-sources", ""]
    argv += ["--disallowedTools", *DENIED_TOOLS]
    argv += ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
             "--max-budget-usd", f"{budget_usd:.2f}"]
    argv += [str(a) for a in extra_args]
    return argv


# --- minimal JSON-schema check (the subset the contracts use) -------------------------------

_JSON_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
               "null": type(None)}


def _type_ok(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES[name])


def validate_schema(value: Any, schema: Mapping, path: str = "$") -> list:
    """Schema errors for ``value``. An empty list means the value conforms."""
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_type_ok(value, n) for n in names):
            return [f"{path}: expected {'/'.join(names)}, got {type(value).__name__}"]
    errors: list = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']!r}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                errors += validate_schema(item, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{key}'")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate_schema(item, schema["items"], f"{path}[{i}]")
    return errors


# --- envelope handling ----------------------------------------------------------------------


def _result_text(value: Any) -> str:
    """The envelope ``result`` as text. A null, object or number is coerced, never a crash (B23)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _envelope_cost(envelope: dict) -> "tuple[float, int]":
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    try:
        usd = float(envelope.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        usd = 0.0
    tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens"))
    return usd, tokens


def _parse_envelope(out: RunOutput) -> dict:
    if not out.stdout.strip():
        raise SpawnFault(f"worker produced no stdout (rc={out.returncode}): "
                         f"{out.stderr.strip()[:300]}")
    try:
        envelope = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise SpawnFault(f"worker stdout was not JSON: {out.stdout.strip()[:300]}") from exc
    if not isinstance(envelope, dict):
        raise SpawnFault(f"worker envelope is {type(envelope).__name__}, not an object")
    return envelope


def _fault(cls: type, message: str, envelope: dict) -> None:
    """Raise ``cls`` carrying the cost this attempt was billed (a failed attempt is still spend)."""
    exc = cls(message)
    exc.usd, exc.tokens = _envelope_cost(envelope)
    raise exc


def _classify(envelope: dict, stderr: str) -> None:
    """Raise a typed fault if the envelope or stderr signals one. Return if the attempt is clean."""
    subtype = envelope.get("subtype")
    if subtype == _BUDGET_SUBTYPE:
        _fault(BudgetFault, "worker hit --max-budget-usd", envelope)
    is_error = bool(envelope.get("is_error"))
    text = _result_text(envelope.get("result"))
    text_l = text.lower()
    err_l = (stderr or "").lower()
    # B24: a banner in an artifact is not a limit. The result counts only on an errored envelope.
    if any(m in err_l for m in _HARD_LIMIT_MARKERS) or (
            is_error and any(m in text_l for m in _HARD_LIMIT_MARKERS)):
        _fault(RateLimitFault, f"worker rate-limited: {text[:200]}", envelope)
    if not is_error:
        return
    haystack = f"{err_l}\n{text_l}"
    if any(m in haystack for m in _AUTH_MARKERS):
        _fault(AuthFault, f"worker auth failure: {text[:200]}", envelope)
    if any(m in haystack for m in _RATE_MARKERS):
        _fault(RateLimitFault, f"worker rate-limited: {text[:200]}", envelope)
    _fault(SpawnFault, f"worker errored (subtype={subtype}): {text[:200]}", envelope)


def _feedback_prompt(base_prompt: str, errors: list, attempt: int) -> str:
    bullets = "\n".join(f"  - {e}" for e in errors)
    return (f"{base_prompt}\n\n{_FEEDBACK_HEADER.format(attempt=attempt)} Your previous JSON "
            f"output failed the schema re-validation with these errors:\n{bullets}\n"
            "Return ONLY a single corrected JSON object that satisfies the schema exactly.")


class _Ledger:
    """Accumulates real spend across every attempt, including the ones that failed."""

    def __init__(self) -> None:
        self.usd = 0.0
        self.tokens = 0
        self.model_calls = 0

    def add(self, usd: float, tokens: int) -> None:
        self.usd += usd
        self.tokens += tokens

    def spend(self) -> Spend:
        return Spend(usd=self.usd, tokens=self.tokens, model_calls=self.model_calls)


class ClaudeCliPort:
    """A tool-less ModelPort over ``claude -p``.

    ``complete(prompt, schema)`` makes up to ``max_strikes`` attempts. Each failed schema
    check is fed back as a repair prompt. Spend is summed over all attempts.
    """

    def __init__(self, *, model: str = "opus", runner: Runner = subprocess_runner,
                 budget_usd: float = 1.00, timeout_s: int = 1200, max_strikes: int = 3,
                 base_env: "Mapping[str, str] | None" = None, extra_args=(),
                 binary: str = "claude") -> None:
        self._model = model
        self._binary = str(binary)
        self._runner = runner
        self._budget_usd = budget_usd
        self._timeout_s = timeout_s
        self._max_strikes = max_strikes
        self._base_env = base_env
        self._extra_args = tuple(extra_args)

    @property
    def supports_tools(self) -> bool:
        return False

    def chat_model(self) -> Any:
        raise NotImplementedError("claude_cli is tool-less and has no chat model; "
                                  "use the api port to drive the tool loop")

    def complete(self, prompt: str, schema: Any = None) -> ModelResult:
        ledger = _Ledger()
        attempt_prompt = prompt
        errors: list = []
        for attempt in range(1, self._max_strikes + 1):
            text = self._call(attempt_prompt, schema, ledger)
            if schema is None:
                return ModelResult(text=text, data=None, spend=ledger.spend())
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                errors = [f"output was not JSON: {text[:200]}"]
            else:
                errors = validate_schema(data, schema)
                if not errors:
                    return ModelResult(text=text, data=data, spend=ledger.spend())
            if attempt < self._max_strikes:
                attempt_prompt = _feedback_prompt(prompt, errors, attempt)
        raise ContractFault(
            f"worker output failed schema after {self._max_strikes} strikes", errors,
            spend=ledger.spend())

    def _call(self, prompt: str, schema: Any, ledger: _Ledger) -> str:
        """One CLI attempt. Returns the envelope ``result`` as text, or raises a ModelFault."""
        argv = build_argv(prompt, model=self._model, json_schema=schema,
                          budget_usd=self._budget_usd, extra_args=self._extra_args,
                          binary=self._binary)
        env = build_env(self._base_env)
        ledger.model_calls += 1
        try:
            out = self._runner(argv, env, self._timeout_s)
            envelope = _parse_envelope(out)
            _classify(envelope, out.stderr)
        except OSError as exc:
            raise SpawnFault(f"could not run worker ({exc.__class__.__name__}); "
                             f"command: {render_command(argv)}") from exc
        except ModelFault as exc:
            ledger.add(exc.usd, exc.tokens)
            exc.spend = ledger.spend()
            raise
        ledger.add(*_envelope_cost(envelope))
        return _result_text(envelope.get("result"))
