"""``api``: a ModelPort over LangChain's ``init_chat_model``.

This is the only port that drives the tool loop: ``chat_model()`` returns the underlying
LangChain chat model for the agent loop. LangChain is imported lazily, inside
``chat_model()``, so importing this module loads nothing heavy.

``factory`` is injectable (tests pass FakeModel's scripted chat model). It receives the
model name and the keyword arguments and returns a chat model.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from core.agent.models import ModelResult
from core.agent.ports import ContractFault
from core.kernel.types import Spend


def _default_factory(model: str, **kwargs: Any) -> Any:
    from langchain.chat_models import init_chat_model  # lazy: heavy import
    return init_chat_model(model, **kwargs)


def _content_text(content: Any) -> str:
    """LangChain content is a string or a list of blocks. Keep the text parts."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _spend(message: Any) -> Spend:
    usage = getattr(message, "usage_metadata", None) or {}
    tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
    # No price table here, so usd stays 0.0. The tokens are real.
    return Spend(usd=0.0, tokens=tokens, model_calls=1)


class ApiPort:
    """A tool-calling ModelPort. ``supports_tools`` is True."""

    def __init__(self, model: str, *, model_provider: Optional[str] = None,
                 factory: Optional[Callable[..., Any]] = None, **model_kwargs: Any) -> None:
        self._model = model
        self._factory = factory or _default_factory
        self._kwargs = dict(model_kwargs)
        if model_provider is not None:
            self._kwargs["model_provider"] = model_provider
        self._chat: Any = None

    @property
    def supports_tools(self) -> bool:
        return True

    def chat_model(self) -> Any:
        if self._chat is None:
            self._chat = self._factory(self._model, **self._kwargs)
        return self._chat

    def complete(self, prompt: str, schema: Any = None) -> ModelResult:
        model = self.chat_model()
        if schema is None:
            message = model.invoke(prompt)
            return ModelResult(text=_content_text(message.content), data=None,
                               spend=_spend(message))
        structured = model.with_structured_output(schema, include_raw=True)
        out = structured.invoke(prompt)
        if out.get("parsing_error") is not None or out.get("parsed") is None:
            raise ContractFault(f"structured output did not parse: {out.get('parsing_error')}",
                                [str(out.get("parsing_error"))], spend=_spend(out.get("raw")))
        raw = out["raw"]
        return ModelResult(text=_content_text(raw.content), data=out["parsed"],
                           spend=_spend(raw))
