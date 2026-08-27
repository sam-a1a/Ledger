"""Conversation persistence, ownership, and the governance boundary."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.accounts import service as accounts
from ledger.conversations import service as conversations
from ledger.conversations.models import derive_title
from ledger.db.base import User

pytestmark = pytest.mark.postgres

PASSWORD = "correct-horse-battery"


async def _user(session: AsyncSession, email: str) -> User:
    return await accounts.sign_up(session, email=email, password=PASSWORD)


async def _conversation(session: AsyncSession, user: User, question: str):  # type: ignore[no-untyped-def]
    return await conversations.ensure(
        session,
        conversation_id=None,
        user_id=user.id,
        role=user.role,
        tenant_id=user.tenant_id,
        first_question=question,
    )


async def test_a_conversation_is_named_after_its_opening_question(
    session: AsyncSession,
) -> None:
    user = await _user(session, "a@example.com")
    conversation = await _conversation(session, user, "Which pickup zones are busiest?")
    assert conversation.title == "Which pickup zones are busiest?"


def test_a_long_question_is_cut_on_a_word_boundary() -> None:
    """A list of chats is scanned, not read; a truncated word costs a beat."""
    title = derive_title("Which pickup zones saw the biggest drop in evening trips " * 3)
    assert title.endswith("…")
    assert "  " not in title
    assert not title[:-1].endswith(" ")


async def test_history_is_returned_in_order_and_bounded(session: AsyncSession) -> None:
    user = await _user(session, "b@example.com")
    conversation = await _conversation(session, user, "first")
    for index in range(10):
        await conversations.append(session, conversation, role="user", content=f"question {index}")
        await conversations.append(
            session, conversation, role="assistant", content=f"answer {index}"
        )
    await session.flush()

    everything = await conversations.history(session, conversation.id, user.id, turns=0)
    assert len(everything) == 20

    bounded = await conversations.history(session, conversation.id, user.id, turns=3)
    assert len(bounded) <= 6
    # A history beginning on an assistant turn is rejected by the model API,
    # and the error names none of that, so the trim has to land on a user turn.
    assert bounded[0]["role"] == "user"


async def test_history_for_someone_elses_conversation_is_empty(
    session: AsyncSession,
) -> None:
    owner = await _user(session, "owner@example.com")
    stranger = await _user(session, "stranger@example.com")
    conversation = await _conversation(session, owner, "mine")
    await conversations.append(session, conversation, role="user", content="secret")
    await session.flush()

    assert await conversations.history(session, conversation.id, owner.id)
    assert await conversations.history(session, conversation.id, stranger.id) == []


async def test_a_conversation_is_invisible_to_another_account(
    session: AsyncSession,
) -> None:
    """Invisible rather than forbidden: a 403 would confirm it exists."""
    owner = await _user(session, "o2@example.com")
    stranger = await _user(session, "s2@example.com")
    conversation = await _conversation(session, owner, "mine")
    await session.flush()

    assert await conversations.get(session, conversation.id, owner.id) is not None
    assert await conversations.get(session, conversation.id, stranger.id) is None
    assert await conversations.list_for(session, stranger.id) == []


async def test_archiving_removes_it_from_the_active_list_without_deleting(
    session: AsyncSession,
) -> None:
    user = await _user(session, "c@example.com")
    conversation = await _conversation(session, user, "archive me")
    await session.flush()

    await conversations.set_archived(session, conversation.id, user.id, archived=True)
    await session.flush()
    assert await conversations.list_for(session, user.id) == []
    assert len(await conversations.list_for(session, user.id, archived=True)) == 1

    await conversations.set_archived(session, conversation.id, user.id, archived=False)
    await session.flush()
    assert len(await conversations.list_for(session, user.id)) == 1


async def test_another_account_cannot_archive_or_delete_it(
    session: AsyncSession,
) -> None:
    owner = await _user(session, "o3@example.com")
    stranger = await _user(session, "s3@example.com")
    conversation = await _conversation(session, owner, "mine")
    await session.flush()

    assert (
        await conversations.set_archived(session, conversation.id, stranger.id, archived=True)
        is None
    )
    assert await conversations.remove(session, conversation.id, stranger.id) is False
    assert await conversations.get(session, conversation.id, owner.id) is not None


async def test_deleting_an_account_takes_its_conversations_with_it(
    session: AsyncSession,
) -> None:
    user = await _user(session, "d@example.com")
    conversation = await _conversation(session, user, "goes away")
    await conversations.append(session, conversation, role="user", content="hello")
    await session.flush()

    await accounts.delete_account(session, user)
    await session.flush()
    assert await conversations.list_for(session, user.id) == []


async def test_messages_keep_their_order_across_appends(session: AsyncSession) -> None:
    user = await _user(session, "e@example.com")
    conversation = await _conversation(session, user, "ordering")
    for index in range(5):
        await conversations.append(session, conversation, role="user", content=f"m{index}")
    await session.flush()

    transcript = await conversations.transcript(session, conversation.id, user.id)
    assert transcript is not None
    assert [m.content for m in transcript.messages] == [f"m{i}" for i in range(5)]
    assert [m.seq for m in transcript.messages] == list(range(5))


async def test_the_role_is_snapshotted_when_the_conversation_starts(
    session: AsyncSession,
) -> None:
    """A later role change must not retroactively alter a past conversation."""
    user = await _user(session, "f@example.com")
    conversation = await _conversation(session, user, "snapshot")
    await session.flush()
    assert conversation.role == user.role

    user.role = "viewer" if user.role == "analyst" else "analyst"
    await session.flush()
    await session.refresh(conversation)
    assert conversation.role != user.role


async def test_sequence_numbers_survive_appends_without_an_intervening_flush(
    session: AsyncSession,
) -> None:
    """The regression: several appends in one session all claimed seq 0.

    `max(seq)` read in a separate statement cannot see rows still pending in
    the session, so a transcript silently reordered — or, thanks to the unique
    constraint, failed loudly. It now computes the next value inside the INSERT.
    """
    user = await _user(session, "seq@example.com")
    conversation = await _conversation(session, user, "sequencing")

    for index in range(6):
        await conversations.append(session, conversation, role="user", content=f"turn {index}")
    await session.flush()

    transcript = await conversations.transcript(session, conversation.id, user.id)
    assert transcript is not None
    assert [m.seq for m in transcript.messages] == list(range(6))
    assert [m.content for m in transcript.messages] == [f"turn {i}" for i in range(6)]
