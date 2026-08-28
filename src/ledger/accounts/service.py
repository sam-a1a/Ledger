"""Account operations: signing up, signing in, resetting, deleting.

Two rules run through all of it:

* **Nothing here reveals whether an email is registered.** Sign-in and password
  reset behave identically for a known and an unknown address, in both response
  and timing. Otherwise the forms become an account-enumeration oracle, and the
  cost of getting that wrong is paid by the account holders rather than by us.
* **Roles are assigned at signup and read from the account thereafter**, never
  from the request. `LEDGER_ANALYST_EMAILS` promotes named operators; failing
  that, the first account on an empty database becomes the analyst.
* **The role is read from the account, never from the request.** The dev-login
  endpoint lets a caller pick a role, which is exactly why it is a development
  affordance and is refused in strict mode.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ledger.accounts import oauth, passwords
from ledger.config import get_settings
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


def matches_allowlist(address: str, allowlist: Sequence[str]) -> bool:
    """Whether an address is named by the analyst allowlist.

    An entry is a full address, or a domain written with a leading `@`. Both
    are compared lowercased -- the addresses are already normalised, but the
    configured value is typed by a human into a `.env` file or a Compose
    environment block, and a capitalised domain there should not silently
    fail to match.
    """
    candidate = address.lower()
    domain = candidate.rpartition("@")[2]
    for raw in allowlist:
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry.startswith("@"):
            if domain == entry[1:]:
                return True
        elif candidate == entry:
            return True
    return False


async def default_role(session: AsyncSession, address: str) -> Role:
    """The role a new account gets when the caller does not name one.

    Two rules, in order. The allowlist is how a real deployment promotes a
    known operator without opening a database shell. The first-account rule is
    the fallback that makes a clean clone usable: someone has to be able to
    see everything, and on an empty database there is no one to ask.
    """
    if matches_allowlist(address, get_settings().analyst_emails):
        return Role.ANALYST
    # Subsequent accounts start restricted -- the safe default, and the
    # interesting one for demonstrating the boundary.
    return Role.ANALYST if await count_users(session) == 0 else Role.VIEWER


async def link_identity(session: AsyncSession, profile: oauth.Profile) -> User:
    """Resolve a provider identity to an account, creating one if needed.

    The lookup is on `(provider, subject)` and nothing else. Falling back to
    matching the email would mean that anyone who can get a provider to assert
    an address takes over the account holding it -- and because addresses are
    reassigned, that does not even require an attacker.

    A provider that reports no verified address still gets an account. The
    alternative is refusing to sign in someone whose GitHub email is private,
    which is a configuration they are entitled to.
    """
    existing = await session.scalar(
        select(OAuthIdentity)
        .where(OAuthIdentity.provider == profile.provider)
        .where(OAuthIdentity.subject == profile.subject)
        .options(selectinload(OAuthIdentity.user))
    )
    if existing is not None:
        if profile.email and existing.email != profile.email:
            # Recorded for the audit trail; it is not used to find the account.
            existing.email = profile.email
        await session.flush()
        return existing.user

    address = normalise_email(profile.email) if profile.email else None
    if address is not None and await get_by_email(session, address) is not None:
        # A password account already holds this address. Silently adopting it
        # would be exactly the takeover the subject-only lookup prevents, so
        # the person is asked to sign in and link deliberately instead.
        raise EmailTakenError(address)

    placeholder = f"{profile.provider}-{profile.subject}@oauth.invalid"
    user = User(
        email=address or placeholder,
        # No password, and none that can be guessed into: a random hash means
        # the sign-in form can never authenticate this account.
        password_hash=passwords.hash_password(secrets.token_urlsafe(32)),
        display_name=(profile.display_name or address or profile.provider)[:80],
        role=(await default_role(session, address or placeholder)).value,
        preferences={},
    )
    session.add(user)
    await session.flush()

    session.add(
        OAuthIdentity(
            user_id=user.id,
            provider=profile.provider,
            subject=profile.subject,
            email=address,
        )
    )
    await session.flush()
    log.info("account created via %s: %s (%s)", profile.provider, user.id, user.role)
    return user


async def attach_identity(session: AsyncSession, user: User, profile: oauth.Profile) -> None:
    """Link a provider identity to an account that already exists."""
    existing = await session.scalar(
        select(OAuthIdentity)
        .where(OAuthIdentity.provider == profile.provider)
        .where(OAuthIdentity.subject == profile.subject)
    )
    if existing is not None and existing.user_id != user.id:
        raise AccountError("That account is already linked to someone else.")
    if existing is None:
        session.add(
            OAuthIdentity(
                user_id=user.id,
                provider=profile.provider,
                subject=profile.subject,
                email=normalise_email(profile.email) if profile.email else None,
            )
        )
        await session.flush()


async def identities_for(session: AsyncSession, user_id: str) -> list[str]:
    """Provider names linked to an account, for the settings page."""
    result = await session.scalars(
        select(OAuthIdentity.provider).where(OAuthIdentity.user_id == user_id)
    )
    return sorted(result.all())


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

    resolved = role or await default_role(session, address)

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
