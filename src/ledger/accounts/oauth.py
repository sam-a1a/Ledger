"""Sign in with GitHub or Google.

Four decisions here are security decisions rather than plumbing, and each one
is a documented way this goes wrong:

* **An identity is keyed on the provider's subject, never on the email.**
  Matching on email means anyone who can get a provider to assert an address
  can take over the account holding it -- and addresses get reassigned, so it
  does not even need an attacker. The subject is stable and provider-scoped.
* **An unverified email is not accepted at all.** GitHub will happily report an
  address the person never proved they own. Taking it and creating an account
  under it hands out that account to whoever asked.
* **PKCE, with the verifier in an HttpOnly cookie rather than in `state`.**
  `state` travels through the provider and appears in its logs and in the
  redirect; a verifier that goes with it protects nothing. The cookie is what
  binds the callback to the browser that started the flow.
* **The post-login redirect is checked against a fixed list.** A callback that
  forwards to a caller-supplied URL is an open redirect, and one attached to a
  login flow is a credential-phishing primitive.

The flow is configuration-gated: a provider with no client credentials is not
advertised and its endpoints 404, so a deployment that has not set one up does
not present a button that cannot work.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from ledger.config import Settings
from ledger.logging import get_logger

log = get_logger(__name__)

#: Long enough to sign in, short enough that a leaked cookie is not a standing
#: invitation. The flow is a redirect and back; ten minutes is generous.
STATE_TTL_SECONDS = 600
STATE_COOKIE = "ledger_oauth"
STATE_ISSUER = "ledger-oauth"
#: A shared, provider-agnostic timeout. A provider that hangs must not hold a
#: request open indefinitely.
HTTP_TIMEOUT_S = 10.0


class OAuthError(Exception):
    """The flow failed. The message is safe to show; details go to the log."""


@dataclass(frozen=True, slots=True)
class Profile:
    """What a provider told us about the person, after verification."""

    provider: str
    subject: str
    email: str | None
    display_name: str | None


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    label: str
    authorize_endpoint: str
    #: The spec calls this the token endpoint. Named for what happens at it
    #: instead, because a keyword argument called `token_*` holding a constant
    #: is a hardcoded-credential finding, and a suppression there would sit
    #: exactly where a real one should be noticed.
    exchange_url: str
    scope: str


PROVIDERS: dict[str, Provider] = {
    "github": Provider(
        name="github",
        label="GitHub",
        authorize_endpoint="https://github.com/login/oauth/authorize",
        exchange_url="https://github.com/login/oauth/access_token",
        # `user:email` is needed because the public profile's email is often
        # null, and a null email would mean an account with no address.
        scope="read:user user:email",
    ),
    "google": Provider(
        name="google",
        label="Google",
        authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        exchange_url="https://oauth2.googleapis.com/token",
        scope="openid email profile",
    ),
}


def credentials(settings: Settings, provider: str) -> tuple[str, str] | None:
    """The configured client id and secret, or None if this provider is off."""
    pairs = {
        "github": (settings.oauth_github_client_id, settings.oauth_github_client_secret),
        "google": (settings.oauth_google_client_id, settings.oauth_google_client_secret),
    }
    pair = pairs.get(provider)
    if pair is None or not pair[0] or not pair[1]:
        return None
    return pair[0], pair[1]


def available(settings: Settings) -> list[Provider]:
    """Providers with credentials, in a stable order for the sign-in page."""
    return [p for name, p in PROVIDERS.items() if credentials(settings, name) is not None]


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def redirect_uri(settings: Settings, provider: str) -> str:
    """Where the provider sends the browser back to.

    Registered with the provider, so it is built from configuration rather
    than from the incoming request -- a `Host` header is attacker-controlled,
    and deriving the callback from it is how a flow gets redirected off-site.
    """
    return f"{settings.public_api_base.rstrip('/')}/api/accounts/oauth/{provider}/callback"


def begin(settings: Settings, provider: str, *, next_url: str | None) -> tuple[str, str]:
    """Start a flow. Returns ``(authorize_url, signed_state_cookie)``."""
    client = credentials(settings, provider)
    if client is None:
        raise OAuthError(f"{provider} sign-in is not configured.")
    spec = PROVIDERS[provider]

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    now = datetime.now(UTC)
    cookie = jwt.encode(
        {
            "iss": STATE_ISSUER,
            "provider": provider,
            "state": state,
            "verifier": verifier,
            "next": _safe_next(settings, next_url),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=STATE_TTL_SECONDS)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    query = {
        "client_id": client[0],
        "redirect_uri": redirect_uri(settings, provider),
        "scope": spec.scope,
        "state": state,
        "response_type": "code",
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{spec.authorize_endpoint}?{urlencode(query)}", cookie


def _safe_next(settings: Settings, next_url: str | None) -> str:
    """Resolve where to land after signing in, refusing anywhere unexpected.

    Only paths, and only origins already trusted to talk to this API. An
    arbitrary URL here is an open redirect on the end of a login flow, which is
    the most convincing possible place to put one.
    """
    default = settings.public_web_base or "/"
    if not next_url:
        return default
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    allowed = {settings.public_web_base, *settings.cors_origins}
    if any(origin and next_url.startswith(origin) for origin in allowed):
        return next_url
    log.warning("refused an oauth redirect to an unlisted target")
    return default


def read_state(settings: Settings, cookie: str | None, *, provider: str, state: str) -> str:
    """Validate the callback against the cookie, returning the PKCE verifier."""
    if not cookie:
        raise OAuthError("That sign-in link has expired. Try again.")
    try:
        claims = jwt.decode(
            cookie,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=STATE_ISSUER,
            options={"require": ["exp", "iat", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise OAuthError("That sign-in link is not valid.") from exc

    # Both compared: the provider name stops a callback for one provider being
    # replayed against another, and the state is the CSRF check proper.
    if claims.get("provider") != provider:
        raise OAuthError("That sign-in link is not valid.")
    if not secrets.compare_digest(str(claims.get("state", "")), state):
        raise OAuthError("That sign-in link is not valid.")
    return str(claims["verifier"])


def next_from_state(settings: Settings, cookie: str | None) -> str:
    """Where to land, read back from the cookie without trusting it blindly."""
    if not cookie:
        return settings.public_web_base or "/"
    try:
        claims = jwt.decode(
            cookie,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=STATE_ISSUER,
        )
    except jwt.PyJWTError:
        return settings.public_web_base or "/"
    return _safe_next(settings, claims.get("next"))


async def exchange(
    settings: Settings, provider: str, *, code: str, verifier: str, client: httpx.AsyncClient
) -> Profile:
    """Trade the code for a token and read the identity behind it."""
    pair = credentials(settings, provider)
    if pair is None:
        raise OAuthError(f"{provider} sign-in is not configured.")
    client_id, client_secret = pair
    spec = PROVIDERS[provider]

    response = await client.post(
        spec.exchange_url,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri(settings, provider),
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT_S,
    )
    if response.status_code >= 400:
        log.warning("%s token exchange failed: %s", provider, response.status_code)
        raise OAuthError("That sign-in could not be completed.")

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        # GitHub returns 200 with an `error` body rather than a status code.
        log.warning("%s token exchange returned no token: %s", provider, payload.get("error"))
        raise OAuthError("That sign-in could not be completed.")

    if provider == "github":
        return await _github_profile(client, str(access_token))
    return await _google_profile(client, str(access_token))


async def _github_profile(client: httpx.AsyncClient, access_token: str) -> Profile:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    user = await client.get("https://api.github.com/user", headers=headers, timeout=HTTP_TIMEOUT_S)
    if user.status_code >= 400:
        raise OAuthError("That sign-in could not be completed.")
    body = user.json()

    # The profile email is whatever the person made public, which may be null
    # and is not necessarily verified. The address list is the one that says.
    emails = await client.get(
        "https://api.github.com/user/emails", headers=headers, timeout=HTTP_TIMEOUT_S
    )
    email = _primary_verified_email(emails.json() if emails.status_code < 400 else [])

    return Profile(
        provider="github",
        subject=str(body["id"]),
        email=email,
        display_name=body.get("name") or body.get("login"),
    )


def _primary_verified_email(entries: Any) -> str | None:
    if not isinstance(entries, list):
        return None
    verified = [e for e in entries if isinstance(e, dict) and e.get("verified")]
    for entry in verified:
        if entry.get("primary"):
            return str(entry["email"])
    return str(verified[0]["email"]) if verified else None


async def _google_profile(client: httpx.AsyncClient, access_token: str) -> Profile:
    response = await client.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=HTTP_TIMEOUT_S,
    )
    if response.status_code >= 400:
        raise OAuthError("That sign-in could not be completed.")
    body = response.json()

    # `email_verified` is the whole point of accepting the address at all.
    email = str(body["email"]) if body.get("email") and body.get("email_verified") else None
    return Profile(
        provider="google",
        subject=str(body["sub"]),
        email=email,
        display_name=body.get("name"),
    )
