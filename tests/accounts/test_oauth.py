"""Provider sign-in, asserted at the points where it is usually got wrong.

None of these need a provider. The parts worth testing are the ones this
service decides -- which identity maps to which account, what the callback
will accept, and where it is willing to send a browser afterwards.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ledger.accounts import oauth
from ledger.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        jwt_secret="a-secret-long-enough-to-sign-with-comfortably",
        oauth_github_client_id="gh-client",
        oauth_github_client_secret="gh-secret",
        public_api_base="https://ledger.example.com",
        public_web_base="https://app.example.com",
        cors_origins=("https://app.example.com",),
    )


def test_only_configured_providers_are_advertised(settings: Settings) -> None:
    """Google has no credentials here, so it is not offered."""
    assert [p.name for p in oauth.available(settings)] == ["github"]
    assert oauth.credentials(settings, "google") is None


def test_the_authorize_url_carries_pkce_and_a_registered_callback(
    settings: Settings,
) -> None:
    url, _ = oauth.begin(settings, "github", next_url=None)
    query = parse_qs(urlparse(url).query)

    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    # Built from configuration, not from the request: a `Host` header is
    # attacker-controlled, and a callback derived from it leaves the site.
    assert query["redirect_uri"] == [
        "https://ledger.example.com/api/accounts/oauth/github/callback"
    ]


def test_the_verifier_never_travels_through_the_provider(settings: Settings) -> None:
    """PKCE only protects anything if the verifier stays in the browser.

    A verifier packed into `state` goes to the provider, into its logs, and
    back through the redirect -- which is every place the code goes.
    """
    url, cookie = oauth.begin(settings, "github", next_url=None)
    state = parse_qs(urlparse(url).query)["state"][0]

    verifier = oauth.read_state(settings, cookie, provider="github", state=state)
    assert verifier
    assert verifier not in url


def test_a_callback_with_the_wrong_state_is_refused(settings: Settings) -> None:
    """The CSRF check: a callback must belong to the flow that started here."""
    _, cookie = oauth.begin(settings, "github", next_url=None)
    with pytest.raises(oauth.OAuthError):
        oauth.read_state(settings, cookie, provider="github", state="not-the-state")


def test_a_callback_for_another_provider_is_refused(settings: Settings) -> None:
    url, cookie = oauth.begin(settings, "github", next_url=None)
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(oauth.OAuthError):
        oauth.read_state(settings, cookie, provider="google", state=state)


def test_a_state_cookie_signed_with_another_key_is_refused(settings: Settings) -> None:
    url, _ = oauth.begin(settings, "github", next_url=None)
    state = parse_qs(urlparse(url).query)["state"][0]

    forged = oauth.begin(
        settings.model_copy(update={"jwt_secret": "a-different-secret-entirely-and-long-enough"}),
        "github",
        next_url=None,
    )[1]
    with pytest.raises(oauth.OAuthError):
        oauth.read_state(settings, forged, provider="github", state=state)


def test_a_missing_cookie_is_refused_rather_than_waved_through(settings: Settings) -> None:
    with pytest.raises(oauth.OAuthError):
        oauth.read_state(settings, None, provider="github", state="anything")


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example.net/harvest",
        "//evil.example.net",
        "http://app.example.com.evil.net",
        "javascript:alert(1)",
    ],
)
def test_the_post_login_redirect_refuses_an_unlisted_target(
    settings: Settings, target: str
) -> None:
    """An open redirect on the end of a login flow is a phishing primitive."""
    _, cookie = oauth.begin(settings, "github", next_url=target)
    assert oauth.next_from_state(settings, cookie) == "https://app.example.com"


@pytest.mark.parametrize("target", ["/settings", "/chat/abc", "https://app.example.com/settings"])
def test_a_listed_target_is_kept(settings: Settings, target: str) -> None:
    _, cookie = oauth.begin(settings, "github", next_url=target)
    assert oauth.next_from_state(settings, cookie) == target


def test_a_github_email_is_taken_only_when_it_is_verified() -> None:
    """GitHub reports addresses the person never proved they own."""
    assert (
        oauth._primary_verified_email(
            [
                {"email": "unverified@example.com", "primary": True, "verified": False},
                {"email": "real@example.com", "primary": False, "verified": True},
            ]
        )
        == "real@example.com"
    )
    assert (
        oauth._primary_verified_email(
            [{"email": "nobody@example.com", "primary": True, "verified": False}]
        )
        is None
    )
    assert oauth._primary_verified_email([]) is None
    assert oauth._primary_verified_email({"unexpected": "shape"}) is None


def _transport(routes: dict[str, tuple[int, object]]) -> httpx.MockTransport:
    """Answer the provider's endpoints without a network."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key not in routes:
            return httpx.Response(404, json={"error": f"unrouted {key}"})
        status, body = routes[key]
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


