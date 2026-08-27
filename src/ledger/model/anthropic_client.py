"""The real model client.

Written now and type-checked on every CI run, but only exercised under
``-m ai_live``. The translation logic -- the part that is actually ours -- is
covered offline by replaying recorded SDK event fixtures.

Request parameters worth not "fixing" later: ``claude-opus-5`` rejects
``temperature``, ``top_p``, ``budget_tokens``, and assistant prefill with a 400.
Thinking display is set explicitly because the default is ``omitted``, which
makes the UI look stalled while the model reasons.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, TextBlockParam, ToolParam
from anthropic.types.output_config_param import OutputConfigParam

from ledger.config import Settings
from ledger.model.protocol import (
    AssistantTurn,
    TextDelta,
    ThinkingDelta,
    ToolUseReady,
    TurnEvent,
    Usage,
)

MAX_TOKENS = 8_000


class _AnthropicStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[TurnEvent]:
        async for event in self._stream:
            translated = translate_event(event)
            if translated is not None:
                yield translated

    async def final_message(self) -> AssistantTurn:
        message = await self._stream.get_final_message()
        return to_assistant_turn(message)


def translate_event(event: Any) -> TurnEvent | None:
    """Map one SDK stream event onto our own.

    Kept as a free function so it can be exercised against recorded fixtures
    with no API key -- this is the only part of the adapter that is our logic
    rather than the SDK's.
    """
    kind = getattr(event, "type", None)

    if kind == "content_block_delta":
        delta = event.delta
        delta_type = getattr(delta, "type", None)
        if delta_type == "text_delta":
            return TextDelta(delta.text)
        if delta_type == "thinking_delta":
            return ThinkingDelta(delta.thinking)
        return None

    if kind == "content_block_stop":
        block = getattr(event, "content_block", None)
        if block is not None and getattr(block, "type", None) == "tool_use":
            # Arguments are complete here, which is when work is about to start.
            return ToolUseReady(id=block.id, name=block.name, input=dict(block.input or {}))
    return None


def to_assistant_turn(message: Any) -> AssistantTurn:
    """Convert a final SDK message, preserving every content block verbatim.

    Thinking blocks must be echoed back unchanged on the next request, and
    reconstructing text-only content is the classic bug that degrades tool use
    several turns in without raising anything.
    """
    content: list[dict[str, Any]] = []
    for block in message.content:
        if hasattr(block, "model_dump"):
            content.append(block.model_dump(exclude_none=True))
        else:  # pragma: no cover - defensive
            content.append(dict(block))

    usage = getattr(message, "usage", None)
    return AssistantTurn(
        content=content,
        stop_reason=getattr(message, "stop_reason", "end_turn") or "end_turn",
        usage=Usage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
    )


class AnthropicModelClient:
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    @asynccontextmanager
    async def stream_turn(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[_AnthropicStream]:
        # The SDK's parameter types are TypedDicts. Our dicts match them by
        # construction -- the tool schemas come from `registry.schemas_for`, and
        # the history is content blocks the SDK itself produced -- so a cast is
        # honest here in a way that loosening the annotations upstream would not
        # be: it keeps the strict types everywhere they can actually catch us.
        async with self._client.messages.stream(
            model=self._settings.anthropic_model,
            max_tokens=MAX_TOKENS,
            system=cast(Iterable[TextBlockParam], system),
            messages=cast(Iterable[MessageParam], messages),
            tools=cast(Iterable[ToolParam], tools),
            # Adaptive is the only supported mode on this model family, and the
            # display must be requested or the stream shows a long silence.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config=cast(OutputConfigParam, {"effort": self._settings.anthropic_effort}),
        ) as stream:
            yield _AnthropicStream(stream)
