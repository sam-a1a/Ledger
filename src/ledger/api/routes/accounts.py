"""Sign up, sign in, reset, and delete.

The responses here are deliberately uninformative in one specific way: nothing
reveals whether an email is registered. Sign-in and password reset behave
identically for a known and an unknown address, in body and in timing, because
otherwise these two forms become an account-enumeration oracle.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ledger.accounts import service as accounts
from ledger.accounts.passwords import MIN_PASSWORD_LENGTH, PasswordError
from ledger.api.deps import SessionDep, UserDep
from ledger.config import AuthMode, get_settings
from ledger.db.base import User
from ledger.logging import get_logger
from ledger.security import jwt as jwt_helper
from ledger.security.principal import Role

log = get_logger(__name__)

router = APIRouter(tags=["accounts"])

Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)]


class SignUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: Password
    display_name: str | None = Field(default=None, max_length=80)


class SignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(max_length=200)


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=200)
    password: Password


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(max_length=200)
    new_password: Password


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Required even though the caller is authenticated: a hijacked session
    #: should not be able to destroy the account it borrowed.
    password: str = Field(max_length=200)


class AccountResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: Role
    tenant_id: int | None
    avatar_url: str | None
    preferences: dict[str, object]
    has_password: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the OAuth 2.0 type
    expires_in: int
    account: AccountResponse


class AcknowledgedResponse(BaseModel):
    """Deliberately says nothing about whether an account existed."""

    ok: Literal[True] = True
    message: str
    #: Only ever populated when `LEDGER_AUTH_MODE=dev`, so the flow can be
    #: exercised without a mail provider. Never present otherwise.
    reset_token: str | None = None


def to_account(user: User) -> AccountResponse:
    return AccountResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=Role(user.role),
        tenant_id=user.tenant_id,
        avatar_url=f"/api/accounts/me/avatar?v={int(user.updated_at.timestamp())}"
        if user.avatar_filename
        else None,
        preferences=dict(user.preferences or {}),
        has_password=user.password_hash is not None,
    )


def _issue(user: User) -> TokenResponse:
    settings = get_settings()
    token, expires_in = jwt_helper.issue(
        settings, subject=user.id, role=Role(user.role), tenant_id=user.tenant_id
    )
    return TokenResponse(access_token=token, expires_in=expires_in, account=to_account(user))


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def sign_up(body: SignUpRequest, session: SessionDep) -> TokenResponse:
    try:
        user = await accounts.sign_up(
            session,
            email=str(body.email),
            password=body.password,
            display_name=body.display_name,
        )
    except accounts.EmailTakenError as exc:
        # The one place enumeration is unavoidable: a signup form has to say
        # the address is taken or the person cannot proceed. Kept narrow --
        # sign-in and reset give nothing away.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        ) from exc
    except PasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except accounts.AccountError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    return _issue(user)


@router.post("/signin", response_model=TokenResponse)
async def sign_in(body: SignInRequest, session: SessionDep) -> TokenResponse:
    user = await accounts.authenticate(session, email=str(body.email), password=body.password)
    if user is None:
        # One message for both "no such account" and "wrong password".
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Those credentials are not valid.",
        )
    return _issue(user)


@router.post("/forgot-password", response_model=AcknowledgedResponse)
async def forgot_password(body: ForgotPasswordRequest, session: SessionDep) -> AcknowledgedResponse:
    settings = get_settings()
    issued = await accounts.begin_password_reset(session, email=str(body.email))

    if issued is not None:
        user, token = issued
        # A real deployment sends this. The console sender logs it, which is
        # enough for development and honest about what it is.
        log.info("password reset requested for %s: token issued", user.id)
        if settings.auth_mode is AuthMode.DEV:
            return AcknowledgedResponse(
                message="If that address has an account, a reset link is on its way.",
                reset_token=token,
            )

    return AcknowledgedResponse(
        message="If that address has an account, a reset link is on its way."
    )


@router.post("/reset-password", response_model=TokenResponse)
async def reset_password(body: ResetPasswordRequest, session: SessionDep) -> TokenResponse:
    try:
        user = await accounts.complete_password_reset(
            session, token=body.token, new_password=body.password
        )
    except PasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That reset link is no longer valid. Request a new one.",
        )
    return _issue(user)


@router.get("/me", response_model=AccountResponse)
async def me(user: UserDep) -> AccountResponse:
    return to_account(user)


@router.post("/me/password", response_model=AcknowledgedResponse)
async def change_password(
    body: ChangePasswordRequest, user: UserDep, session: SessionDep
) -> AcknowledgedResponse:
    try:
        changed = await accounts.change_password(
            session, user, current=body.current_password, new=body.new_password
        )
    except PasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    if not changed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your current password is not correct.",
        )
    return AcknowledgedResponse(message="Password changed.")


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    body: DeleteAccountRequest, user: UserDep, session: SessionDep
) -> Response:
    from ledger.accounts import passwords

    if not passwords.verify(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your password is not correct.",
        )
    await accounts.delete_account(session, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
