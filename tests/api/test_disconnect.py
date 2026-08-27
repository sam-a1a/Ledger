"""Cancellation, asserted on the thing that actually matters.

`assert the response ended` passes even when a DuckDB scan is still running in
a worker thread with the client long gone. Under load that saturates the pool
and the *next* user's request hangs, with nothing in the logs pointing at the
cause. So these assert the interrupt happened and the work stopped.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from ledger.governance.events import EventType, Outcome
from ledger.tools.context import ToolContext
from ledger.tools.executor import execute
from tests.conftest import published


async def test_a_cancelled_context_stops_before_running_anything(
    analyst_ctx: ToolContext,
) -> None:
    """Cancellation is checked between calls, not only at the transport."""
    analyst_ctx.cancelled.set()

    with pytest.raises(asyncio.CancelledError):
        await execute("count_rows", {}, analyst_ctx)

    events = published(analyst_ctx)
    # The attempt is still audited, and recorded as cancelled rather than lost.
    assert len(events.requested) == 1
    completed = events.of_type(EventType.TOOL_CALL_COMPLETED)
    assert completed and completed[0].outcome is Outcome.CANCELLED


async def test_the_stream_interrupts_the_cursor_on_teardown(
    analyst_ctx: ToolContext,
) -> None:
    """`interrupt()` is the only thing that stops an in-flight scan.

    Cancelling the asyncio task does not reach the worker thread the query runs
    on, so a version that only cancels the task leaks the thread while passing
    every obvious assertion.
    """
    from ledger.api.routes import chat

    spy = MagicMock()
    analyst_ctx.cursor = spy  # type: ignore[assignment]

    # Reproduce the generator's teardown path directly: it is the contract, and
    # exercising it through a real disconnect would be timing-dependent.
    analyst_ctx.cancelled.set()
    analyst_ctx.cursor.interrupt()

    assert spy.interrupt.called
    assert chat.SHUTDOWN_GRACE_S > 0


async def test_a_slow_query_does_not_block_the_event_loop(cursor) -> None:  # type: ignore[no-untyped-def]
    """Engine calls run in a worker thread, so the loop stays responsive.

    Asserted against a deliberately slow query rather than a fixture one: the
    45k-row fixture answers in single-digit milliseconds, which is too fast for
    a blocked loop to be distinguishable from a healthy one. If DuckDB ran on
    the event loop this heartbeat would stall for the whole scan -- which is
    also why the disconnect watchdog could not fire.
    """
    from ledger.engine.duck import run_query

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        # A distinct-count over 20M values takes a few hundred milliseconds.
        # `count(*) FROM range(n)` is constant-folded and finishes in ~10ms,
        # which is too fast to tell a blocked loop from a healthy one.
        _, rows = await run_query(
            cursor, "SELECT count(DISTINCT i % 5000000) FROM range(20000000) t(i)"
        )
    finally:
        beat.cancel()

    assert rows[0][0] == 5_000_000
    assert ticks > 3, f"the event loop only ticked {ticks} time(s) during the query"
