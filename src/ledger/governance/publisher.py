"""Publishing governance events to Kafka.

**There is exactly one implementation of :class:`AuditPublisher` in ``src``.**
No null publisher, no disabled mode, no environment flag that turns auditing
off -- ``tests/governance/test_no_escape_hatch.py`` asserts this, so the
decision is enforced rather than merely intended. Tests that need to inspect
published events use a recording double that lives under ``tests/`` and is not
importable from the application.

The delivery contract is two-phase and fails closed:

* ``tool_call_requested`` is awaited with ``acks=all`` **before** the query runs.
  If it cannot be published, the tool call is refused and the query never
  executes. There is no un-audited data access -- not merely no un-audited
  *result*.
* ``tool_call_completed`` is published after, and journalled if it fails. The
  consumer independently flags a request with no completion as ``unknown``, so
  a gap is visible rather than silent.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from ledger.config import Settings
from ledger.errors import EventPublishError
from ledger.governance.events import AuditTopics, GovernanceEvent
from ledger.governance.journal import EventJournal
from ledger.logging import get_logger

log = get_logger(__name__)


@runtime_checkable
class AuditPublisher(Protocol):
    """Anything that can durably record a governance event."""

    async def publish(self, event: GovernanceEvent) -> None:
        """Publish and wait for acknowledgement. Raise on failure."""
        ...

    async def publish_best_effort(self, event: GovernanceEvent) -> None:
        """Publish; on failure journal it rather than raising."""
        ...


class KafkaAuditPublisher:
    """The only production publisher."""

    def __init__(
        self,
        producer: AIOKafkaProducer,
        topics: AuditTopics,
        journal: EventJournal,
        *,
        timeout_s: float = 5.0,
    ) -> None:
        self._producer = producer
        self._topics = topics
        self._journal = journal
        self._timeout_s = timeout_s

    @classmethod
    def build_producer(cls, settings: Settings) -> AIOKafkaProducer:
        return AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            client_id=settings.kafka_client_id,
            # acks=all with idempotence is the correct pairing for an audit log:
            # no silent loss on leader failover, no duplicates from internal retry.
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
            compression_type="gzip",
            request_timeout_ms=settings.kafka_request_timeout_ms,
            value_serializer=lambda event: event.model_dump_json().encode(),
            key_serializer=lambda key: key.encode(),
        )

    async def publish(self, event: GovernanceEvent) -> None:
        """Publish and wait. Raises :class:`EventPublishError` on any failure.

        Callers treat that exception as fatal to the tool call.
        """
        topic = self._topics.for_event(event)
        try:
            await asyncio.wait_for(
                self._producer.send_and_wait(topic, event, key=event.topic_key()),
                timeout=self._timeout_s,
            )
        except (KafkaError, TimeoutError) as exc:
            # Journal first: the attempt itself is a governance fact, and it is
            # most worth keeping precisely when the broker is unreachable.
            self._journal.append(event)
            raise EventPublishError(
                f"could not record {event.event_type} on {topic}: {exc}"
            ) from exc

    async def publish_best_effort(self, event: GovernanceEvent) -> None:
        try:
            await self.publish(event)
        except EventPublishError as exc:
            log.warning("journalled unpublished event %s: %s", event.event_id, exc)

    async def drain_journal(self) -> int:
        """Replay journalled events after the broker comes back."""
        pending = list(self._journal.pending())
        if not pending:
            return 0
        replayed = 0
        for event in pending:
            try:
                await self.publish(event)
            except EventPublishError:
                log.warning("journal drain stopped after %d event(s)", replayed)
                return replayed
            replayed += 1
        self._journal.clear()
        log.info("replayed %d journalled event(s)", replayed)
        return replayed
