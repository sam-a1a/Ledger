"""The streaming chat endpoint.

Two structural decisions, both about failure rather than the happy path.

**POST, not EventSource.** ``EventSource`` is GET-only, so using it would mean
putting the question and the conversation into a query string and losing the
Authorization header. The client uses ``fetch`` with a ReadableStream instead,
which also gives it an AbortController -- so cancellation is driven from the
browser and closes the loop with the server side below.

**The agent loop is not the response generator.** It runs as a task writing into
a queue, and the generator only drains that queue. If the loop *were* the
generator, cancellation would land inside the model client's HTTP stream and the
upstream connection might not close cleanly. Decoupling makes the generator
trivially cancellable and gives the task's `finally` sole ownership of cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ledger.agent import events as ev
from ledger.agent.loop import run_turn
from ledger.api import sse
from ledger.api.deps import StateDep, ToolContextDep
from ledger.logging import get_logger
from ledger.model.factory import make_model_client
from ledger.tools.context import ToolContext

log = get_logger(__name__)

router = APIRouter(tags=["chat"])

#: How long the generator waits for an event before emitting a keepalive.
HEARTBEAT_S = 10.0
#: Bounded so a runaway loop cannot exhaust memory; the producer awaits space.
QUEUE_SIZE = 256
#: How long to let the agent task unwind after a disconnect.
SHUTDOWN_GRACE_S = 5.0


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


_SENTINEL = object()


async def _pump(
    ctx: ToolContext,
    model: Any,
    body: ChatRequest,
    conversation_id: str,
    message_id: str,
    queue: asyncio.Queue[Any],
) -> None:
    """Run the loop, putting every event on the queue. Always closes the queue."""
    try:
        async for event in run_turn(
            ctx,
            model,
            history=[],
            user_message=body.message,
            conversation_id=conversation_id,
            message_id=message_id,
        ):
            await queue.put(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("agent loop failed")
        await queue.put(ev.Error(code="internal", message=str(exc), fatal=True))
    finally:
        await queue.put(_SENTINEL)


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    ctx: ToolContextDep,
    state: StateDep,
) -> StreamingResponse:
    conversation_id = body.conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    ctx.conversation_id = conversation_id
    ctx.message_id = message_id

    model = make_model_client(state.settings)
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_SIZE)

    async def stream() -> AsyncIterator[str]:
        task = asyncio.create_task(_pump(ctx, model, body, conversation_id, message_id, queue))
        seq = 0
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    # A long tool call must not look like a dead connection.
                    if await request.is_disconnected():
                        break
                    yield sse.comment("keepalive")
                    continue

                if item is _SENTINEL:
                    break
                seq += 1
                yield sse.format_frame(sse.event_name(item), sse.to_payload(item, seq))
        finally:
            # Cancelling the task is not enough on its own: a DuckDB scan runs
            # in a worker thread that an asyncio cancellation does not touch.
            # `interrupt()` is the only thing that actually stops it, and
            # without it the thread runs to completion holding a pool slot
            # while the client is long gone.
            ctx.cancelled.set()
            with contextlib.suppress(Exception):
                ctx.cursor.interrupt()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=SHUTDOWN_GRACE_S)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx and the Vite dev proxy both buffer otherwise, which is the
            # classic "streaming works locally but not in Docker" bug.
            "X-Accel-Buffering": "no",
        },
    )
