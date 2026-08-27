"""The seam between the agent loop and whatever is generating tokens.

``AssistantTurn.content`` carries **the Anthropic wire shape** -- a list of
``{"type": "text" | "thinking" | "tool_use", ...}`` dicts -- rather than a
parallel type system of our own. Two reasons, and the second is the one that
matters:

* The real adapter becomes nearly pass-through.
* The agent loop appends ``final.content`` to the conversation *verbatim*. If
  the fake produced a different shape, it would pass tests the real client
  fails, which is the classic way a test double rots into a lie.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class TextDelta:
    text: str


@dataclass(slots=True)
class ThinkingDelta:
    text: str


@dataclass(slots=True)
class ToolUseReady:
    """Emitted once a tool call's arguments are fully parsed.

    Fired at ``content_block_stop`` rather than on the first delta, so a status
    row appears exactly when work is about to start rather than while the model
    is still writing the arguments.
    """

    id: str
    name: str
    input: dict[str, Any]


TurnEvent = TextDelta | ThinkingDelta | ToolUseReady


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(slots=True)
class AssistantTurn:
    """One assistant turn, in the shape the conversation history needs."""

    content: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)

    def tool_uses(self) -> list[dict[str, Any]]:
        return [block for block in self.content if block.get("type") == "tool_use"]

    def text(self) -> str:
        return "".join(
            block.get("text", "") for block in self.content if block.get("type") == "text"
        )


class TurnStream(Protocol):
    """One streamed assistant turn."""

    def __aiter__(self) -> AsyncIterator[TurnEvent]: ...

    async def final_message(self) -> AssistantTurn: ...


class ModelClient(Protocol):
    """Anything that can produce an assistant turn."""

    name: str

    def stream_turn(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AbstractAsyncContextManager[TurnStream]: ...
