"""The agent loop, driven by a scripted model.

Only the model is faked. Tool calls go through the real registry, real
validation, real RBAC scoping, real DuckDB, and the real audit path -- so these
exercise the whole pipeline minus one component, offline and with no API key.
"""

from __future__ import annotations

from typing import Any

from ledger.agent import events as ev
from ledger.agent.loop import run_turn
from ledger.model.fake import FakeModelClient, ScriptedTurn, last_tool_result, sequence
from ledger.tools.context import ToolContext
from tests.conftest import published


async def drive(
    ctx: ToolContext, responder: Any, question: str = "how many trips?"
) -> list[ev.AgentEvent]:
    model = FakeModelClient(responder)
    return [
        event
        async for event in run_turn(
            ctx,
            model,
            history=[],
            user_message=question,
            conversation_id="conv-1",
            message_id="msg-1",
        )
    ]


def of(events: list[ev.AgentEvent], kind: type) -> list[Any]:
    return [e for e in events if isinstance(e, kind)]


async def test_a_plain_answer_streams_tokens_and_finishes(
    analyst_ctx: ToolContext,
) -> None:
    events = await drive(analyst_ctx, sequence([ScriptedTurn(text="There are 45,000 trips.")]))
    assert isinstance(events[0], ev.Meta)
    assert isinstance(events[-1], ev.Done)
    assert "".join(t.text for t in of(events, ev.Token)) == "There are 45,000 trips."
    assert events[-1].stop_reason == "end_turn"


async def test_a_tool_call_emits_start_then_end_around_execution(
    analyst_ctx: ToolContext,
) -> None:
    events = await drive(
        analyst_ctx,
        sequence(
            [
                ScriptedTurn(tool_calls=[("count_rows", {})]),
                ScriptedTurn(text="45,000 trips."),
            ]
        ),
    )
    starts, ends = of(events, ev.ToolCallStart), of(events, ev.ToolCallEnd)
    assert len(starts) == len(ends) == 1
    assert starts[0].call_id == ends[0].call_id
    assert ends[0].ok and ends[0].row_count == 1
    # The order matters: the status row must appear before the work, not after.
    assert events.index(starts[0]) < events.index(ends[0])


async def test_the_model_recovers_from_a_hallucinated_column(
    analyst_ctx: ToolContext,
) -> None:
    """The single most valuable offline test in the suite.

    It proves the whole recovery loop end to end -- hallucinated column, typed
    error, suggestion, corrected retry, answer -- and it is impossible with a
    flat list of canned turns, because turn two has to depend on what turn one
    was told.
    """

    def responder(messages: list[dict[str, Any]]) -> ScriptedTurn:
        last = last_tool_result(messages)
        if last is None:
            return ScriptedTurn(
                tool_calls=[("aggregate", {"metrics": [{"op": "avg", "column": "tip_pct"}]})]
            )
        if not last.get("ok") and last.get("error") == "unknown_column":
            # The fake genuinely reads the error, exactly as a model would.
            corrected = last["suggestions"][0]
            return ScriptedTurn(
                tool_calls=[("aggregate", {"metrics": [{"op": "avg", "column": corrected}]})]
            )
        value = last["rows"][0][0]
        return ScriptedTurn(text=f"The average is {value:.2f}.")

    events = await drive(analyst_ctx, responder)
    ends = of(events, ev.ToolCallEnd)

    assert len(ends) == 2
    assert not ends[0].ok and ends[0].error_code == "unknown_column"
    assert ends[1].ok
    answer = "".join(t.text for t in of(events, ev.Token))
    assert answer.startswith("The average is")
    # ...and both attempts are on the governance log.
    assert len(published(analyst_ctx).requested) == 2


