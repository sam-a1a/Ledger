"""Account behaviour, with the security properties asserted explicitly."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.accounts import passwords, service
from ledger.config import get_settings
from ledger.db.base import PasswordReset, User
from ledger.security.principal import Role

pytestmark = pytest.mark.postgres

PASSWORD = "correct-horse-battery"


async def test_the_first_account_is_an_analyst_and_the_rest_are_not(
    session: AsyncSession,
) -> None:
    """A fresh deployment needs someone who can see everything; nobody else does."""
    first = await service.sign_up(session, email="first@example.com", password=PASSWORD)
    second = await service.sign_up(session, email="second@example.com", password=PASSWORD)
    assert first.role == Role.ANALYST
    assert second.role == Role.VIEWER


async def test_email_is_normalised(session: AsyncSession) -> None:
    """Case-sensitive emails duplicate accounts and confuse everyone."""
    user = await service.sign_up(session, email="  Sam@Example.COM ", password=PASSWORD)
    assert user.email == "sam@example.com"
    assert await service.get_by_email(session, "SAM@EXAMPLE.COM") is not None


async def test_signing_up_twice_is_refused(session: AsyncSession) -> None:
    await service.sign_up(session, email="dup@example.com", password=PASSWORD)
    with pytest.raises(service.EmailTakenError):
        await service.sign_up(session, email="DUP@example.com", password=PASSWORD)


async def test_authenticate_accepts_the_right_password(session: AsyncSession) -> None:
    await service.sign_up(session, email="ok@example.com", password=PASSWORD)
    assert await service.authenticate(session, email="ok@example.com", password=PASSWORD)


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("ok@example.com", "the-wrong-password"),
        ("nobody@example.com", PASSWORD),
        ("not-an-email", PASSWORD),
    ],
    ids=["wrong password", "no such account", "malformed address"],
)
async def test_authenticate_refuses_everything_else(
    session: AsyncSession, email: str, password: str
) -> None:
    await service.sign_up(session, email="ok@example.com", password=PASSWORD)
    assert await service.authenticate(session, email=email, password=password) is None


async def test_an_unknown_account_costs_the_same_as_a_wrong_password(
    session: AsyncSession,
) -> None:
    """Otherwise the sign-in form is an account-enumeration oracle.

    Timing is compared as a ratio rather than an absolute, since the hashing
    cost is deliberately machine-dependent.
    """
    await service.sign_up(session, email="timed@example.com", password=PASSWORD)

    start = time.perf_counter()
    await service.authenticate(session, email="timed@example.com", password="wrong-one-x")
    wrong_password = time.perf_counter() - start

    start = time.perf_counter()
    await service.authenticate(session, email="ghost@example.com", password="wrong-one-x")
    no_account = time.perf_counter() - start

    ratio = max(wrong_password, no_account) / max(min(wrong_password, no_account), 1e-9)
    assert ratio < 3.0, f"timing differs by {ratio:.1f}x, which leaks account existence"


async def test_a_short_password_is_refused(session: AsyncSession) -> None:
    with pytest.raises(passwords.PasswordError):
        await service.sign_up(session, email="short@example.com", password="tiny")


async def test_password_reset_is_single_use(session: AsyncSession) -> None:
    await service.sign_up(session, email="reset@example.com", password=PASSWORD)
    issued = await service.begin_password_reset(session, email="reset@example.com")
    assert issued is not None
    _, token = issued

    assert await service.complete_password_reset(
        session, token=token, new_password="a-brand-new-password"
    )
    # The same link must not work twice.
    assert (
        await service.complete_password_reset(
            session, token=token, new_password="yet-another-password"
        )
        is None
    )
    assert await service.authenticate(
        session, email="reset@example.com", password="a-brand-new-password"
    )


async def test_the_reset_token_is_never_stored(session: AsyncSession) -> None:
    """A leaked database must not yield working reset links."""
    await service.sign_up(session, email="hash@example.com", password=PASSWORD)
    issued = await service.begin_password_reset(session, email="hash@example.com")
    assert issued is not None
    _, token = issued

    rows = (await session.execute(select(PasswordReset))).scalars().all()
    assert rows
    assert all(row.token_hash != token for row in rows)
    assert all(token not in row.token_hash for row in rows)


async def test_requesting_a_reset_a_second_time_voids_the_first(
    session: AsyncSession,
) -> None:
    """Or a stolen older email stays usable after the owner reacts."""
    await service.sign_up(session, email="twice@example.com", password=PASSWORD)
    first = await service.begin_password_reset(session, email="twice@example.com")
    second = await service.begin_password_reset(session, email="twice@example.com")
    assert first and second

    assert (
        await service.complete_password_reset(
            session, token=first[1], new_password="password-from-first"
        )
        is None
    )
    assert await service.complete_password_reset(
        session, token=second[1], new_password="password-from-second"
    )


async def test_an_expired_reset_token_is_refused(session: AsyncSession) -> None:
    await service.sign_up(session, email="stale@example.com", password=PASSWORD)
    issued = await service.begin_password_reset(session, email="stale@example.com")
    assert issued is not None
    _, token = issued

    reset = (await session.execute(select(PasswordReset))).scalar_one()
    reset.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()

    assert (
        await service.complete_password_reset(
            session, token=token, new_password="too-late-for-this"
        )
        is None
    )


async def test_requesting_a_reset_for_an_unknown_address_reveals_nothing(
    session: AsyncSession,
) -> None:
    assert await service.begin_password_reset(session, email="ghost@example.com") is None


async def test_changing_a_password_requires_the_current_one(
    session: AsyncSession,
) -> None:
    """A hijacked session should not be able to lock the owner out."""
    user = await service.sign_up(session, email="change@example.com", password=PASSWORD)
    assert not await service.change_password(
        session, user, current="not-the-password", new="a-new-password-here"
    )
    assert await service.change_password(session, user, current=PASSWORD, new="a-new-password-here")


async def test_deleting_an_account_removes_it(session: AsyncSession) -> None:
    user = await service.sign_up(session, email="gone@example.com", password=PASSWORD)
    await service.delete_account(session, user)
    await session.flush()
    assert await service.get_by_email(session, "gone@example.com") is None
    assert (await session.execute(select(User))).scalars().all() == []


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("ops@example.com", True),
        ("OPS@example.com", True),  # entries are matched case-insensitively
        ("someone@staff.example.com", True),  # domain entry
        ("someone@other.example.com", False),
        ("ops@evil.com", False),
        # A subdomain must not inherit the parent domain's grant: anyone who
        # can register `staff.example.com.evil.net` would otherwise promote
        # themselves.
        ("someone@sub.staff.example.com", False),
        ("", False),
    ],
)
def test_the_analyst_allowlist_matches_addresses_and_domains_only(
    address: str, expected: bool
) -> None:
    allowlist = ("ops@example.com", "@staff.example.com", "  ")
    assert service.matches_allowlist(address, allowlist) is expected


@pytest.mark.postgres
async def test_an_allowlisted_address_is_an_analyst_however_late_it_signs_up(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule a deployment actually needs: promotion without a database shell.

    Signup order decides the role only when nothing else does, so a deployment
    is not forced to care about who reaches the form first.
    """
    await service.sign_up(session, email="first@example.com", password=PASSWORD)

    settings = get_settings()
    monkeypatch.setattr(settings, "analyst_emails", ("@staff.example.com",), raising=False)

    late = await service.sign_up(session, email="late@staff.example.com", password=PASSWORD)
    assert late.role == Role.ANALYST

    other = await service.sign_up(session, email="other@example.com", password=PASSWORD)
    assert other.role == Role.VIEWER
