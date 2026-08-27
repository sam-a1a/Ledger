"""The audit materialiser.

Runs as its own process, deliberately. In-process it would be a debug print with
extra steps: a slow consumer would add backpressure to the API's event loop, a
crash would take down chat, and "the trace panel is a view over an event log"
would not be meaningfully true. Separate, the audit store can be rebuilt from
the log without touching the API.

Offsets are committed **after** the parquet flush is durable. Combined with
deduplication on ``event_id`` at read time, at-least-once delivery becomes
effectively exactly-once for the view.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from aiokafka import AIOKafkaConsumer

from ledger.config import Settings, get_settings
from ledger.governance.events import GovernanceEvent
from ledger.governance.store import events_dir, flatten
from ledger.governance.topics import topics_for
from ledger.logging import configure_logging, get_logger

log = get_logger(__name__)

#: Flush when either threshold trips, so a quiet system still materialises
#: promptly and a busy one still batches.
BATCH_SIZE = 200
FLUSH_INTERVAL_MS = 1_000


class AuditConsumer:
    """Drains the audit topics into date-partitioned parquet."""

    def __init__(self, settings: Settings, *, group_suffix: str = "") -> None:
        self._settings = settings
        self._topics = topics_for(settings)
        self._group = settings.kafka_consumer_group + group_suffix
        self._dir = events_dir(settings.data_dir)
        self._buffer: list[dict[str, Any]] = []
        self._consumer: AIOKafkaConsumer | None = None

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            *self._topics.all(),
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._group,
            # Manual commits: the offset must not advance until the data is on
            # disk, or a crash silently loses events.
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=lambda raw: raw.decode(),
        )
        await self._consumer.start()
        log.info("audit consumer reading %s as %s", ", ".join(self._topics.all()), self._group)

    async def stop(self) -> None:
        if self._consumer is not None:
            with contextlib.suppress(Exception):
                await self._flush()
            await self._consumer.stop()

    async def run_once(self, timeout_ms: int = FLUSH_INTERVAL_MS) -> int:
        """Poll once, buffer, and flush if the batch is ready. Returns rows written."""
        if self._consumer is None:
            raise RuntimeError("consumer was not started")

        batches = await self._consumer.getmany(timeout_ms=timeout_ms)
        for records in batches.values():
            for record in records:
                try:
                    event = GovernanceEvent.model_validate_json(record.value)
                except ValueError:
                    # A malformed record must not wedge the consumer forever.
                    log.warning("skipping unparseable event at offset %d", record.offset)
                    continue
                self._buffer.append(flatten(event))

        if not self._buffer:
            return 0
        if len(self._buffer) >= BATCH_SIZE or batches:
            return await self._flush()
        return 0

    async def _flush(self) -> int:
        if self._consumer is None or not self._buffer:
            return 0

        rows, self._buffer = self._buffer, []
        written = await asyncio.to_thread(self._write, rows)
        # Only now is it safe to advance.
        await self._consumer.commit()
        log.info("materialised %d event(s)", written)
        return written

    def _write(self, rows: list[dict[str, Any]]) -> int:
        partition = self._dir / f"dt={datetime.now(UTC):%Y-%m-%d}"
        partition.mkdir(parents=True, exist_ok=True)

        table = pa.Table.from_pylist(_normalise(rows))
        name = f"part-{uuid.uuid4().hex[:16]}.parquet"
        # Write then rename: a reader in another process sees a whole file or
        # no file, never a half-written one.
        temporary = partition / f".{name}.tmp"
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(partition / name)
        return len(rows)

    async def run_forever(self) -> None:
        while True:
            await self.run_once()


def _normalise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every row the same keys, so parquet gets one stable schema."""
    keys: set[str] = set()
    for row in rows:
        keys |= set(row)
    ordered = sorted(keys)
    return [{key: row.get(key) for key in ordered} for row in rows]


async def _main(rebuild: bool) -> int:
    settings = get_settings()
    suffix = ""
    if rebuild:
        # A fresh group id replays the whole topic -- the demonstration that the
        # log, not the parquet, is the source of truth.
        suffix = f"-rebuild-{uuid.uuid4().hex[:8]}"
        target = events_dir(settings.data_dir)
        if target.exists():
            for path in target.rglob("*.parquet"):
                path.unlink()
        log.info("rebuilding the audit store from the beginning of the log")

    consumer = AuditConsumer(settings, group_suffix=suffix)
    await consumer.start()
    try:
        await consumer.run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="ledger-audit", description=__doc__)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Discard the materialised store and replay the topic from the start",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main(args.rebuild))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
