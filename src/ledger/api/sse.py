"""Rendering agent events as server-sent events.

The payload models here are the wire contract with the frontend, and
``scripts/gen_types.py`` generates the TypeScript from them so the two cannot
drift silently.

**There is deliberately no ``trace`` event.** The trace is derived on the client
from the ``tool_call_start`` / ``tool_call_end`` pairs it already has. A terminal
trace event would be a second source of truth that can disagree with the first,
and it would arrive *after* the answer -- so a stream that dies mid-answer would
lose the trace at exactly the moment it was most wanted.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ledger.agent import events as ev


class SsePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Monotonic within one response, so a client can detect a gap and a test
    #: can assert ordering.
    seq: int


class MetaPayload(SsePayload):
    conversation_id: str
    message_id: str
    role: str
    tenant_id: int | None
    model_backend: str
    catalog_version: str
    demo_mode: bool


class TokenPayload(SsePayload):
    text: str


class ThinkingPayload(SsePayload):
    text: str


class ToolCallStartPayload(SsePayload):
    call_id: str
    tool: str
    args: dict[str, Any]
    turn: int


class ToolCallEndPayload(SsePayload):
    call_id: str
    tool: str
    ok: bool
    duration_ms: int
    row_count: int | None = None
    truncated: bool = False
    result_id: str | None = None
    sql: str | None = None
    error_code: str | None = None
    message: str | None = None
    notes: list[str] = []


class ChartPayload(SsePayload):
    chart_id: str
    call_id: str
    spec: dict[str, Any]
    columns: list[dict[str, str]]
    rows: list[list[Any]]


class ErrorPayload(SsePayload):
    code: str
    message: str
    fatal: bool


class UsagePayload(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


class DonePayload(SsePayload):
    message_id: str
    stop_reason: str
    turns: int
    total_duration_ms: int
    usage: UsagePayload


EventName = Literal[
    "meta", "token", "thinking", "tool_call_start", "tool_call_end", "chart", "error", "done"
]

_NAMES: dict[type, EventName] = {
    ev.Meta: "meta",
    ev.Token: "token",
    ev.Thinking: "thinking",
    ev.ToolCallStart: "tool_call_start",
    ev.ToolCallEnd: "tool_call_end",
    ev.Chart: "chart",
    ev.Error: "error",
    ev.Done: "done",
}

_PAYLOADS: dict[type, type[SsePayload]] = {
    ev.Meta: MetaPayload,
    ev.Token: TokenPayload,
    ev.Thinking: ThinkingPayload,
    ev.ToolCallStart: ToolCallStartPayload,
    ev.ToolCallEnd: ToolCallEndPayload,
    ev.Chart: ChartPayload,
    ev.Error: ErrorPayload,
    ev.Done: DonePayload,
}

ALL_PAYLOADS: tuple[type[SsePayload], ...] = (
    MetaPayload,
    TokenPayload,
    ThinkingPayload,
    ToolCallStartPayload,
    ToolCallEndPayload,
    ChartPayload,
    ErrorPayload,
    DonePayload,
)


def event_name(event: ev.AgentEvent) -> EventName:
    return _NAMES[type(event)]


def to_payload(event: ev.AgentEvent, seq: int) -> SsePayload:
    model = _PAYLOADS[type(event)]
    data = asdict(event)
    if isinstance(event, ev.Done):
        data["usage"] = asdict(event.usage)
    return model.model_validate({"seq": seq, **data})


def format_frame(name: str, payload: SsePayload) -> str:
    """One SSE frame. Data is always a single line, so no re-joining is needed."""
    body = json.dumps(payload.model_dump(mode="json"), separators=(",", ":"), default=str)
    return f"event: {name}\ndata: {body}\n\n"


def comment(text: str) -> str:
    """A keepalive. Ignored by clients, but keeps proxies from timing out."""
    return f": {text}\n\n"
