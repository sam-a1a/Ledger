"""The OAuth endpoints over HTTP, with the provider stubbed out.

There is no provider here and there does not need to be one. What is worth
asserting is what this service does with a callback: whether it checks the
flow belongs to the browser that started it, where it is willing to redirect
afterwards, and what it hands back when it will not sign someone in.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ledger.accounts import oauth
from ledger.config import Settings, get_settings

WEB = "http://app.test"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """A deployment with GitHub set up, and Google deliberately not."""
    monkeypatch.setenv("LEDGER_OAUTH_GITHUB_CLIENT_ID", "gh-client")
    monkeypatch.setenv("LEDGER_OAUTH_GITHUB_CLIENT_SECRET", "gh-secret")
    monkeypatch.setenv("LEDGER_PUBLIC_API_BASE", "http://ledger")
    monkeypatch.setenv("LEDGER_PUBLIC_WEB_BASE", WEB)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a fixed identity instead of calling GitHub."""

    async def _exchange(
        settings: Settings,
        provider: str,
        *,
        code: str,
        verifier: str,
        client: httpx.AsyncClient,
    ) -> oauth.Profile:
        assert verifier, "the callback must recover the PKCE verifier"
        return oauth.Profile(
            provider=provider,
            subject=f"subject-for-{code}",
            email=f"{code}@example.com",
            display_name="Provider Person",
        )

    monkeypatch.setattr(oauth, "exchange", _exchange)


async def test_an_unconfigured_deployment_advertises_nothing(
    client: httpx.AsyncClient,
) -> None:
    listed = await client.get("/api/accounts/oauth/providers")
    assert listed.status_code == 200
    assert listed.json()["providers"] == []


async def test_an_unconfigured_provider_is_not_a_route(client: httpx.AsyncClient) -> None:
    """404, not "not configured": the second tells a scanner what to try later."""
    response = await client.get("/api/accounts/oauth/github/start")
    assert response.status_code == 404


async def test_only_configured_providers_are_offered(
    client: httpx.AsyncClient, configured: Settings
) -> None:
    listed = await client.get("/api/accounts/oauth/providers")
    assert [p["name"] for p in listed.json()["providers"]] == ["github"]

    assert (await client.get("/api/accounts/oauth/google/start")).status_code == 404


async def test_starting_a_flow_redirects_and_remembers_it_in_a_cookie(
    client: httpx.AsyncClient, configured: Settings
) -> None:
    response = await client.get("/api/accounts/oauth/github/start")
    assert response.status_code == 307

    target = urlparse(response.headers["location"])
    assert target.netloc == "github.com"

    cookie = response.cookies.get(oauth.STATE_COOKIE)
    assert cookie, "the flow is not bound to this browser without it"
    # The verifier must not travel with the code, so it is not in the URL.
    verifier = oauth.read_state(
        configured, cookie, provider="github", state=parse_qs(target.query)["state"][0]
    )
    assert verifier not in response.headers["location"]


async def test_a_completed_flow_returns_a_token_in_the_fragment(
    client: httpx.AsyncClient, configured: Settings, stub_provider: None
) -> None:
    """The token goes in the fragment, which is never sent to a server.

    In the query string it would land in access logs, in `Referer`, and in
    anything sitting in front of the app.
    """
    started = await client.get("/api/accounts/oauth/github/start")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    callback = await client.get(
        "/api/accounts/oauth/github/callback",
        params={"code": "newcomer", "state": state},
    )
    assert callback.status_code == 303

    location = callback.headers["location"]
    assert location.startswith(f"{WEB}#access_token=")
    assert "?" not in location

    token = location.split("#access_token=", 1)[1]
    me = await client.get("/api/accounts/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "newcomer@example.com"


async def test_a_callback_with_a_forged_state_signs_nobody_in(
    client: httpx.AsyncClient, configured: Settings, stub_provider: None
) -> None:
    # Started for its effect on the cookie jar: the browser holds a valid
    # state cookie, and the callback still arrives with the wrong state.
    await client.get("/api/accounts/oauth/github/start")

    callback = await client.get(
        "/api/accounts/oauth/github/callback",
        params={"code": "attacker", "state": "not-the-state"},
    )
    assert callback.status_code == 303
    assert "access_token" not in callback.headers["location"]
    assert "oauth_error" in callback.headers["location"]


async def test_a_callback_with_no_cookie_signs_nobody_in(
    client: httpx.AsyncClient, configured: Settings, stub_provider: None
) -> None:
    """A callback replayed outside the browser that started it has no cookie."""
    started = await client.get("/api/accounts/oauth/github/start")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    # The callback arrives in a browser that never started the flow, so it
    # carries the code and the state but not the cookie binding them.
    client.cookies.clear()

    callback = await client.get(
        "/api/accounts/oauth/github/callback", params={"code": "attacker", "state": state}
    )
    assert callback.status_code == 303
    assert "access_token" not in callback.headers["location"]
    assert "oauth_error" in callback.headers["location"]


async def test_declining_at_the_provider_lands_back_without_an_error_page(
    client: httpx.AsyncClient, configured: Settings
) -> None:
    callback = await client.get(
        "/api/accounts/oauth/github/callback", params={"error": "access_denied"}
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == f"{WEB}#oauth_error=cancelled"


async def test_an_address_already_held_by_a_password_account_is_not_adopted(
    client: httpx.AsyncClient, configured: Settings, stub_provider: None
) -> None:
    """The takeover case, at the HTTP boundary.

    Signing in with a provider that asserts an address someone else registered
    must not hand over that account. It stops, and says why in a way the app
    can act on.
    """
    signup = await client.post(
        "/api/accounts/signup",
        json={"email": "victim@example.com", "password": "correct-horse-battery"},
    )
    assert signup.status_code == 201

    started = await client.get("/api/accounts/oauth/github/start")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    callback = await client.get(
        "/api/accounts/oauth/github/callback",
        params={"code": "victim", "state": state},
    )
    assert callback.headers["location"] == f"{WEB}#oauth_error=email_in_use"

    # And the original account still signs in with its password.
    signin = await client.post(
        "/api/accounts/signin",
        json={"email": "victim@example.com", "password": "correct-horse-battery"},
    )
    assert signin.status_code == 200


async def test_the_settings_page_can_list_linked_providers(
    client: httpx.AsyncClient, configured: Settings, stub_provider: None
) -> None:
    started = await client.get("/api/accounts/oauth/github/start")
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    callback = await client.get(
        "/api/accounts/oauth/github/callback",
        params={"code": "listed", "state": state},
    )
    token = callback.headers["location"].split("#access_token=", 1)[1]

    linked = await client.get(
        "/api/accounts/me/identities", headers={"Authorization": f"Bearer {token}"}
    )
    assert [p["name"] for p in linked.json()] == ["github"]
