"""Listing, resuming, archiving, and deleting conversations.

Ownership is applied in the query rather than checked in the handler, so a
conversation belonging to another account returns 404 rather than 403 — the
same non-probeable posture the column layer uses. A 403 would confirm the
conversation exists, which is exactly the thing not to confirm.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ledger.api.deps import SessionDep, UserDep
from ledger.conversations import service as conversations
from ledger.db.base import Conversation

router = APIRouter(tags=["conversations"])


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    archived: bool
    message_count: int = 0


class TraceEntry(BaseModel):
    model_config = ConfigDict(extra="allow")


class TranscriptMessage(BaseModel):
    role: str
    text: str
    trace: list[dict[str, object]] = Field(default_factory=list)
    created_at: datetime


class TranscriptResponse(BaseModel):
    conversation: ConversationSummary
    messages: list[TranscriptMessage]


class ConversationList(BaseModel):
    conversations: list[ConversationSummary]


class UpdateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


def _summary(conversation: Conversation, message_count: int = 0) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        archived=conversation.archived_at is not None,
        message_count=message_count,
    )


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    user: UserDep,
    session: SessionDep,
    archived: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> ConversationList:
    found = await conversations.list_for(session, user.id, archived=archived, limit=limit)
    counts = await conversations.message_counts(session, [c.id for c in found])
    return ConversationList(conversations=[_summary(c, counts.get(c.id, 0)) for c in found])


@router.get("/conversations/{conversation_id}", response_model=TranscriptResponse)
async def get_conversation(
    conversation_id: str, user: UserDep, session: SessionDep
) -> TranscriptResponse:
    conversation = await conversations.transcript(session, conversation_id, user.id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation.")

    messages = [
        TranscriptMessage(
            role=message.role,
            # `rendered` is what the person saw. Falling back to the raw content
            # blocks would show tool_use JSON in the transcript.
            text=message.rendered or (message.content if isinstance(message.content, str) else ""),
            trace=list(message.trace or []),
            created_at=message.created_at,
        )
        for message in conversation.messages
    ]
    return TranscriptResponse(conversation=_summary(conversation, len(messages)), messages=messages)


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummary)
async def update_conversation(
    conversation_id: str, body: UpdateConversation, user: UserDep, session: SessionDep
) -> ConversationSummary:
    conversation = await conversations.get(session, conversation_id, user.id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation.")

    if body.title is not None:
        await conversations.rename(session, conversation_id, user.id, body.title)
    if body.archived is not None:
        await conversations.set_archived(session, conversation_id, user.id, archived=body.archived)

    await session.flush()
    await session.refresh(conversation)
    return _summary(conversation)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: str, user: UserDep, session: SessionDep) -> Response:
    """Delete a conversation. The audit log is untouched.

    The transcript belongs to the person; the record of what was queried
    belongs to the organisation. `/api/audit` still shows every call this
    conversation made, and the UI says so before you confirm.
    """
    if not await conversations.remove(session, conversation_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
