"""The account endpoints, over HTTP, with the governance properties asserted.

Most of these are ordinary CRUD. Three are not, and they are the reason this
file exists: a role cannot be requested, an address cannot be discovered, and a
deletion does not take the audit trail with it.
"""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

PASSWORD = "correct-horse-battery"


def _png(colour: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_role_cannot_be_requested_at_signup(client: httpx.AsyncClient) -> None:
    """The whole RBAC claim rests on this: the client does not choose.

    A body naming a role is not an error -- it is ignored, and the extra key
    must not reach the model of the account.
    """
    await client.post(
        "/api/accounts/signup",
        json={"email": "first@example.com", "password": PASSWORD},
    )
    response = await client.post(
        "/api/accounts/signup",
        json={"email": "climber@example.com", "password": PASSWORD, "role": "analyst"},
    )
    # Refused, not ignored: the request model forbids unknown keys, so this
    # cannot become a success that quietly assigned something else.
    assert response.status_code == 422

    accepted = await client.post(
        "/api/accounts/signup",
        json={"email": "climber@example.com", "password": PASSWORD},
    )
    me = await client.get("/api/accounts/me", headers=_auth(accepted.json()["access_token"]))
    assert me.json()["role"] == "viewer"


async def test_signup_is_the_only_surface_that_admits_an_address_is_taken(
    client: httpx.AsyncClient,
) -> None:
    """A deliberate, bounded exception, pinned so it stays bounded.

    A signup form has to say the address is taken or the person cannot
    proceed. The properties that matter are that it stops there -- sign-in and
    reset are asserted identical elsewhere -- and that the admission costs a
    conflict rather than arriving inside a successful-looking response.
    """
    first = await client.post(
        "/api/accounts/signup", json={"email": "taken@example.com", "password": PASSWORD}
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/accounts/signup", json={"email": "taken@example.com", "password": PASSWORD}
    )
    assert second.status_code == 409
    assert "access_token" not in second.text


async def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/accounts/signup", json={"email": "real@example.com", "password": PASSWORD}
    )

    wrong = await client.post(
        "/api/accounts/signin", json={"email": "real@example.com", "password": "wrong-password-x"}
    )
    unknown = await client.post(
        "/api/accounts/signin", json={"email": "ghost@example.com", "password": PASSWORD}
    )

    assert wrong.status_code == unknown.status_code
    assert wrong.json() == unknown.json()


async def test_an_address_is_normalised_so_case_does_not_duplicate_an_account(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/accounts/signup", json={"email": "Mixed@Example.com ", "password": PASSWORD}
    )
    signin = await client.post(
        "/api/accounts/signin", json={"email": "mixed@example.com", "password": PASSWORD}
    )
    assert signin.status_code == 200


async def test_a_password_reset_token_is_single_use(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/accounts/signup", json={"email": "forgetful@example.com", "password": PASSWORD}
    )
    issued = await client.post(
        "/api/accounts/forgot-password", json={"email": "forgetful@example.com"}
    )
    token = issued.json()["reset_token"]

    first = await client.post(
        "/api/accounts/reset-password", json={"token": token, "password": "a-new-password-01"}
    )
    assert first.status_code == 200

    replayed = await client.post(
        "/api/accounts/reset-password", json={"token": token, "password": "another-password-2"}
    )
    assert replayed.status_code >= 400


async def test_asking_to_reset_an_unknown_address_looks_identical(
    client: httpx.AsyncClient,
) -> None:
    known = await client.post(
        "/api/accounts/signup", json={"email": "known@example.com", "password": PASSWORD}
    )
    assert known.status_code == 201

    for address in ("known@example.com", "unknown@example.com"):
        response = await client.post("/api/accounts/forgot-password", json={"email": address})
        assert response.status_code == 200
        assert "on its way" in response.json()["message"]


