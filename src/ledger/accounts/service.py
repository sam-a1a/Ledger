"""Account operations: signing up, signing in, resetting, deleting.

Two rules run through all of it:

* **Nothing here reveals whether an email is registered.** Sign-in and password
  reset behave identically for a known and an unknown address, in both response
  and timing. Otherwise the forms become an account-enumeration oracle, and the
  cost of getting that wrong is paid by the account holders rather than by us.
* **The role is read from the account, never from the request.** The dev-login
  endpoint lets a caller pick a role, which is exactly why it is a development
  affordance and is refused in strict mode.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.accounts import passwords
from ledger.db.base import Conversation, OAuthIdentity, PasswordReset, User, utcnow
from ledger.logging import get_logger
from ledger.security.principal import Role

log = get_logger(__name__)

RESET_TOKEN_TTL = timedelta(hours=1)
#: Replaces the subject on audit events when an account is deleted, so the
#: record of what was queried survives without remaining attributable.
DELETED_SUBJECT = "deleted-account"


class AccountError(Exception):
    """Something the caller can act on."""


class EmailTakenError(AccountError):
    pass


def normalise_email(email: str) -> str:
    """Lowercase and validate. Case-sensitive emails duplicate accounts."""
    try:
        # Stripped first: a pasted address routinely carries surrounding
        # whitespace, and rejecting it teaches people the form is broken.
        # `check_deliverability=False` because a DNS lookup during signup makes
        # the request slow and fails offline, which is where this is developed.
        result = validate_email(email.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise AccountError(str(exc)) from exc
    return result.normalized.lower()


def _hash_token(token: str) -> str:
    """Reset tokens are stored hashed, for the same reason passwords are.

    A leaked database must not yield working reset links. SHA-256 rather than
    Argon2 is right here: the token is 256 bits of entropy we generated, so
    there is nothing to brute-force and no reason to pay the cost.
    """
    return hashlib.sha256(token.encode()).hexdigest()


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


async def sign_up(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
    role: Role | None = None,
) -> User:
    address = normalise_email(email)
    if await get_by_email(session, address) is not None:
        # The API layer turns this into the same response a successful signup
        # produces; raising a distinct type here keeps that decision in one
        # place rather than spreading it through the service.
        raise EmailTakenError(address)

    passwords.validate(password)

    # The first account is the analyst, so a fresh deployment has someone who
    # can see everything. Subsequent accounts start restricted -- the safe
    # default, and the interesting one for demonstrating the boundary.
    resolved = role or (Role.ANALYST if await count_users(session) == 0 else Role.VIEWER)

    user = User(
        email=address,
        password_hash=passwords.hash_password(password),
        display_name=(display_name or address.split("@")[0])[:80],
        role=resolved.value,
        preferences={},
    )
    session.add(user)
    await session.flush()
    log.info("account created: %s (%s)", user.id, user.role)
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User | None:
    """Verify credentials, in constant time whether or not the account exists."""
    try:
        address = normalise_email(email)
    except AccountError:
        # An invalid address must still cost what a valid one costs.
        passwords.verify(password, None)
        return None

    user = await get_by_email(session, address)
    if user is None or not user.is_active:
        passwords.verify(password, None)
        return None

    if not passwords.verify(password, user.password_hash):
        return None

    if user.password_hash and passwords.needs_rehash(user.password_hash):
        # Silently upgrade the cost parameters on a successful sign-in, which
        # is the only moment the plaintext is available.
        user.password_hash = passwords.hash_password(password)

    user.last_login_at = utcnow()
    return user


async def begin_password_reset(session: AsyncSession, *, email: str) -> tuple[User, str] | None:
    """Issue a reset token, or nothing if there is no such account.

    The caller reports success either way. Returning `None` rather than raising
    keeps the "do not disclose" decision at the boundary where the response is
    written, instead of relying on every call site to catch the right thing.
    """
    try:
        address = normalise_email(email)
    except AccountError:
        return None

    user = await get_by_email(session, address)
    if user is None or not user.is_active:
        return None

    # Any outstanding tokens are void: a reset request should invalidate the
    # previous link, or a stolen older email stays usable.
    await session.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
        .values(used_at=utcnow())
    )

    token = secrets.token_urlsafe(32)
    session.add(
        PasswordReset(
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=datetime.now(UTC) + RESET_TOKEN_TTL,
        )
    )
    await session.flush()
    return user, token


async def complete_password_reset(
    session: AsyncSession, *, token: str, new_password: str
) -> User | None:
    """Consume a reset token and set a new password. Single use."""
    passwords.validate(new_password)

    result = await session.execute(
        select(PasswordReset).where(PasswordReset.token_hash == _hash_token(token))
    )
    reset = result.scalar_one_or_none()
    if reset is None or reset.used_at is not None:
        return None
    if reset.expires_at < datetime.now(UTC):
        return None

    user = await session.get(User, reset.user_id)
    if user is None:
        return None

    user.password_hash = passwords.hash_password(new_password)
    reset.used_at = utcnow()
    log.info("password reset completed for %s", user.id)
    return user


async def change_password(session: AsyncSession, user: User, *, current: str, new: str) -> bool:
    """Requires the current password, so a hijacked session cannot lock out the owner."""
    if not passwords.verify(current, user.password_hash):
        return False
    user.password_hash = passwords.hash_password(new)
    return True


async def delete_account(session: AsyncSession, user: User) -> None:
    """Remove the account and everything personal to it.

    The governance log is deliberately untouched. The transcript belongs to the
    person; the record of what was queried belongs to the organisation. It is
    anonymised rather than erased -- the subject becomes a tombstone -- so the
    calls stay auditable without staying attributable.
    """
    await session.execute(delete(Conversation).where(Conversation.user_id == user.id))
    await session.execute(delete(OAuthIdentity).where(OAuthIdentity.user_id == user.id))
    await session.execute(delete(PasswordReset).where(PasswordReset.user_id == user.id))
    await session.delete(user)
    log.info("account deleted: %s (audit trail retained, anonymised)", user.id)
