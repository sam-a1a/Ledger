"""The agentic loop.

Hand-written rather than the SDK's tool runner, for reasons that are specific
rather than stylistic: the fake-model seam is one Protocol here instead of a
mock of the SDK's beta namespace; the tool registry stays shared verbatim with
the MCP server rather than being welded to Anthropic decorators; per-request
context (principal, cursor, publisher, result cache) threads through cleanly;
and we control exactly when a status event is emitted and what it is timed
against.

Three things in here are non-negotiable, and each is a bug that survives review:

1. ``final.content`` is appended **verbatim**, thinking blocks included.
   Reconstructing text-only content loses blocks the model must see echoed
   back, and the symptom is erratic tool use several turns later, not a crash.
2. All tool results for a turn go back in **one** user message. Splitting them
   quietly teaches the model to stop making parallel calls.
3. Tool inputs are used as parsed dicts, never string-matched.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from ledger.agent import events as ev
from ledger.agent.prompt import system_blocks
from ledger.model.protocol import (
    AssistantTurn,
    ModelClient,
    TextDelta,
    ThinkingDelta,
    ToolUseReady,
    Usage,
)
from ledger.tools import registry
from ledger.tools.context import ToolContext
from ledger.tools.executor import execute
from ledger.tools.results import ToolError, ToolResult


def _tool_result_block(tool_use_id: str, outcome: ToolResult | ToolError) -> dict[str, Any]:
    """Render a tool outcome for the conversation.

    Errors carry ``is_error`` so the model treats them as something to recover
    from rather than as data.
    """
    payload = outcome.model_dump(mode="json", exclude_none=True)
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(payload, default=str),
        "is_error": not outcome.ok,
    }


async def run_turn(
    ctx: ToolContext,
    model: ModelClient,
    *,
    history: list[dict[str, Any]],
    user_message: str,
    conversation_id: str,
    message_id: str,
) -> AsyncIterator[ev.AgentEvent]:
    """Drive one user question to an answer, emitting events as it goes."""
    settings = ctx.settings
    started = time.perf_counter()

    yield ev.Meta(
        conversation_id=conversation_id,
        message_id=message_id,
        role=ctx.principal.role.value,
        tenant_id=ctx.principal.tenant_id,
        model_backend=model.name,
        catalog_version=ctx.catalog_version,
        demo_mode=settings.demo_mode,
    )

    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": user_message}]
    system = system_blocks(ctx.scope)
    tools = registry.schemas_for(ctx.scope)

    last_usage: Usage | None = None
    turns = 0
    stop_reason: str = "end_turn"

    for turn in range(settings.max_turns):
        turns = turn + 1
        ctx.raise_if_cancelled()

        pending: list[ToolUseReady] = []
        async with model.stream_turn(system=system, messages=messages, tools=tools) as stream:
            async for event in stream:
                match event:
                    case TextDelta(text):
                        yield ev.Token(text)
                    case ThinkingDelta(text):
                        if settings.show_thinking:
                            yield ev.Thinking(text)
                    case ToolUseReady() as ready:
                        pending.append(ready)
            final: AssistantTurn = await stream.final_message()

        last_usage = final.usage
        # Verbatim: thinking blocks must survive into the next request.
        messages.append({"role": "assistant", "content": final.content})

        if final.stop_reason == "pause_turn":
            continue
        if final.stop_reason != "tool_use" or not pending:
            stop_reason = "end_turn"
            break

        results: list[dict[str, Any]] = []
        for ready in pending:
            yield ev.ToolCallStart(call_id=ready.id, tool=ready.name, args=ready.input, turn=turn)
            outcome = await execute(ready.name, ready.input, ctx, call_id=ready.id)

            if outcome.ok:
                yield ev.ToolCallEnd(
                    call_id=ready.id,
                    tool=ready.name,
                    ok=True,
                    duration_ms=outcome.duration_ms,
                    row_count=outcome.row_count,
                    truncated=outcome.truncated,
                    result_id=outcome.result_id,
                    notes=outcome.notes,
                )
                if ready.name == "plot":
                    chart = _chart_event(ready, outcome, ctx)
                    if chart is not None:
                        yield chart
            else:
                yield ev.ToolCallEnd(
                    call_id=ready.id,
                    tool=ready.name,
                    ok=False,
                    duration_ms=0,
                    error_code=outcome.error.value,
                    message=outcome.message,
                )

            results.append(_tool_result_block(ready.id, outcome))

        # One message, every result. Splitting them trains the model out of
        # making parallel calls.
        messages.append({"role": "user", "content": results})
    else:
        stop_reason = "max_turns"
        yield ev.Error(
            code="max_turns",
            message=(
                f"Reached the {settings.max_turns}-step limit without finishing. "
                "Try a narrower question."
            ),
            fatal=False,
        )

    yield ev.Done(
        message_id=message_id,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        turns=turns,
        total_duration_ms=int((time.perf_counter() - started) * 1000),
        usage=last_usage or Usage(),
    )


def _chart_event(ready: ToolUseReady, outcome: ToolResult, ctx: ToolContext) -> ev.Chart | None:
    """Attach the cached rows to the chart the model just asked for.

    The cache holds more rows than the model was shown, so a chart can render
    detail the model never had to read.
    """
    spec = ready.input.get("chart")
    source_id = ready.input.get("result_id")
    if not isinstance(spec, dict) or not isinstance(source_id, str):
        return None
    cached = ctx.results.get(source_id)
    if cached is None:
        return None
    return ev.Chart(
        chart_id=outcome.result_id,
        call_id=ready.id,
        spec=spec,
        columns=[{"name": c.name, "type": c.type} for c in cached.columns],
        rows=cached.rows,
    )
