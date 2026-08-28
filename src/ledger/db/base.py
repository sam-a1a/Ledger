"""The relational schema behind accounts and conversations.

This is *application* state, deliberately separate from the analytical store.
DuckDB serves the dataset read-only and in-memory over parquet, because that is
what lets the API, the MCP server, and the audit consumer coexist. Accounts and
conversations are mutable, related, and need real constraints, so they live in
Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    """Time-ordered, so rows cluster by creation and indexes stay tidy."""
    # uuid.uuid7 landed in Python 3.14; typeshed has not caught up.
    generated: uuid.UUID = uuid.uuid7()  # type: ignore[attr-defined,unused-ignore]
    return generated.hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)

    #: Stored lowercase. Case-sensitive emails are a support burden and an
    #: account-duplication path.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    #: Null for an account created purely through an OAuth provider, which has
    #: no password to verify.
    password_hash: Mapped[str | None] = mapped_column(String(255), default=None)

    display_name: Mapped[str] = mapped_column(String(80))
    #: A filename under the avatar directory, never a path from the client.
    avatar_filename: Mapped[str | None] = mapped_column(String(120), default=None)

    #: Granted, not chosen. The request never carries a role.
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    tenant_id: Mapped[int | None] = mapped_column(Integer, default=None)

    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    identities: Mapped[list[OAuthIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    __table_args__ = (
        # Keyed on the provider's subject, never the email: emails change, and
        # matching on them lets a reassigned address inherit an account.
        UniqueConstraint("provider", "subject", name="oauth_provider_subject"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="identities")


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: The token is never stored. A leaked database must not yield working
    #: reset links, which is the same reasoning as never storing a password.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # The list view is "mine, not archived, newest first".
        Index("conversations_by_owner", "user_id", "archived_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    title: Mapped[str] = mapped_column(String(200))
    #: Snapshot of the role in force when the conversation started. A later
    #: change must not retroactively alter what a past conversation was allowed
    #: to see.
    role: Mapped[str] = mapped_column(String(20))
    tenant_id: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.seq",
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="message_seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))

    #: The Anthropic wire shape verbatim -- text, thinking, and tool_use blocks
    #: alike -- because the agent loop replays it unchanged and thinking blocks
    #: must survive the round trip.
    content: Mapped[Any] = mapped_column(JSONB)
    #: What the user actually sees. Kept separately so rendering a transcript
    #: does not mean re-deriving it from content blocks.
    rendered: Mapped[str | None] = mapped_column(Text, default=None)
    trace: Mapped[Any] = mapped_column(JSONB, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


__all__ = [
    "Base",
    "Conversation",
    "Message",
    "OAuthIdentity",
    "PasswordReset",
    "User",
    "func",
    "new_id",
    "utcnow",
]
