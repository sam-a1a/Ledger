"""Canned conversations for the scripted backend.

These make the demo work with no API key, and they are deliberately *honest*
about it: the UI shows a demo-mode banner whenever they are in play. They also
double as the fixtures the golden suite runs against, so the scripts stay
exercised rather than drifting into decoration.

Matching is by keyword rather than anything clever. A scripted model that
pretended to understand arbitrary questions would be a worse lie than one that
obviously recognises a handful.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ledger.model.fake import ScriptedTurn, last_tool_result

Script = Callable[[list[dict[str, Any]], str], ScriptedTurn]


def _question(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return str(message["content"]).lower()
    return ""


def _busiest_zones(messages: list[dict[str, Any]], _: str) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            thinking="Ranking pickup zones by trip count.",
            tool_calls=[("top_n", {"dimension": "pickup_zone", "metric": {"op": "count"}, "n": 8})],
        )
    if last.get("ok") and last.get("tool") == "top_n":
        return ScriptedTurn(
            tool_calls=[
                (
                    "plot",
                    {
                        "result_id": last["result_id"],
                        "chart": {
                            "kind": "bar",
                            "x": "pickup_zone",
                            "y": ["row_count"],
                            "title": "Busiest pickup zones",
                            "sort": "y_desc",
                        },
                    },
                )
            ]
        )
    return ScriptedTurn(
        text=(
            "The busiest pickup zones are concentrated in Manhattan. The chart shows "
            "the top eight by trip count."
        )
    )


def _congestion(messages: list[dict[str, Any]], _: str) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            thinking="Congestion pricing began on 5 January 2025, so I want a monthly series.",
            tool_calls=[
                (
                    "timeseries",
                    {
                        "time_column": "pickup_at",
                        "grain": "month",
                        "metrics": [
                            {"op": "avg", "column": "cbd_congestion_fee", "alias": "avg_fee"},
                            {"op": "count"},
                        ],
                    },
                )
            ],
        )
    if last.get("ok") and last.get("tool") == "timeseries":
        return ScriptedTurn(
            tool_calls=[
                (
                    "plot",
                    {
                        "result_id": last["result_id"],
                        "chart": {
                            "kind": "line",
                            "x": "bucket",
                            "y": ["avg_fee"],
                            "title": "Average CBD congestion fee by month",
                        },
                    },
                )
            ]
        )
    return ScriptedTurn(
        text=(
            "The Manhattan CBD congestion fee first appears in January 2025 and is not "
            "recorded at all before then, because the column did not exist in the "
            "earlier files. January averages below the full 0.75 because the charge "
            "only took effect on the 5th, so part of that month predates it."
        )
    )


def _restricted(messages: list[dict[str, Any]], _: str) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(
            tool_calls=[
                (
                    "aggregate",
                    {
                        "metrics": [{"op": "avg", "column": "tip_amount"}],
                        "group_by": ["payment_type_label"],
                    },
                )
            ]
        )
    if last.get("ok"):
        return ScriptedTurn(
            text=(
                "Tips are only recorded for card payments, so the zero for cash is an "
                "artefact of how the data is collected rather than a real result."
            )
        )
    return ScriptedTurn(
        text=(
            "I do not have a tip column available in this dataset, so I cannot answer "
            "that. I can break down the total fare by payment type instead."
        )
    )


def _fallback(messages: list[dict[str, Any]], _: str) -> ScriptedTurn:
    last = last_tool_result(messages)
    if last is None:
        return ScriptedTurn(tool_calls=[("count_rows", {})])
    if last.get("ok"):
        matched = last["rows"][0][0]
        return ScriptedTurn(
            text=(
                f"This dataset holds {matched:,} trips. I am running in demo mode with a "
                "scripted model, so try one of the suggested questions -- with an API key "
                "configured I would answer this one properly."
            )
        )
    return ScriptedTurn(text="I could not answer that in demo mode.")


SCRIPTS: tuple[tuple[re.Pattern[str], Script], ...] = (
    (re.compile(r"busiest|most trips|top .*zone|popular"), _busiest_zones),
    (re.compile(r"congestion|fare change|after the charge|cbd"), _congestion),
    (re.compile(r"\btip"), _restricted),
)


def default_responder() -> Callable[[list[dict[str, Any]]], ScriptedTurn]:
    """Pick a script from the question and stay on it for the conversation."""

    def respond(messages: list[dict[str, Any]]) -> ScriptedTurn:
        question = _question(messages)
        for pattern, script in SCRIPTS:
            if pattern.search(question):
                return script(messages, question)
        return _fallback(messages, question)

    return respond
