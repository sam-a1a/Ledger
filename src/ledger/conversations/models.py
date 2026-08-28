"""What a stored conversation looks like."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Turns kept when reconstructing history for the model. A conversation can be
#: arbitrarily long; the prompt cannot.
DEFAULT_HISTORY_TURNS = 12

#: Titles are derived from the opening question, the way a person would name it.
MAX_TITLE_CHARS = 70


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    #: Snapshot of who created it. A later role change must not retroactively
    #: alter who a past conversation belonged to.
    subject: str
    role: str
    tenant_id: int | None = None

    created_at: datetime
    updated_at: datetime
    archived: bool = False
    message_count: int = 0


class StoredMessage(BaseModel):
    """One turn, in the shape the agent loop consumes.

    ``content`` holds the Anthropic wire shape verbatim -- text, thinking, and
    tool_use blocks alike -- because the loop replays it unchanged and thinking
    blocks must survive the round trip.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]
    created_at: datetime | None = None


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation: Conversation
    messages: list[StoredMessage] = Field(default_factory=list)


def derive_title(question: str) -> str:
    """Name a conversation after its opening question.

    Cut on a word boundary rather than mid-word: a list of chats is scanned, not
    read, and a truncated word costs a beat every time the eye passes it.
    """
    cleaned = " ".join(question.split())
    if len(cleaned) <= MAX_TITLE_CHARS:
        return cleaned or "New chat"
    clipped = cleaned[:MAX_TITLE_CHARS]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,.;:") + "…"