async def test_parallel_tool_results_go_back_in_one_message(
    analyst_ctx: ToolContext,
) -> None:
    """Splitting them silently trains the model out of parallel calls."""
    captured: list[list[dict[str, Any]]] = []

    def responder(messages: list[dict[str, Any]]) -> ScriptedTurn:
        captured.append([dict(m) for m in messages])
        if last_tool_result(messages) is None:
            return ScriptedTurn(
                tool_calls=[
                    ("count_rows", {}),
                    ("list_columns", {}),
                ]
            )
        return ScriptedTurn(text="done")

    await drive(analyst_ctx, responder)
    final_history = captured[-1]
    tool_result_messages = [
        m
        for m in final_history
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert len(tool_result_messages) == 1
    assert len(tool_result_messages[0]["content"]) == 2


async def test_thinking_blocks_survive_into_the_next_request(
    analyst_ctx: ToolContext,
) -> None:
    """Reconstructing text-only content is the bug that degrades tool use later."""
    captured: list[list[dict[str, Any]]] = []

    def responder(messages: list[dict[str, Any]]) -> ScriptedTurn:
        captured.append([dict(m) for m in messages])
        if last_tool_result(messages) is None:
            return ScriptedTurn(thinking="Let me count.", tool_calls=[("count_rows", {})])
        return ScriptedTurn(text="45,000.")

    await drive(analyst_ctx, responder)
    assistant_turns = [m for m in captured[-1] if m["role"] == "assistant"]
    blocks = assistant_turns[0]["content"]
    assert any(b["type"] == "thinking" for b in blocks)
    assert any(b["type"] == "tool_use" for b in blocks)


async def test_thinking_is_not_streamed_unless_asked_for(
    analyst_ctx: ToolContext,
) -> None:
    events = await drive(analyst_ctx, sequence([ScriptedTurn(thinking="hmm", text="answer")]))
    assert not of(events, ev.Thinking)


async def test_plot_emits_a_chart_carrying_the_cached_rows(
    analyst_ctx: ToolContext,
) -> None:
    """The chart renders what the model narrated, never a fresh query."""
    state: dict[str, str] = {}

    def responder(messages: list[dict[str, Any]]) -> ScriptedTurn:
        last = last_tool_result(messages)
        if last is None:
            return ScriptedTurn(
                tool_calls=[
                    ("top_n", {"dimension": "pickup_borough", "metric": {"op": "count"}, "n": 4})
                ]
            )
        if "result_id" not in state:
            state["result_id"] = last["result_id"]
            return ScriptedTurn(
                tool_calls=[
                    (
                        "plot",
                        {
                            "result_id": state["result_id"],
                            "chart": {
                                "kind": "bar",
                                "x": "pickup_borough",
                                "y": ["row_count"],
                                "title": "Trips by borough",
                            },
                        },
                    )
                ]
            )
        return ScriptedTurn(text="Manhattan leads.")

    events = await drive(analyst_ctx, responder)
    charts = of(events, ev.Chart)
    assert len(charts) == 1
    assert charts[0].spec["kind"] == "bar"
    assert [c["name"] for c in charts[0].columns] == ["pickup_borough", "row_count"]
    assert len(charts[0].rows) == 4


async def test_the_loop_stops_at_the_turn_limit(analyst_ctx: ToolContext) -> None:
    """A model that never stops calling tools must not run forever."""

    def responder(_: list[dict[str, Any]]) -> ScriptedTurn:
        return ScriptedTurn(tool_calls=[("count_rows", {})])

    events = await drive(analyst_ctx, responder)
    errors = of(events, ev.Error)
    assert errors and errors[0].code == "max_turns"
    assert not errors[0].fatal
    assert events[-1].stop_reason == "max_turns"
    assert events[-1].turns == analyst_ctx.settings.max_turns


async def test_a_viewer_is_refused_and_the_denial_is_audited(
    viewer_ctx: ToolContext,
) -> None:
    """The end-to-end RBAC path, with a model in the loop."""

    def responder(messages: list[dict[str, Any]]) -> ScriptedTurn:
        last = last_tool_result(messages)
        if last is None:
            return ScriptedTurn(
                tool_calls=[("aggregate", {"metrics": [{"op": "avg", "column": "tip_amount"}]})]
            )
        return ScriptedTurn(text="That column is not available to me.")

    events = await drive(viewer_ctx, responder, question="what is the average tip?")
    ends = of(events, ev.ToolCallEnd)
    assert not ends[0].ok
    assert ends[0].error_code == "unknown_column"

    answer = "".join(t.text for t in of(events, ev.Token))
    assert "not available" in answer
    # The model was never told the column exists; the audit log records that it
    # was reached for.
    assert published(viewer_ctx).denied_columns() == {"tip_amount"}
