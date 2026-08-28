"""Reading and writing conversations.

**Ownership is enforced in the query, never in the handler.** Every read and
every mutation carries the owner, so a route that forgot to check could not
return somebody else's conversation even by accident. A conversation belonging
to another account is indistinguishable from one that does not exist -- the same
non-probeable posture the column layer uses, for the same reason.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ledger.conversations.models import DEFAULT_HISTORY_TURNS, derive_title
from ledger.db.base import Conversation, Message, utcnow
from ledger.logging import get_logger

log = get_logger(__name__)


async def list_for(
    session: AsyncSession, user_id: str, *, archived: bool = False, limit: int = 100
) -> list[Conversation]:
    condition = (
        Conversation.archived_at.is_not(None) if archived else Conversation.archived_at.is_(None)
    )
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id, condition)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def get(session: AsyncSession, conversation_id: str, user_id: str) -> Conversation | None:
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def transcript(
    session: AsyncSession, conversation_id: str, user_id: str
) -> Conversation | None:
    result = await session.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def message_counts(session: AsyncSession, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    result = await session.execute(
        select(Message.conversation_id, func.count())
        .where(Message.conversation_id.in_(ids))
        .group_by(Message.conversation_id)
    )
    return {row[0]: int(row[1]) for row in result}


async def history(
    session: AsyncSession,
    conversation_id: str,
    user_id: str,
    *,
    turns: int = DEFAULT_HISTORY_TURNS,
) -> list[dict[str, Any]]:
    """The messages to replay into the model.

    Bounded, and trimmed to begin on a user turn. A history that starts with an
    assistant message -- or worse, with tool results whose originating tool_use
    was trimmed away -- is rejected by the API, and the error names none of that.
    """
    conversation = await transcript(session, conversation_id, user_id)
    if conversation is None:
        return []

    messages = [{"role": m.role, "content": m.content} for m in conversation.messages]
    if turns > 0:
        messages = messages[-(turns * 2) :]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


async def ensure(
    session: AsyncSession,
    *,
    conversation_id: str | None,
    user_id: str,
    role: str,
    tenant_id: int | None,
    first_question: str,
) -> Conversation:
    """Fetch the conversation, creating it on first use."""
    if conversation_id:
        existing = await get(session, conversation_id, user_id)
        if existing is not None:
            return existing

    conversation = Conversation(
        user_id=user_id,
        title=derive_title(first_question),
        role=role,
        tenant_id=tenant_id,
    )
    if conversation_id:
        conversation.id = conversation_id
    session.add(conversation)
    await session.flush()
    return conversation


async def append(
    session: AsyncSession,
    conversation: Conversation,
    *,
    role: str,
    content: str | list[dict[str, Any]],
    rendered: str | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> Message:
    # The next sequence number is computed *inside* the INSERT rather than
    # read first and used second. Reading it separately is wrong twice: rows
    # still pending in the session are invisible to the SELECT, so several
    # appends before a flush all claim seq 0; and even after flushing, two
    # concurrent writers would read the same value. The unique constraint on
    # (conversation_id, seq) is the backstop that made this visible rather
    # than silently reordering a transcript.
    next_seq = (
        select(func.coalesce(func.max(Message.seq), -1) + 1)
        .where(Message.conversation_id == conversation.id)
        .scalar_subquery()
    )

    message = Message(
        conversation_id=conversation.id,
        seq=next_seq,
        role=role,
        content=content,
        rendered=rendered,
        trace=trace or [],
    )
    session.add(message)
    conversation.updated_at = utcnow()
    return message


async def rename(
    session: AsyncSession, conversation_id: str, user_id: str, title: str
) -> Conversation | None:
    conversation = await get(session, conversation_id, user_id)
    if conversation is None:
        return None
    conversation.title = (title.strip() or "New chat")[:200]
    return conversation


async def set_archived(
    session: AsyncSession, conversation_id: str, user_id: str, *, archived: bool
) -> Conversation | None:
    conversation = await get(session, conversation_id, user_id)
    if conversation is None:
        return None
    conversation.archived_at = utcnow() if archived else None
    return conversation


async def remove(session: AsyncSession, conversation_id: str, user_id: str) -> bool:
    """Delete a conversation and its messages.

    The governance log is untouched, deliberately. A system where deleting a
    chat erases the evidence of what it asked is not a governed one, so
    `/api/audit` still shows every call the conversation made.
    """
    # Fetch first rather than reading `rowcount` off the delete: the typed
    # Result does not expose it, and the ownership check has to happen either
    # way, so this is one query rather than a cast.
    conversation = await get(session, conversation_id, user_id)
    if conversation is None:
        return False
    await session.execute(
        delete(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    deleted = True
    if deleted:
        log.info("conversation %s deleted (audit trail retained)", conversation_id)
    return deleted
