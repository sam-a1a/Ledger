"""Everything a tool call needs, assembled per request.

``ToolContext`` cannot be constructed without a scoped catalogue and a
publisher. That is deliberate: it makes "forgot to scope it" and "forgot to
audit it" constructor errors rather than silent omissions.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

import duckdb

from ledger.catalog.models import ScopedCatalog
from ledger.config import Settings
from ledger.governance.publisher import AuditPublisher
from ledger.security.principal import Principal
from ledger.tools.results import ToolResult

#: How many prior results a conversation can plot from.
RESULT_CACHE_SIZE = 16


class ResultCache:
    """Bounded per-conversation store of recent results.

    Exists so ``plot`` renders exactly the rows the model just narrated instead
    of re-running the query. A re-query can return different numbers -- a
    non-deterministic tie-break, a shifted boundary -- and a chart that disagrees
    with the prose above it is worse than no chart.
    """

    def __init__(self, maxsize: int = RESULT_CACHE_SIZE) -> None:
        self._items: OrderedDict[str, ToolResult] = OrderedDict()
        self._maxsize = maxsize

    def put(self, result: ToolResult) -> None:
        self._items[result.result_id] = result
        self._items.move_to_end(result.result_id)
        while len(self._items) > self._maxsize:
            self._items.popitem(last=False)

    def get(self, result_id: str) -> ToolResult | None:
        item = self._items.get(result_id)
        if item is not None:
            self._items.move_to_end(result_id)
        return item

    def ids(self) -> list[str]:
        return list(self._items)


@dataclass(slots=True)
class ToolContext:
    """Per-request state threaded into every tool."""

    principal: Principal
    scope: ScopedCatalog
    cursor: duckdb.DuckDBPyConnection
    publisher: AuditPublisher
    settings: Settings
    results: ResultCache = field(default_factory=ResultCache)
    conversation_id: str | None = None
    message_id: str | None = None
    #: Set when the client disconnects; checked between tool calls.
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def catalog_version(self) -> str:
        return self.scope.version

    def raise_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise asyncio.CancelledError("client disconnected")
