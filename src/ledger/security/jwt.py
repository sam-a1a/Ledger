"""Issuing and verifying the tokens that carry a role.

HS256 with a shared secret, which is right for a single-service demo and is
where a real deployment would swap in an external issuer. The important part is
not the algorithm: it is that the role arrives *signed*, so the tool layer is
handed a principal it can trust rather than a header anyone can set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from ledger.config import Settings
from ledger.security.principal import Channel, Principal, Role

ISSUER = "ledger"


class TokenError(Exception):
    """The token was missing, malformed, expired, or not ours."""


def issue(
    settings: Settings,
    *,
    subject: str,
    role: Role,
    tenant_id: int | None = None,
) -> tuple[str, int]:
    """Mint a token. Returns ``(token, expires_in_seconds)``."""
    now = datetime.now(UTC)
    expires_in = settings.jwt_ttl_seconds
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": subject,
        "role": role.value,
        "tenant_id": tenant_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm), expires_in


def verify(settings: Settings, token: str, *, channel: Channel = Channel.HTTP) -> Principal:
    """Decode a token into a principal, or raise :class:`TokenError`."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=ISSUER,
            # Required rather than defaulted: a token without a role is not a
            # token we can make an access decision from, and quietly treating
            # it as a viewer would hide a broken issuer.
            options={"require": ["exp", "iat", "iss", "sub", "role"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise TokenError("token was not issued by this service") from exc
    except jwt.PyJWTError as exc:
        raise TokenError(f"invalid token: {exc}") from exc

    try:
        role = Role(claims["role"])
    except ValueError as exc:
        raise TokenError(f"unknown role {claims['role']!r}") from exc

    tenant = claims.get("tenant_id")
    return Principal(
        subject=str(claims["sub"]),
        role=role,
        channel=channel,
        tenant_id=int(tenant) if tenant is not None else None,
    )