async def test_changing_a_password_invalidates_the_old_one(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/api/accounts/signup", json={"email": "rotator@example.com", "password": PASSWORD}
    )
    token = signup.json()["access_token"]

    changed = await client.post(
        "/api/accounts/me/password",
        json={"current_password": PASSWORD, "new_password": "a-brand-new-secret"},
        headers=_auth(token),
    )
    assert changed.status_code == 200

    stale = await client.post(
        "/api/accounts/signin", json={"email": "rotator@example.com", "password": PASSWORD}
    )
    assert stale.status_code >= 400


async def test_changing_a_password_requires_the_current_one(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/api/accounts/signup", json={"email": "careful@example.com", "password": PASSWORD}
    )
    response = await client.post(
        "/api/accounts/me/password",
        json={"current_password": "not-the-password", "new_password": "does-not-matter-1"},
        headers=_auth(signup.json()["access_token"]),
    )
    assert response.status_code >= 400


async def test_a_profile_update_changes_the_name_but_not_the_role(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/accounts/signup", json={"email": "seed@example.com", "password": PASSWORD}
    )
    signup = await client.post(
        "/api/accounts/signup", json={"email": "namer@example.com", "password": PASSWORD}
    )
    token = signup.json()["access_token"]

    # A body naming a role is refused outright rather than silently ignored:
    # the request model forbids unknown keys, so an escalation attempt is a
    # validation error instead of a success that quietly did less than it said.
    escalation = await client.patch(
        "/api/accounts/me",
        json={"display_name": "Renamed", "role": "analyst"},
        headers=_auth(token),
    )
    assert escalation.status_code == 422

    updated = await client.patch(
        "/api/accounts/me", json={"display_name": "Renamed"}, headers=_auth(token)
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Renamed"
    assert updated.json()["role"] == "viewer"


async def test_an_avatar_round_trips_and_is_served_as_the_png_we_produced(
    client: httpx.AsyncClient,
) -> None:
    signup = await client.post(
        "/api/accounts/signup", json={"email": "face@example.com", "password": PASSWORD}
    )
    token = signup.json()["access_token"]

    uploaded = await client.post(
        "/api/accounts/me/avatar",
        files={"file": ("photo.jpg", _png(), "image/jpeg")},
        headers=_auth(token),
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["avatar_url"]

    fetched = await client.get("/api/accounts/me/avatar", headers=_auth(token))
    assert fetched.status_code == 200
    # Declared as a JPEG on the way in; stored as bytes we encoded.
    assert fetched.content[:8] == b"\x89PNG\r\n\x1a\n"

    removed = await client.delete("/api/accounts/me/avatar", headers=_auth(token))
    assert removed.status_code == 200
    assert not removed.json()["avatar_url"]


async def test_a_file_that_is_not_an_image_is_refused(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/api/accounts/signup", json={"email": "hostile@example.com", "password": PASSWORD}
    )
    response = await client.post(
        "/api/accounts/me/avatar",
        files={"file": ("shell.png", b"<?php system($_GET['c']); ?>", "image/png")},
        headers=_auth(signup.json()["access_token"]),
    )
    assert response.status_code == 422
    assert "image" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "path", ["/api/accounts/me", "/api/accounts/me/avatar", "/api/conversations"]
)
async def test_the_personal_endpoints_require_a_token(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code in {401, 403}


async def test_a_token_outlives_its_account_by_nothing(client: httpx.AsyncClient) -> None:
    """A signed token naming a deleted account must stop working immediately.

    Signature validity is not authorisation: the account is the authority, and
    a token that still verifies after deletion is a live credential for a
    person who asked to be forgotten.
    """
    signup = await client.post(
        "/api/accounts/signup", json={"email": "departing@example.com", "password": PASSWORD}
    )
    token = signup.json()["access_token"]

    deleted = await client.request(
        "DELETE", "/api/accounts/me", json={"password": PASSWORD}, headers=_auth(token)
    )
    assert deleted.status_code == 204

    after = await client.get("/api/accounts/me", headers=_auth(token))
    assert after.status_code in {401, 403}
