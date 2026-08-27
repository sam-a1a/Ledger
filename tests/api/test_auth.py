"""Token issuance and verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from ledger.config import AuthMode, Settings
from ledger.security.jwt import ISSUER, TokenError, issue, verify
from ledger.security.principal import Role

pytestmark = []


@pytest.fixture
def auth_settings() -> Settings:
    return Settings(
        jwt_secret="test-signing-key-long-enough-for-hs256-rfc7518",
        auth_mode=AuthMode.DEV,
    )


def test_a_token_round_trips_to_a_principal(auth_settings: Settings) -> None:
    token, expires_in = issue(auth_settings, subject="u-1", role=Role.ANALYST, tenant_id=2)
    principal = verify(auth_settings, token)
    assert principal.subject == "u-1"
    assert principal.role is Role.ANALYST
    assert principal.tenant_id == 2
    assert expires_in > 0


def test_a_tampered_token_is_rejected(auth_settings: Settings) -> None:
    """The whole point of signing: a client cannot promote itself."""
    token, _ = issue(auth_settings, subject="u", role=Role.VIEWER)
    forged = jwt.encode(
        {**jwt.decode(token, options={"verify_signature": False}), "role": "analyst"},
        "a-different-signing-key-also-long-enough-for-hs256",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        verify(auth_settings, forged)


def test_an_expired_token_is_rejected(auth_settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    expired = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "u",
            "role": "analyst",
            "iat": int(past.timestamp()),
            "exp": int(past.timestamp()),
        },
        auth_settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError, match="expired"):
        verify(auth_settings, expired)


def test_a_token_from_another_issuer_is_rejected(auth_settings: Settings) -> None:
    now = datetime.now(UTC)
    foreign = jwt.encode(
        {
            "iss": "somebody-else",
            "sub": "u",
            "role": "analyst",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        auth_settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        verify(auth_settings, foreign)


def test_a_token_without_a_role_is_rejected_not_defaulted(
    auth_settings: Settings,
) -> None:
    """Quietly treating it as a viewer would hide a broken issuer."""
    now = datetime.now(UTC)
    roleless = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "u",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        auth_settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        verify(auth_settings, roleless)


def test_an_unknown_role_is_rejected(auth_settings: Settings) -> None:
    now = datetime.now(UTC)
    bogus = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "u",
            "role": "superuser",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        auth_settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError, match="unknown role"):
        verify(auth_settings, bogus)
