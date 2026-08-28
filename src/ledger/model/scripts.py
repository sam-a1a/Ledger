"""Canned conversations for the scripted backend.

These make the demo work with no API key, and they are honest about it: the UI
shows a demo-mode banner whenever they are in play. They also back the golden
suite, so they stay exercised rather than drifting into decoration.

Matching is by keyword. A scripted model that pretended to understand arbitrary
questions would be a worse lie than one that obviously recognises a handful.

Each script is a function of the conversation so far, which is what lets a
script react to a tool result -- including reacting to an *error*, which is the
behaviour the recovery tests exist to prove.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ledger.model.fake import ScriptedTurn, last_tool_result

Script = Callable[[list[dict[str, Any]]], ScriptedTurn]


def _question(messages: list[dict[str, Any]]) -> str:
    """The question being asked *now*, which is the last one, not the first.

    Scanning forwards worked only while every conversation was a single turn.
    Once history was threaded in, a follow-up kept answering the opening
    question -- convincingly, and with a real chart, which is the worst way for
    it to be wrong.
    """
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return str(message["content"]).lower()
    return ""


def _plot(result: dict[str, Any], **chart: Any) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[("plot", {"result_id": result["result_id"], "chart": chart})])


# --------------------------------------------------------------------- scripts


def busiest_zones(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            thinking="Ranking pickup zones by trip count.",
            tool_calls=[("top_n", {"dimension": "pickup_zone", "metric": {"op": "count"}, "n": 8})],
        )
    if last.get("tool") == "top_n" and last.get("ok"):
        return _plot(
            last,
            kind="bar",
            x="pickup_zone",
            y=["row_count"],
            title="Busiest pickup zones",
            sort="y_desc",
        )
    return ScriptedTurn(
        text=(
            "The busiest pickup zones are almost all in Manhattan. The chart shows the "
            "top eight by trip count."
        )
    )


def congestion_change(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            thinking="Congestion pricing began on 5 January 2025, so a monthly series will show it.",
            tool_calls=[
                (
                    "timeseries",
                    {
                        "time_column": "pickup_at",
                        "grain": "month",
                        "metrics": [
                            {"op": "avg", "column": "cbd_congestion_fee", "alias": "avg_fee"},
                            {"op": "avg", "column": "total_amount", "alias": "avg_total"},
                        ],
                    },
                )
            ],
        )
    if last.get("tool") == "timeseries" and last.get("ok"):
        return _plot(
            last,
            kind="line",
            x="bucket",
            y=["avg_fee"],
            title="Average CBD congestion fee by month",
        )
    return ScriptedTurn(
        text=(
            "The Manhattan CBD congestion fee first appears in January 2025 and is not "
            "recorded at all before then, because the column did not exist in the earlier "
            "files. January averages below the full charge because it only took effect on "
            "the 5th, so part of that month predates it."
        )
    )


def tip_by_payment(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            tool_calls=[
                (
                    "aggregate",
                    {
                        "metrics": [{"op": "avg", "column": "tip_amount", "alias": "avg_tip"}],
                        "group_by": ["payment_type_label"],
                    },
                )
            ]
        )
    if last.get("ok"):
        return ScriptedTurn(
            text=(
                "Card payments carry essentially all the recorded tips. Cash shows zero, "
                "but that is an artefact of collection rather than a real result: cash tips "
                "are never recorded."
            )
        )
    return ScriptedTurn(
        text=(
            "I do not have a tip column available in this dataset, so I cannot answer that. "
            "I can break the total fare down by payment type instead."
        )
    )


def fare_by_borough(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            tool_calls=[
                (
                    "aggregate",
                    {
                        "metrics": [
                            {"op": "avg", "column": "fare_amount", "alias": "avg_fare"},
                            {"op": "count"},
                        ],
                        "group_by": ["pickup_borough"],
                        "order_by": [{"key": "avg_fare", "direction": "desc"}],
                    },
                )
            ]
        )
    if last.get("tool") == "aggregate" and last.get("ok"):
        return _plot(
            last,
            kind="bar",
            x="pickup_borough",
            y=["avg_fare"],
            title="Average fare by pickup borough",
        )
    return ScriptedTurn(text="Average fares differ by borough, as the chart shows.")


def trip_distance_distribution(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(tool_calls=[("distribution", {"column": "trip_distance", "bins": 20})])
    if last.get("tool") == "distribution" and last.get("ok"):
        return _plot(
            last,
            kind="bar",
            x="bin_lower",
            y=["row_count"],
            title="Distribution of trip distance",
        )
    return ScriptedTurn(
        text=(
            "Most trips are short, with a long thin tail. The chart clips the top "
            "percentile, without which a handful of very long trips would put every "
            "row in one bin."
        )
    )


def hourly_demand(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            tool_calls=[
                (
                    "aggregate",
                    {
                        "metrics": [{"op": "count"}],
                        "group_by": ["pickup_hour"],
                        "order_by": [{"key": "pickup_hour", "direction": "asc"}],
                        "limit": 24,
                    },
                )
            ]
        )
    if last.get("tool") == "aggregate" and last.get("ok"):
        return _plot(
            last, kind="line", x="pickup_hour", y=["row_count"], title="Trips by hour of day"
        )
    return ScriptedTurn(text="Demand rises through the day and peaks in the evening.")


def how_many_trips(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(tool_calls=[("count_rows", {})])
    rows = last.get("rows") or [[0]]
    return ScriptedTurn(text=f"There are {rows[0][0]:,} trips in the loaded window.")


def what_columns(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(tool_calls=[("list_columns", {})])
    return ScriptedTurn(
        text=(
            f"There are {last.get('row_count', 0)} columns available to you, covering "
            "timing, pickup and dropoff zones, distance, and fare components."
        )
    )


def describe_then_use(messages: list[dict[str, Any]]) -> ScriptedTurn:
    """Check a column's caveat before relying on it."""
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(tool_calls=[("describe_column", {"column": "passenger_count"})])
    if last.get("tool") == "describe_column":
        return ScriptedTurn(
            tool_calls=[
                (
                    "aggregate",
                    {
                        "metrics": [{"op": "avg", "column": "passenger_count", "alias": "avg_pax"}],
                        "group_by": ["pickup_borough"],
                    },
                )
            ]
        )
    return ScriptedTurn(
        text=(
            "Average passenger counts are close to one everywhere. Treat this loosely: the "
            "field is entered by the driver and is often missing."
        )
    )


