"""The development login endpoint.

Explicitly a demo affordance: in ``LEDGER_AUTH_MODE=dev`` it mints a token for
any requested role with no password, so the role switcher in the UI works and
Playwright can drive both roles. In ``strict`` it is not mounted at all and
tokens must come from a real issuer.

The distinction that matters for the RBAC story is not how the token is
obtained, it is that everything downstream trusts only a *signed* one.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from ledger.config import AuthMode, Settings, get_settings
from ledger.security import jwt as jwt_helper
from ledger.security.principal import Role

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role = Role.ANALYST
    #: ``None`` means "all tenants". Any value scopes every query to it.
    tenant_id: int | None = None


class TokenResponse(BaseModel):
    access_token: str
    # Not a secret: the literal OAuth 2.0 token type, required by the response shape.
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int
    role: Role
    tenant_id: int | None
    demo: bool


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    settings: Settings = get_settings()
    if settings.auth_mode is not AuthMode.DEV:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="dev login is disabled; obtain a token from your identity provider",
        )

    subject = f"demo-{body.role.value}"
    token, expires_in = jwt_helper.issue(
        settings, subject=subject, role=body.role, tenant_id=body.tenant_id
    )
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        role=body.role,
        tenant_id=body.tenant_id,
        demo=True,
    )
