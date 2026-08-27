"""The governance event schema.

Every tool call and every access denial becomes an event on a Kafka topic
before it becomes an answer. The trace panel is a view over this log, not a
debug print, and that is only meaningfully true because the events are
published and materialised by a genuinely separate process.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ledger.security.principal import Channel, Role

#: Bumped when the envelope changes shape, so a consumer can reject or migrate.
EVENT_SCHEMA_VERSION = 1


class EventType(StrEnum):
    #: Published *before* the query runs. If this fails, the query does not run.
    TOOL_CALL_REQUESTED = "tool_call_requested"
    #: Published after, carrying the outcome.
    TOOL_CALL_COMPLETED = "tool_call_completed"
    #: A caller named a column their role cannot see.
    ACCESS_DENIED = "access_denied"
    #: The metadata pipeline is itself on the spine.
    CATALOG_BUILT = "catalog_built"


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ERROR = "error"
    CANCELLED = "cancelled"


def new_event_id() -> str:
    """A time-ordered id that doubles as the consumer's dedupe key.

    uuid7 is sortable by creation time, so the audit store stays roughly ordered
    on disk without a separate sequence.
    """
    # uuid.uuid7 landed in Python 3.14; typeshed has not caught up yet.
    return str(uuid.uuid7())  # type: ignore[attr-defined,unused-ignore]


class PrincipalRef(BaseModel):
    """Who acted. A snapshot, so a later role change cannot rewrite history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    role: Role
    tenant_id: int | None = None


class GovernanceEvent(BaseModel):
    """One durable record of something a caller did or was refused."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: str = Field(default_factory=new_event_id)
    event_type: EventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    channel: Channel = Channel.HTTP
    principal: PrincipalRef

    conversation_id: str | None = None
    message_id: str | None = None
    #: Links a `requested` event to its `completed` event.
    call_id: str | None = None

    tool: str | None = None
    #: The *validated* arguments, never raw model output. Raw output can carry
    #: injected text; validated args are what actually executed.
    args: dict[str, Any] | None = None

    outcome: Outcome | None = None
    error_code: str | None = None
    row_count: int | None = None
    duration_ms: int | None = None
    result_id: str | None = None

    #: A hash of the SQL template, not the SQL. Proves what shape of query ran
    #: without putting filter values onto the topic.
    sql_fingerprint: str | None = None

    #: Only on ACCESS_DENIED, where the attempted name is the entire point.
    attempted_columns: list[str] | None = None

    catalog_version: str | None = None

    def topic_key(self) -> str:
        """Partition key. Keying on the conversation keeps its events ordered."""
        return self.conversation_id or self.principal.subject


class AuditTopics(BaseModel):
    """Topic names, resolved from settings so tests can namespace them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_calls: str
    access_denied: str

    def for_event(self, event: GovernanceEvent) -> str:
        if event.event_type is EventType.ACCESS_DENIED:
            return self.access_denied
        return self.tool_calls

    def all(self) -> tuple[str, ...]:
        return (self.tool_calls, self.access_denied)


def requested(
    *,
    principal: PrincipalRef,
    channel: Channel,
    tool: str,
    args: dict[str, Any],
    call_id: str,
    conversation_id: str | None = None,
    message_id: str | None = None,
    catalog_version: str | None = None,
) -> GovernanceEvent:
    return GovernanceEvent(
        event_type=EventType.TOOL_CALL_REQUESTED,
        principal=principal,
        channel=channel,
        tool=tool,
        args=args,
        call_id=call_id,
        conversation_id=conversation_id,
        message_id=message_id,
        catalog_version=catalog_version,
    )


def completed(
    request: GovernanceEvent,
    *,
    outcome: Outcome,
    duration_ms: int,
    row_count: int | None = None,
    error_code: str | None = None,
    result_id: str | None = None,
    sql_fingerprint: str | None = None,
) -> GovernanceEvent:
    """Build the outcome event that closes out ``request``."""
    return GovernanceEvent(
        event_type=EventType.TOOL_CALL_COMPLETED,
        principal=request.principal,
        channel=request.channel,
        tool=request.tool,
        call_id=request.call_id,
        conversation_id=request.conversation_id,
        message_id=request.message_id,
        catalog_version=request.catalog_version,
        outcome=outcome,
        duration_ms=duration_ms,
        row_count=row_count,
        error_code=error_code,
        result_id=result_id,
        sql_fingerprint=sql_fingerprint,
    )


def access_denied(
    *,
    principal: PrincipalRef,
    channel: Channel,
    tool: str,
    attempted_columns: list[str],
    call_id: str,
    conversation_id: str | None = None,
) -> GovernanceEvent:
    """Record what the caller reached for.

    The caller is told the column does not exist; the audit log knows better.
    That asymmetry is the point -- it is what makes the denial non-probeable
    without making it invisible to an operator.
    """
    return GovernanceEvent(
        event_type=EventType.ACCESS_DENIED,
        principal=principal,
        channel=channel,
        tool=tool,
        call_id=call_id,
        conversation_id=conversation_id,
        outcome=Outcome.DENY,
        attempted_columns=sorted(attempted_columns),
    )


AnyEvent = GovernanceEvent
EventLiteral = Literal[
    "tool_call_requested", "tool_call_completed", "access_denied", "catalog_built"
]
