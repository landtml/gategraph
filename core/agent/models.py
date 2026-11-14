"""Model port: one small interface for every model the agent layer calls.

Three implementations are planned (section 1.11): ``api`` (tool-calling chat models,
the only port that drives the tool loop), ``claude_cli`` (subprocess, no tools, for
verifiers), and ``fake`` (this module's FakeModel, scripted responses for tests).

This module imports only the standard library and core.kernel.types, so it loads
with the system python3. LangChain is imported lazily, inside ``chat_model()``.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Protocol, runtime_checkable

from core.kernel.types import Spend


@dataclasses.dataclass(frozen=True)
class ModelResult:
    """Result of ``complete``. ``data`` is the parsed structured value, or None for text."""
    text: str
    data: Any
    spend: Spend


@dataclasses.dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    id: str


@dataclasses.dataclass(frozen=True)
class ChatTurn:
    """Result of ``FakeModel.chat``: text and/or the tool calls the model asked for."""
    text: str
    tool_calls: list
    spend: Spend


@runtime_checkable
class ModelPort(Protocol):
    @property
    def supports_tools(self) -> bool:
        """True when ``chat_model()`` can drive the tool loop."""
        ...

    def complete(self, prompt: str, schema: Any = None) -> ModelResult:
        """One prompt in, one result out. ``schema`` requests structured data."""
        ...

    def chat_model(self) -> Any:
        """A LangChain chat model for the agent loop. Imports LangChain lazily."""
        ...


# --------------------------------------------------------------------------
# FakeModel: scripted, offline, deterministic
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Structured:
    """A scripted structured response. ``complete`` returns ``data``; ``chat`` returns its JSON text."""
    data: Any


class ScriptError(RuntimeError):
    """A scripted step does not fit the call made (for example, tool calls asked of ``complete``)."""


class ScriptExhausted(RuntimeError):
    """The test asked for more responses than the script holds."""


def _spend() -> Spend:
    return Spend(model_calls=1)


class FakeModel:
    """Replays a script. Steps, consumed in order:

    * ``str``: a text response;
    * ``Structured(data)``: a structured response;
    * ``list[ToolCall]``: tool calls, returned by ``chat`` only.

    Every prompt (``complete``) or message list (``chat``, or the messages a LangChain
    model receives) is appended to ``calls``. ``schemas`` holds the schema passed with
    each ``complete`` call, aligned with ``calls``. Never touches the network.
    """

    supports_tools = True

    def __init__(self, script):
        self._steps = list(script)
        self.calls: list = []
        self.schemas: list = []

    def _take(self, tools_ok: bool):
        if not self._steps:
            raise ScriptExhausted("FakeModel script is exhausted: the code under test "
                                  "asked for more model responses than the test scripted")
        step = self._steps[0]
        if isinstance(step, list) and not tools_ok:
            raise ScriptError("the next scripted step is a list of tool calls, which "
                              "complete() cannot return; use chat() for tool calls")
        return self._steps.pop(0)

    def complete(self, prompt: str, schema: Any = None) -> ModelResult:
        self.calls.append(prompt)
        self.schemas.append(schema)
        step = self._take(tools_ok=False)
        if isinstance(step, Structured):
            return ModelResult(text=json.dumps(step.data, sort_keys=True),
                               data=step.data, spend=_spend())
        return ModelResult(text=step, data=None, spend=_spend())

    def chat(self, messages) -> ChatTurn:
        self.calls.append(messages)
        step = self._take(tools_ok=True)
        if isinstance(step, list):
            return ChatTurn(text="", tool_calls=list(step), spend=_spend())
        if isinstance(step, Structured):
            return ChatTurn(text=json.dumps(step.data, sort_keys=True),
                            tool_calls=[], spend=_spend())
        return ChatTurn(text=step, tool_calls=[], spend=_spend())

    def chat_model(self) -> Any:
        """A LangChain BaseChatModel that replays this script through ``chat``."""
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        owner = self

        class _ScriptedChatModel(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "fake-scripted"

            def bind_tools(self, tools, **kwargs):
                return self  # the script decides which tools to call

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                turn = owner.chat(messages)
                message = AIMessage(content=turn.text, tool_calls=[
                    {"name": call.name, "args": call.args, "id": call.id}
                    for call in turn.tool_calls])
                return ChatResult(generations=[ChatGeneration(message=message)])

        return _ScriptedChatModel()
