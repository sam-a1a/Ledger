"""Conversation endpoints, with ownership asserted rather than assumed.

The interesting cases are not list-and-rename. They are: another account's
conversation is indistinguishable from one that does not exist, history is
rebuilt from the store rather than accepted from the caller, and deleting a
transcript does not delete the record of what it queried.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from aiokafka import AIOKafkaConsumer

from ledger.governance.events import GovernanceEvent
from ledger.governance.topics import AuditTopics
from tests.api.test_sse_protocol import collect

pytestmark = pytest.mark.kafka

PASSWORD = "correct-horse-battery"


async def _drain(bootstrap: str, topic: str, conversation_id: str) -> list[GovernanceEvent]:
    """Every event on the topic belonging to one conversation."""
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        group_id=None,
    )
    await consumer.start()
    try:
        collected: list[GovernanceEvent] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        while loop.time() < deadline:
            batch = await consumer.getmany(timeout_ms=500)
            if not batch:
                break
            for records in batch.values():
                collected.extend(GovernanceEvent.model_validate_json(r.value) for r in records)
        return [e for e in collected if e.conversation_id == conversation_id]
    finally:
        await consumer.stop()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _second_account(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/accounts/signup",
        json={"email": "other-person@example.com", "password": PASSWORD},
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


async def _conversation_id(client: httpx.AsyncClient, token: str, question: str) -> str:
    frames = await collect(client, token, question)
    meta = next(data for name, data in frames if name == "meta")
    return str(meta["conversation_id"])


async def test_a_conversation_is_listed_with_a_title_from_its_opening_question(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    await _conversation_id(client, analyst_token, "Which pickup zones are busiest?")

    listed = await client.get("/api/conversations", headers=_auth(analyst_token))
    assert listed.status_code == 200
    conversations = listed.json()["conversations"]
    assert len(conversations) == 1
    assert "pickup zones" in conversations[0]["title"].lower()
    assert conversations[0]["message_count"] >= 2


async def test_a_transcript_returns_what_the_person_saw_not_the_wire_blocks(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    conversation_id = await _conversation_id(
        client, analyst_token, "Which pickup zones are busiest?"
    )

    transcript = await client.get(
        f"/api/conversations/{conversation_id}", headers=_auth(analyst_token)
    )
    assert transcript.status_code == 200

    messages = transcript.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["text"]
    # Raw tool_use blocks reaching the transcript is the failure mode here.
    assert "tool_use" not in messages[1]["text"]
    assert messages[1]["trace"], "the trace is stored with the turn, not re-derived"


async def test_a_second_turn_is_appended_to_the_same_conversation(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    """Two turns in one conversation, with sequence numbers that do not collide.

    `seq` was once computed in a statement of its own and then used in the
    INSERT, so a second turn raced the first and failed on the primary key.
    """
    first = await _conversation_id(client, analyst_token, "Which pickup zones are busiest?")
    await collect(
        client,
        analyst_token,
        "What is the average fare by borough?",
        conversation_id=first,
    )

    listed = await client.get("/api/conversations", headers=_auth(analyst_token))
    assert len(listed.json()["conversations"]) == 1

    transcript = await client.get(f"/api/conversations/{first}", headers=_auth(analyst_token))
    assert len(transcript.json()["messages"]) == 4


async def test_another_accounts_conversation_is_indistinguishable_from_a_missing_one(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    """404, not 403. A 403 confirms the id is real, which is the leak."""
    conversation_id = await _conversation_id(
        client, analyst_token, "Which pickup zones are busiest?"
    )
    intruder = await _second_account(client)

    for method, kwargs in (
        ("GET", {}),
        ("PATCH", {"json": {"title": "mine now"}}),
        ("DELETE", {}),
    ):
        response = await client.request(
            method,
            f"/api/conversations/{conversation_id}",
            headers=_auth(intruder),
            **kwargs,
        )
        assert response.status_code == 404, method

    invented = await client.get("/api/conversations/does-not-exist", headers=_auth(intruder))
    assert invented.status_code == 404


async def test_another_accounts_conversation_is_not_listed(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    await _conversation_id(client, analyst_token, "Which pickup zones are busiest?")
    intruder = await _second_account(client)

    listed = await client.get("/api/conversations", headers=_auth(intruder))
    assert listed.json()["conversations"] == []


async def test_renaming_and_archiving_a_conversation(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    conversation_id = await _conversation_id(
        client, analyst_token, "Which pickup zones are busiest?"
    )

    renamed = await client.patch(
        f"/api/conversations/{conversation_id}",
        json={"title": "Zone volumes"},
        headers=_auth(analyst_token),
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Zone volumes"

    archived = await client.patch(
        f"/api/conversations/{conversation_id}",
        json={"archived": True},
        headers=_auth(analyst_token),
    )
    assert archived.json()["archived"] is True

    # Archiving hides it from the default list without destroying it.
    default = await client.get("/api/conversations", headers=_auth(analyst_token))
    assert default.json()["conversations"] == []

    included = await client.get(
        "/api/conversations", params={"archived": True}, headers=_auth(analyst_token)
    )
    assert len(included.json()["conversations"]) == 1

    restored = await client.patch(
        f"/api/conversations/{conversation_id}",
        json={"archived": False},
        headers=_auth(analyst_token),
    )
    assert restored.json()["archived"] is False


async def test_deleting_a_conversation_keeps_the_record_of_what_it_queried(
    client: httpx.AsyncClient,
    analyst_token: str,
    kafka_bootstrap: str,
    audit_topics: AuditTopics,
) -> None:
    """The governance property, stated as a test.

    The transcript belongs to the person and goes. The record of what was
    asked of the data belongs to the organisation and stays -- otherwise
    anyone could erase their own audit trail by tidying up their chat list.

    Asserted against the topic rather than `/api/audit`, because the audit view
    is materialised by the consumer and this fixture does not run one. The
    topic is the source of truth either way; the view is derived from it.
    """
    conversation_id = await _conversation_id(
        client, analyst_token, "Which pickup zones are busiest?"
    )

    topic = audit_topics.tool_calls
    before = await _drain(kafka_bootstrap, topic, conversation_id)
    assert before, "the conversation made no audited calls, so this proves nothing"

    deleted = await client.delete(
        f"/api/conversations/{conversation_id}", headers=_auth(analyst_token)
    )
    assert deleted.status_code == 204

    gone = await client.get(f"/api/conversations/{conversation_id}", headers=_auth(analyst_token))
    assert gone.status_code == 404

    after = await _drain(kafka_bootstrap, topic, conversation_id)
    assert len(after) == len(before)
