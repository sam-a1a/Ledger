"""What the agent loop emits.

These become SSE frames, but the loop does not know that: it writes into a
queue, and the transport decides how to render them. That separation is what
lets the same loop drive the HTTP endpoint, the tests, and the golden suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ledger.model.protocol import Usage


@dataclass(slots=True)
class Meta:
    conversation_id: str
    message_id: str
    role: str
    tenant_id: int | None
    model_backend: str
    catalog_version: str
    demo_mode: bool


@dataclass(slots=True)
class Token:
    text: str


@dataclass(slots=True)
class Thinking:
    text: str


@dataclass(slots=True)
class ToolCallStart:
    call_id: str
    tool: str
    args: dict[str, Any]
    turn: int


@dataclass(slots=True)
class ToolCallEnd:
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
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Chart:
    chart_id: str
    call_id: str
    spec: dict[str, Any]
    columns: list[dict[str, str]]
    rows: list[list[Any]]


@dataclass(slots=True)
class Error:
    code: str
    message: str
    fatal: bool = True


@dataclass(slots=True)
class Done:
    message_id: str
    stop_reason: Literal["end_turn", "max_turns", "error", "cancelled"]
    turns: int
    total_duration_ms: int
    usage: Usage


AgentEvent = Meta | Token | Thinking | ToolCallStart | ToolCallEnd | Chart | Error | Done
