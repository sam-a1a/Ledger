"""Test doubles.

This module lives under ``tests/`` and is never importable from ``ledger``.
That is the point: the application ships exactly one ``AuditPublisher``, and
``tests/governance/test_no_escape_hatch.py`` fails if a second one ever appears
in ``src``. A recording double here lets the tool suite assert on published
events without a broker, without reintroducing the escape hatch the hard
dependency was chosen to avoid.
"""

from __future__ import annotations

import contextlib

from ledger.errors import EventPublishError
from ledger.governance.events import EventType, GovernanceEvent


class RecordingPublisher:
    """Captures events in memory. Optionally fails, to exercise the closed path."""

    def __init__(self, *, fail_on: set[EventType] | None = None) -> None:
        self.events: list[GovernanceEvent] = []
        self.fail_on = fail_on or set()

    async def publish(self, event: GovernanceEvent) -> None:
        if event.event_type in self.fail_on:
            raise EventPublishError(f"simulated outage for {event.event_type}")
        self.events.append(event)

    async def publish_best_effort(self, event: GovernanceEvent) -> None:
        with contextlib.suppress(EventPublishError):
            await self.publish(event)

    # --- assertions -------------------------------------------------------

    def of_type(self, event_type: EventType) -> list[GovernanceEvent]:
        return [e for e in self.events if e.event_type is event_type]

    @property
    def requested(self) -> list[GovernanceEvent]:
        return self.of_type(EventType.TOOL_CALL_REQUESTED)

    @property
    def completed(self) -> list[GovernanceEvent]:
        return self.of_type(EventType.TOOL_CALL_COMPLETED)

    @property
    def denials(self) -> list[GovernanceEvent]:
        return self.of_type(EventType.ACCESS_DENIED)

    def denied_columns(self) -> set[str]:
        return {c for e in self.denials for c in (e.attempted_columns or [])}