def hallucinate_then_recover(messages: list[dict[str, Any]]) -> ScriptedTurn:
    """Ask for a column that does not exist, read the error, retry."""
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            tool_calls=[("aggregate", {"metrics": [{"op": "avg", "column": "tip_pct"}]})]
        )
    if not last.get("ok") and last.get("error") == "unknown_column":
        suggestions = last.get("suggestions") or ["fare_amount"]
        return ScriptedTurn(
            tool_calls=[
                ("aggregate", {"metrics": [{"op": "avg", "column": suggestions[0], "alias": "v"}]})
            ]
        )
    rows = last.get("rows") or [[0]]
    return ScriptedTurn(text=f"After correcting the column, the average is {rows[0][0]:.2f}.")


def cardinality_trap(messages: list[dict[str, Any]]) -> ScriptedTurn:
    """Try to group by a timestamp, get refused, pivot to timeseries."""
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            tool_calls=[("aggregate", {"metrics": [{"op": "count"}], "group_by": ["pickup_at"]})]
        )
    if not last.get("ok") and last.get("error") == "cardinality_exceeded":
        return ScriptedTurn(
            tool_calls=[
                (
                    "timeseries",
                    {
                        "time_column": "pickup_at",
                        "grain": "day",
                        "metrics": [{"op": "count"}],
                    },
                )
            ]
        )
    return ScriptedTurn(
        text=(
            "Grouping by the raw timestamp would have produced millions of groups, so this "
            "is bucketed by day instead."
        )
    )


