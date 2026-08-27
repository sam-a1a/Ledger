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
from ledger.api.deps import SessionDep, StateDep, ToolContextDep, UserDep
from ledger.conversations import service as conversations
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
    """What the client may send.

    Deliberately *not* a message history. History is reconstructed server-side
    from the conversation id: a client that could replay arbitrary assistant
    turns could fabricate tool results the model then treats as its own
    findings, which would defeat the premise that the model only ever sees what
    the tool layer returned.
    """

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
    history: list[dict[str, Any]],
    recorder: _Recorder,
) -> None:
    """Run the loop, putting every event on the queue. Always closes the queue."""
    try:
        async for event in run_turn(
            ctx,
            model,
            history=history,
            user_message=body.message,
            conversation_id=conversation_id,
            message_id=message_id,
        ):
            recorder.observe(event)
            await queue.put(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("agent loop failed")
        await queue.put(ev.Error(code="internal", message=str(exc), fatal=True))
    finally:
        await queue.put(_SENTINEL)


class _Recorder:
    """Collects what needs persisting as the turn streams.

    The answer text and the trace are assembled here rather than re-derived
    afterwards, so a transcript reads back exactly as it was seen.
    """

    def __init__(self) -> None:
        self.text: list[str] = []
        self.trace: list[dict[str, Any]] = []
        self._calls: dict[str, dict[str, Any]] = {}

    def observe(self, event: Any) -> None:
        if isinstance(event, ev.Token):
            self.text.append(event.text)
        elif isinstance(event, ev.ToolCallStart):
            entry: dict[str, Any] = {
                "tool": event.tool,
                "args": event.args,
                "call_id": event.call_id,
            }
            self._calls[event.call_id] = entry
            self.trace.append(entry)
        elif isinstance(event, ev.ToolCallEnd):
            existing = self._calls.get(event.call_id)
            if existing is not None:
                existing["ok"] = event.ok
                existing["row_count"] = event.row_count
                existing["duration_ms"] = event.duration_ms
                existing["error_code"] = event.error_code

    @property
    def answer(self) -> str:
        return "".join(self.text)


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    ctx: ToolContextDep,
    state: StateDep,
    user: UserDep,
    session: SessionDep,
) -> StreamingResponse:
    conversation = await conversations.ensure(
        session,
        conversation_id=body.conversation_id,
        user_id=user.id,
        role=user.role,
        tenant_id=user.tenant_id,
        first_question=body.message,
    )
    # Reconstructed here, never accepted from the client.
    history = await conversations.history(session, conversation.id, user.id)

    conversation_id = conversation.id
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    ctx.conversation_id = conversation_id
    ctx.message_id = message_id

    await conversations.append(session, conversation, role="user", content=body.message)
    await session.commit()

    recorder = _Recorder()
    model = make_model_client(state.settings)
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_SIZE)

    async def stream() -> AsyncIterator[str]:
        task = asyncio.create_task(
            _pump(ctx, model, body, conversation_id, message_id, queue, history, recorder)
        )
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

            # Persist whatever was produced, including a partial answer after a
            # disconnect: a cancelled turn that vanishes from the transcript is
            # more confusing than one that stops mid-sentence.
            if recorder.answer or recorder.trace:
                with contextlib.suppress(Exception):
                    await conversations.append(
                        session,
                        conversation,
                        role="assistant",
                        content=recorder.answer,
                        rendered=recorder.answer,
                        trace=recorder.trace,
                    )
                    await session.commit()

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