async def test_a_github_exchange_yields_the_verified_primary_address(
    settings: Settings,
) -> None:
    transport = _transport(
        {
            "POST /login/oauth/access_token": (200, {"access_token": "gh-token"}),
            "GET /user": (200, {"id": 4242, "login": "octo", "name": "Octo Cat"}),
            "GET /user/emails": (
                200,
                [
                    {"email": "public@example.com", "primary": True, "verified": False},
                    {"email": "verified@example.com", "primary": False, "verified": True},
                ],
            ),
        }
    )
    async with httpx.AsyncClient(transport=transport) as client:
        profile = await oauth.exchange(settings, "github", code="c", verifier="v", client=client)

    assert profile.subject == "4242"
    # The primary address is unverified, so it is not the one taken: an
    # address the person never proved they own must not become their identity.
    assert profile.email == "verified@example.com"
    assert profile.display_name == "Octo Cat"


async def test_an_exchange_that_returns_no_token_is_an_error_not_a_sign_in(
    settings: Settings,
) -> None:
    """GitHub answers a bad code with 200 and an `error` body, not a 4xx."""
    transport = _transport(
        {"POST /login/oauth/access_token": (200, {"error": "bad_verification_code"})}
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(oauth.OAuthError):
            await oauth.exchange(settings, "github", code="c", verifier="v", client=client)


async def test_a_failed_exchange_does_not_leak_the_provider_response(
    settings: Settings,
) -> None:
    transport = _transport(
        {"POST /login/oauth/access_token": (401, {"error": "client_secret_is_wrong"})}
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(oauth.OAuthError) as raised:
            await oauth.exchange(settings, "github", code="c", verifier="v", client=client)

    assert "client_secret" not in str(raised.value)


async def test_google_accepts_an_address_only_when_the_provider_verified_it() -> None:
    settings = Settings(
        jwt_secret="a-secret-long-enough-to-sign-with-comfortably",
        oauth_google_client_id="g-client",
        oauth_google_client_secret="g-secret",
    )
    routes = {
        "POST /token": (200, {"access_token": "g-token"}),
        "GET /v1/userinfo": (
            200,
            {"sub": "10", "email": "person@example.com", "email_verified": False, "name": "P"},
        ),
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        unverified = await oauth.exchange(settings, "google", code="c", verifier="v", client=client)
    assert unverified.subject == "10"
    assert unverified.email is None

    routes["GET /v1/userinfo"] = (
        200,
        {"sub": "10", "email": "person@example.com", "email_verified": True, "name": "P"},
    )
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        verified = await oauth.exchange(settings, "google", code="c", verifier="v", client=client)
    assert verified.email == "person@example.com"


async def test_an_exchange_for_an_unconfigured_provider_is_refused(
    settings: Settings,
) -> None:
    async with httpx.AsyncClient(transport=_transport({})) as client:
        with pytest.raises(oauth.OAuthError):
            await oauth.exchange(settings, "google", code="c", verifier="v", client=client)
