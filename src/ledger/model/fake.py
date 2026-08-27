"""A scripted model, and the reason the whole project runs with no API key.

**The fake replaces only the model.** Its tool calls go through the real
registry, real validation, real RBAC scoping, real DuckDB, and the real Kafka
audit path -- so a test exercises the entire pipeline minus one component.

Responders are functions of the conversation so far, not a flat list of turns.
A flat list cannot express the single most valuable test in the suite -- the
model hallucinating a column, reading the typed error, and retrying with the
suggested name -- because turn two has to depend on what turn one was told.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from ledger.model.protocol import (
    AssistantTurn,
    TextDelta,
    ThinkingDelta,
    ToolUseReady,
    TurnEvent,
    Usage,
)

#: Roughly a word at a time, which is what streaming looks like to a reader.
CHUNK_CHARS = 6


@dataclass(slots=True)
class ScriptedTurn:
    """What the fake should say and do for one turn."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def stop_reason(self) -> str:
        return "tool_use" if self.tool_calls else "end_turn"


Responder = Callable[[list[dict[str, Any]]], ScriptedTurn]


def last_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent tool result, parsed, so a responder can react to it."""
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                raw = block.get("content")
                if isinstance(raw, str):
                    try:
                        parsed: dict[str, Any] = json.loads(raw)
                    except json.JSONDecodeError:
                        return {"ok": False, "message": raw}
                    return parsed
                if isinstance(raw, dict):
                    return raw
    return None


def sequence(turns: list[ScriptedTurn]) -> Responder:
    """Play turns in order, repeating the last one if the loop runs on."""
    remaining = list(turns)

    def respond(_: list[dict[str, Any]]) -> ScriptedTurn:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0] if remaining else ScriptedTurn(text="Done.")

    return respond


def _chunks(text: str) -> list[str]:
    return [text[i : i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)] or []


class _FakeStream:
    def __init__(self, turn: ScriptedTurn, index: int, delay_ms: int = 0) -> None:
        self._turn = turn
        self._index = index
        self._delay_s = delay_ms / 1000

    async def __aiter__(self) -> AsyncIterator[TurnEvent]:
        for chunk in _chunks(self._turn.thinking):
            yield ThinkingDelta(chunk)
        for chunk in _chunks(self._turn.text):
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            yield TextDelta(chunk)
        for position, (name, arguments) in enumerate(self._turn.tool_calls):
            yield ToolUseReady(
                id=f"toolu_fake_{self._index}_{position}", name=name, input=arguments
            )

    async def final_message(self) -> AssistantTurn:
        content: list[dict[str, Any]] = []
        if self._turn.thinking:
            content.append({"type": "thinking", "thinking": self._turn.thinking})
        if self._turn.text:
            content.append({"type": "text", "text": self._turn.text})
        for position, (name, arguments) in enumerate(self._turn.tool_calls):
            content.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_fake_{self._index}_{position}",
                    "name": name,
                    "input": arguments,
                }
            )
        return AssistantTurn(
            content=content,
            stop_reason=self._turn.stop_reason,
            usage=Usage(input_tokens=0, output_tokens=len(self._turn.text)),
        )


class FakeModelClient:
    """Deterministic, offline, and free."""

    name = "fake"

    def __init__(self, responder: Responder, *, token_delay_ms: int = 0) -> None:
        self._responder = responder
        self._turn_index = 0
        self._token_delay_ms = token_delay_ms

    @asynccontextmanager
    async def stream_turn(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[_FakeStream]:
        turn = self._responder(messages)
        stream = _FakeStream(turn, self._turn_index, self._token_delay_ms)
        self._turn_index += 1
        yield stream
