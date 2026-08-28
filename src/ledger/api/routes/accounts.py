"""Sign up, sign in, reset, and delete.

The responses here are deliberately uninformative in one specific way: nothing
reveals whether an email is registered. Sign-in and password reset behave
identically for a known and an unknown address, in body and in timing, because
otherwise these two forms become an account-enumeration oracle.
"""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from ledger.accounts import avatars, oauth
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


class UpdateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    preferences: dict[str, object] | None = None


@router.patch("/me", response_model=AccountResponse)
async def update_profile(
    body: UpdateProfileRequest, user: UserDep, session: SessionDep
) -> AccountResponse:
    """Update the parts of a profile the account holder owns.

    Role and tenant are absent by construction: they are granted, not chosen,
    and an endpoint that accepted them would be a privilege-escalation path
    however carefully it was guarded elsewhere.
    """
    if body.display_name is not None:
        user.display_name = body.display_name.strip()[:80]
    if body.preferences is not None:
        merged = dict(user.preferences or {})
        merged.update(body.preferences)
        user.preferences = merged
    await session.flush()
    return to_account(user)


@router.post("/me/avatar", response_model=AccountResponse)
async def upload_avatar(
    user: UserDep, session: SessionDep, file: Annotated[UploadFile, File()]
) -> AccountResponse:
    settings = get_settings()

    # Read with a hard ceiling rather than trusting the declared length.
    data = await file.read(avatars.MAX_UPLOAD_BYTES + 1)
    if len(data) > avatars.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Images must be under {avatars.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    try:
        processed = avatars.process(data)
    except avatars.AvatarError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    user.avatar_filename = avatars.store(settings.state_path, user.id, processed)
    await session.flush()
    return to_account(user)


@router.get("/me/avatar")
async def get_avatar(user: UserDep) -> FileResponse:
    settings = get_settings()
    if not user.avatar_filename:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No avatar set.")
    path = avatars.path_for(settings.state_path, user.avatar_filename)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No avatar set.")
    # Always PNG: it is re-encoded on upload, so the type is ours to state.
    return FileResponse(path, media_type="image/png")


@router.delete("/me/avatar", response_model=AccountResponse)
async def delete_avatar(user: UserDep, session: SessionDep) -> AccountResponse:
    settings = get_settings()
    avatars.remove(settings.state_path, user.avatar_filename)
    user.avatar_filename = None
    await session.flush()
    return to_account(user)


class OAuthProvider(BaseModel):
    name: str
    label: str


class OAuthProviders(BaseModel):
    providers: list[OAuthProvider]


@router.get("/oauth/providers", response_model=OAuthProviders)
async def oauth_providers() -> OAuthProviders:
    """Which providers this deployment can actually sign people in with.

    The sign-in page asks rather than assuming, so an unconfigured provider is
    never offered as a button that fails after the redirect.
    """
    settings = get_settings()
    return OAuthProviders(
        providers=[OAuthProvider(name=p.name, label=p.label) for p in oauth.available(settings)]
    )


def _oauth_provider_or_404(provider: str) -> str:
    settings = get_settings()
    if provider not in oauth.PROVIDERS or oauth.credentials(settings, provider) is None:
        # 404 rather than 501: an unconfigured provider is not a route that
        # exists here, and saying "not configured" tells a scanner what to
        # come back for.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such sign-in method.")
    return provider


@router.get("/oauth/{provider}/start")
async def oauth_start(
    provider: str,
    # Named `next` on the wire because that is the convention a person typing
    # the URL expects; `next` is a builtin, so it is not the parameter name.
    next_url: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    """Send the browser to the provider, remembering the flow in a cookie."""
    settings = get_settings()
    _oauth_provider_or_404(provider)

    url, cookie = oauth.begin(settings, provider, next_url=next_url)
    response = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    response.set_cookie(
        oauth.STATE_COOKIE,
        cookie,
        max_age=oauth.STATE_TTL_SECONDS,
        httponly=True,
        # Lax rather than Strict: the callback arrives as a top-level
        # navigation from the provider, and Strict would withhold the cookie
        # exactly then, which is the one moment it is needed.
        samesite="lax",
        secure=settings.public_api_base.startswith("https://"),
        path="/api/accounts/oauth",
    )
    return response


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    session: SessionDep,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Complete the flow and hand the token to the app.

    The token goes back in the URL *fragment*, not the query string: a
    fragment is not sent to the server, so it stays out of access logs, out of
    the `Referer` header, and out of anything sitting in front of the app.
    """
    settings = get_settings()
    _oauth_provider_or_404(provider)
    cookie = request.cookies.get(oauth.STATE_COOKIE)
    landing = oauth.next_from_state(settings, cookie)

    if error or not code or not state:
        # A person who declines at the provider is not an error to shout about.
        log.info("oauth flow for %s ended without a code: %s", provider, error or "cancelled")
        return _oauth_redirect(f"{landing}#oauth_error=cancelled")

    try:
        verifier = oauth.read_state(settings, cookie, provider=provider, state=state)
        async with httpx.AsyncClient() as client:
            profile = await oauth.exchange(
                settings, provider, code=code, verifier=verifier, client=client
            )
        user = await accounts.link_identity(session, profile)
    except accounts.EmailTakenError:
        # A password account already holds that address. Linking is a
        # deliberate act from inside the account, not something a login flow
        # should do on the person's behalf.
        return _oauth_redirect(f"{landing}#oauth_error=email_in_use")
    except (oauth.OAuthError, accounts.AccountError) as exc:
        log.warning("oauth sign-in failed for %s: %s", provider, exc)
        return _oauth_redirect(f"{landing}#oauth_error=failed")

    issued = _issue(user)
    response = _oauth_redirect(f"{landing}#access_token={issued.access_token}")
    response.delete_cookie(oauth.STATE_COOKIE, path="/api/accounts/oauth")
    return response


def _oauth_redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me/identities", response_model=list[OAuthProvider])
async def linked_identities(user: UserDep, session: SessionDep) -> list[OAuthProvider]:
    """Which providers this account can sign in with, for the settings page."""
    linked = await accounts.identities_for(session, user.id)
    return [
        OAuthProvider(name=name, label=oauth.PROVIDERS[name].label)
        for name in linked
        if name in oauth.PROVIDERS
    ]