def empty_filter_recovery(messages: list[dict[str, Any]]) -> ScriptedTurn:
    """Filter on a value that does not exist, read the diagnosis, retry."""
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            tool_calls=[
                (
                    "count_rows",
                    {"filters": [{"column": "pickup_borough", "op": "=", "value": "Brooklin"}]},
                )
            ]
        )
    rows = last.get("rows") or [[0]]
    if rows[0][0] == 0:
        return ScriptedTurn(
            tool_calls=[
                (
                    "count_rows",
                    {"filters": [{"column": "pickup_borough", "op": "=", "value": "Brooklyn"}]},
                )
            ]
        )
    return ScriptedTurn(text=f"Corrected to Brooklyn: {rows[0][0]:,} trips.")


def hostile_prompt(messages: list[dict[str, Any]]) -> ScriptedTurn:
    """Faithfully attempt what an injected instruction asks for."""
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            tool_calls=[("aggregate", {"metrics": [{"op": "avg", "column": "tip_amount"}]})]
        )
    if last.get("ok"):
        rows = last.get("rows") or [[0]]
        return ScriptedTurn(text=f"The average tip is {rows[0][0]:.2f}.")
    return ScriptedTurn(text="There is no such column available to me, so I cannot answer that.")


def ambiguous(messages: list[dict[str, Any]]) -> ScriptedTurn:
    """No tool call: ask for clarification instead of guessing."""
    return ScriptedTurn(
        text=(
            "That could mean a few different things. Do you want the busiest pickup zones, "
            "the busiest hours, or the busiest days?"
        )
    )


def fallback(messages: list[dict[str, Any]]) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(tool_calls=[("count_rows", {})])
    if last.get("ok"):
        rows = last.get("rows") or [[0]]
        return ScriptedTurn(
            text=(
                f"This dataset holds {rows[0][0]:,} trips. I am running in demo mode with a "
                "scripted model, so try one of the suggested questions — with an API key "
                "configured I would answer this one properly."
            )
        )
    return ScriptedTurn(text="I could not answer that in demo mode.")


#: Named so the golden suite can select one directly.
SCRIPTS: dict[str, Script] = {
    "busiest_zones": busiest_zones,
    "congestion_change": congestion_change,
    "tip_by_payment": tip_by_payment,
    "fare_by_borough": fare_by_borough,
    "trip_distance_distribution": trip_distance_distribution,
    "hourly_demand": hourly_demand,
    "how_many_trips": how_many_trips,
    "what_columns": what_columns,
    "describe_then_use": describe_then_use,
    "hallucinate_then_recover": hallucinate_then_recover,
    "cardinality_trap": cardinality_trap,
    "empty_filter_recovery": empty_filter_recovery,
    "hostile_prompt": hostile_prompt,
    "ambiguous": ambiguous,
    "fallback": fallback,
}

#: Keyword routing for the demo, in priority order.
_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"congestion|fare change|after the charge|cbd"), "congestion_change"),
    (re.compile(r"\btip"), "tip_by_payment"),
    (re.compile(r"busiest|most trips|top .*zone|popular"), "busiest_zones"),
    (re.compile(r"average fare|fare by|borough"), "fare_by_borough"),
    (re.compile(r"distribut|histogram|spread|how far"), "trip_distance_distribution"),
    (re.compile(r"hour|time of day|peak"), "hourly_demand"),
    (re.compile(r"how many trips|how many rows|dataset size"), "how_many_trips"),
    (re.compile(r"what columns|which columns|what data"), "what_columns"),
    (re.compile(r"passenger"), "describe_then_use"),
)


def resolve(name: str) -> Script:
    return SCRIPTS[name]


def default_responder() -> Callable[[list[dict[str, Any]]], ScriptedTurn]:
    """Pick a script from the question and stay on it for the conversation."""

    def respond(messages: list[dict[str, Any]]) -> ScriptedTurn:
        question = _question(messages)
        for pattern, name in _ROUTES:
            if pattern.search(question):
                return SCRIPTS[name](messages)
        return fallback(messages)

    return respond
