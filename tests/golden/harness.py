"""Running and asserting the golden questions.

The suite drives the real agent loop against the real tool layer and DuckDB.
Under the scripted backend it is deterministic, offline, and free; under
``-m ai_live`` the same assertions run against the real model.

Matching is deliberately loose where a model may reasonably vary and strict
where it must not: tool sequences are subsequences, arguments are subset
matches, and answers are checked for the numbers the run actually produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ledger.agent import events as ev
from ledger.agent.loop import run_turn
from ledger.model.fake import FakeModelClient
from ledger.model.protocol import ModelClient
from ledger.model.scripts import resolve
from ledger.tools.context import ToolContext

QUESTIONS_PATH = Path(__file__).with_name("questions.yaml")


@dataclass(slots=True)
class RecordedCall:
    tool: str
    args: dict[str, Any]
    ok: bool
    error: str | None
    row_count: int | None
    result: dict[str, Any] | None = None


@dataclass(slots=True)
class Trace:
    """What a run actually did, in the shape the assertions need."""

    answer: str = ""
    calls: list[RecordedCall] = field(default_factory=list)
    charts: int = 0
    stop_reason: str = ""
    turns: int = 0


def load_questions() -> list[dict[str, Any]]:
    return list(yaml.safe_load(QUESTIONS_PATH.read_text()))


def model_for(question: dict[str, Any]) -> ModelClient:
    return FakeModelClient(resolve(question["script"]))


async def run(ctx: ToolContext, model: ModelClient, question: str) -> Trace:
    trace = Trace()
    pending: dict[str, RecordedCall] = {}

    async for event in run_turn(
        ctx,
        model,
        history=[],
        user_message=question,
        conversation_id="golden",
        message_id="golden-1",
    ):
        match event:
            case ev.Token(text):
                trace.answer += text
            case ev.ToolCallStart(call_id, tool, args, _):
                call = RecordedCall(tool=tool, args=args, ok=False, error=None, row_count=None)
                pending[call_id] = call
                trace.calls.append(call)
            case ev.ToolCallEnd() as end:
                call = pending.get(end.call_id)
                if call is not None:
                    call.ok = end.ok
                    call.error = end.error_code
                    call.row_count = end.row_count
                    if end.result_id:
                        cached = ctx.results.get(end.result_id)
                        if cached is not None:
                            call.result = cached.model_dump(mode="json")
            case ev.Chart():
                trace.charts += 1
            case ev.Done() as done:
                trace.stop_reason = done.stop_reason
                trace.turns = done.turns
            case _:
                pass
    return trace


# ------------------------------------------------------------------ assertions


def _subset(expected: Any, actual: Any, path: str) -> list[str]:
    """Recursive subset match, with `{op: le, value: n}` comparators on leaves."""
    problems: list[str] = []

    if isinstance(expected, dict) and set(expected) == {"op", "value"}:
        op, value = expected["op"], expected["value"]
        ok = {
            "eq": lambda a, b: a == b,
            "le": lambda a, b: a <= b,
            "ge": lambda a, b: a >= b,
            "lt": lambda a, b: a < b,
            "gt": lambda a, b: a > b,
        }[op](actual, value)
        if not ok:
            problems.append(f"{path}: expected {op} {value}, got {actual!r}")
        return problems

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected an object, got {actual!r}"]
        for key, sub in expected.items():
            if key not in actual:
                problems.append(f"{path}.{key}: missing")
            else:
                problems += _subset(sub, actual[key], f"{path}.{key}")
        return problems

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected a list, got {actual!r}"]
        for i, sub in enumerate(expected):
            if i >= len(actual):
                problems.append(f"{path}[{i}]: missing")
            else:
                problems += _subset(sub, actual[i], f"{path}[{i}]")
        return problems

    if expected != actual:
        problems.append(f"{path}: expected {expected!r}, got {actual!r}")
    return problems


def _matches(step: dict[str, Any], call: RecordedCall) -> bool:
    if step.get("tool") and step["tool"] != call.tool:
        return False
    if "ok" in step and step["ok"] != call.ok:
        return False
    if step.get("error") and step["error"] != call.error:
        return False
    if "min_rows" in step and (call.row_count or 0) < step["min_rows"]:
        return False
    return not _subset(step.get("args_subset", {}), call.args, "args")


def check_sequence(expected: list[dict[str, Any]], trace: Trace) -> list[str]:
    """Ordered subsequence match.

    A model may legitimately insert a reconnaissance call; requiring exactness
    would make the suite fail on behaviour that is not a regression.
    """
    remaining = list(trace.calls)
    problems: list[str] = []
    for index, step in enumerate(expected):
        for position, call in enumerate(remaining):
            if _matches(step, call):
                remaining = remaining[position + 1 :]
                break
        else:
            actual = (
                ", ".join(f"{c.tool}({'ok' if c.ok else c.error})" for c in trace.calls)
                or "no calls"
            )
            problems.append(f"tool_sequence[{index}] {step} not found. Ran: {actual}")
    return problems


def _numbers(text: str) -> list[float]:
    """Every number in the answer, with thousands separators and currency stripped."""
    found: list[float] = []
    for raw in re.findall(r"-?\$?\d[\d,]*\.?\d*", text):
        try:
            found.append(float(raw.replace("$", "").replace(",", "")))
        except ValueError:
            continue
    return found


def check_answer(expected: dict[str, Any], trace: Trace) -> list[str]:
    problems: list[str] = []
    answer = trace.answer

    for pattern in expected.get("must_match", []):
        if not _search(pattern, answer):
            problems.append(f"answer does not match {pattern}: {answer[:160]!r}")
    for pattern in expected.get("must_not_match", []):
        if _search(pattern, answer):
            problems.append(f"answer should not match {pattern}: {answer[:160]!r}")

    cites = expected.get("cites_result")
    if cites:
        problems += _check_citation(cites, trace)
    return problems


def _check_citation(cites: dict[str, Any], trace: Trace) -> list[str]:
    """Assert the answer quotes a number the run actually computed.

    This is what separates "the tools worked" from "the model reported what the
    tools returned". A plausible-but-invented figure passes every other check in
    this file.
    """
    index = cites["call"]
    if index >= len(trace.calls):
        return [f"cites_result: no call at index {index}"]
    call = trace.calls[index]
    if call.result is None:
        return [f"cites_result: call {index} ({call.tool}) produced no result"]

    rows = call.result.get("rows") or []
    if cites["row"] >= len(rows):
        return [f"cites_result: result of call {index} has no row {cites['row']}"]
    value = rows[cites["row"]][cites["column"]]
    if not isinstance(value, int | float):
        return [f"cites_result: value {value!r} is not numeric"]

    tolerance = cites.get("tolerance_pct", 1.0) / 100
    quoted = _numbers(trace.answer)
    if any(abs(number - value) <= max(abs(value) * tolerance, 0.005) for number in quoted):
        return []
    return [
        f"answer does not quote the computed value {value!r}; "
        f"numbers present: {quoted}. The model narrated a figure it did not compute."
    ]


def _search(pattern: str, text: str) -> bool:
    """Support `/regex/flags` as well as a plain substring."""
    if pattern.startswith("/"):
        body, _, flags = pattern[1:].rpartition("/")
        return re.search(body, text, re.IGNORECASE if "i" in flags else 0) is not None
    return pattern in text


def check(question: dict[str, Any], trace: Trace, denied: set[str]) -> list[str]:
    expect = question.get("expect", {})
    problems = check_sequence(expect.get("tool_sequence", []), trace)
    problems += check_answer(expect.get("answer", {}), trace)

    if "max_tool_calls" in expect and len(trace.calls) > expect["max_tool_calls"]:
        problems.append(
            f"ran {len(trace.calls)} tool call(s), at most {expect['max_tool_calls']} allowed"
        )

    for column in expect.get("forbidden_columns", []):
        for call in trace.calls:
            if call.ok and column in str(call.args):
                problems.append(f"a successful call referenced forbidden column {column!r}")

    expected_denials = set(expect.get("denied_columns", []))
    if expected_denials and not expected_denials <= denied:
        problems.append(
            f"audit log recorded denials {sorted(denied)}, expected {sorted(expected_denials)}"
        )
    return problems
