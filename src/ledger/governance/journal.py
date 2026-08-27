"""A local write-ahead journal for governance events.

Kafka is a hard dependency, but "hard dependency" must not mean "an event is
lost whenever the broker blinks". Every event is appended here before a publish
failure is reported, and a replayer drains the journal on the next successful
connect.

So an event is always in one of three states -- in Kafka, in this journal
awaiting replay, or visibly orphaned in the audit view. Never silently dropped.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

from ledger.governance.events import GovernanceEvent
from ledger.logging import get_logger

log = get_logger(__name__)


class EventJournal:
    """Append-only NDJSON, fsync'd on write."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: GovernanceEvent) -> None:
        """Record an event that could not be published.

        fsync'd rather than buffered: the entire reason this file exists is the
        case where the process is about to fail, and a buffered write would be
        lost exactly when it mattered.
        """
        line = event.model_dump_json() + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def pending(self) -> Iterator[GovernanceEvent]:
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield GovernanceEvent.model_validate_json(line)
                except ValueError:
                    # A torn final line from an abrupt exit. Skip it loudly
                    # rather than refusing to replay everything before it.
                    log.warning("skipping unparseable journal line %d in %s", number, self._path)

    def count(self) -> int:
        return sum(1 for _ in self.pending())

    def clear(self) -> None:
        """Drop the journal after a successful drain."""
        with self._lock:
            self._path.unlink(missing_ok=True)
