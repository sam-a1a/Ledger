"""The wire contract, asserted against raw bytes.

Parsing the frames rather than using a client helper is deliberate: the frame
format *is* the contract with the frontend, and a helper would paper over a
malformed one.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.kafka


async def collect(
    client: httpx.AsyncClient, token: str, message: str
) -> list[tuple[str, dict[str, Any]]]:
    """Read a whole SSE response into (event, data) pairs."""
    frames: list[tuple[str, dict[str, Any]]] = []
    async with client.stream(
        "POST",
        "/api/chat",
        json={"message": message},
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        timeout=60.0,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"

        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                if raw.startswith(":"):
                    continue  # keepalive
                name = data = None
                for line in raw.splitlines():
                    if line.startswith("event: "):
                        name = line.removeprefix("event: ")
                    elif line.startswith("data: "):
                        data = line.removeprefix("data: ")
                if name and data:
                    frames.append((name, json.loads(data)))
    return frames


async def test_a_question_streams_a_well_formed_conversation(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    frames = await collect(client, analyst_token, "which zones are busiest?")
    names = [name for name, _ in frames]

    assert names[0] == "meta"
    assert names[-1] == "done"
    assert "token" in names


async def test_sequence_numbers_are_monotonic(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    """A client uses these to detect a gap; a test uses them to assert ordering."""
    frames = await collect(client, analyst_token, "which zones are busiest?")
    seqs = [data["seq"] for _, data in frames]
    assert seqs == sorted(seqs)
    assert seqs == list(range(1, len(seqs) + 1))


async def test_every_tool_call_start_has_a_matching_end(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    frames = await collect(client, analyst_token, "which zones are busiest?")
    starts = {d["call_id"] for n, d in frames if n == "tool_call_start"}
    ends = {d["call_id"] for n, d in frames if n == "tool_call_end"}
    assert starts and starts == ends


async def test_a_chart_carries_its_own_rows(client: httpx.AsyncClient, analyst_token: str) -> None:
    """The frontend renders from these, so they travel with the spec."""
    frames = await collect(client, analyst_token, "which zones are busiest?")
    charts = [d for n, d in frames if n == "chart"]
    assert charts
    assert charts[0]["spec"]["kind"] == "bar"
    assert charts[0]["rows"]
    assert [c["name"] for c in charts[0]["columns"]]


async def test_meta_reports_demo_mode_honestly(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    """The UI shows a banner from this; hiding it would be the dishonest choice."""
    frames = await collect(client, analyst_token, "how many trips are there?")
    meta = next(d for n, d in frames if n == "meta")
    assert meta["model_backend"] == "fake"
    assert meta["demo_mode"] is True
    assert meta["role"] == "analyst"


async def test_there_is_no_trace_event(client: httpx.AsyncClient, analyst_token: str) -> None:
    """The trace is derived from the call pairs, not sent again at the end.

    A terminal trace event would be a second source of truth, and it would
    arrive after the answer -- so a stream that died mid-answer would lose it
    exactly when it was most wanted.
    """
    frames = await collect(client, analyst_token, "which zones are busiest?")
    assert "trace" not in {name for name, _ in frames}


async def test_a_viewer_is_refused_over_the_wire(
    client: httpx.AsyncClient, viewer_token: str
) -> None:
    frames = await collect(client, viewer_token, "what is the average tip?")
    ends = [d for n, d in frames if n == "tool_call_end"]
    assert any(not e["ok"] and e["error_code"] == "unknown_column" for e in ends)

    answer = "".join(d["text"] for n, d in frames if n == "token")
    assert "do not have" in answer or "not available" in answer

    # The name does appear in the stream -- in the arguments the caller sent and
    # in the error echoing them back. That is a 404 repeating the path, not
    # disclosure: the caller supplied the string, so nothing is learned from
    # seeing it returned. What must not leak is any signal that the column is
    # real but withheld, so those are what get asserted.
    refusal = next(d for n, d in frames if n == "tool_call_end" and not d["ok"])
    assert refusal["error_code"] == "unknown_column"  # identical to a typo
    for word in ("restricted", "permission", "forbidden", "denied", "not allowed"):
        assert word not in refusal["message"].lower(), word
        assert word not in answer.lower(), word

    # ...and no hidden column is ever offered as a correction.
    payload = json.dumps(frames)
    for hidden in ("cbd_congestion_fee", "tolls_amount", "improvement_surcharge"):
        assert hidden not in payload


async def test_requests_without_a_token_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"message": "hello"})
    assert response.status_code == 401


async def test_a_garbage_token_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/chat",
        json={"message": "hello"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401
