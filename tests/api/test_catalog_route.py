"""The catalogue endpoint, and the boundary it makes visible.

Serving the catalogue directly is a convenience. Serving it *scoped* is the
point: a viewer's is visibly shorter than an analyst's, so the access boundary
is something you can see rather than something you discover by being refused.
"""

from __future__ import annotations

import httpx
import pytest

from ledger.security.policy import restricted_columns

pytestmark = pytest.mark.kafka


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_the_catalogue_describes_the_columns_it_lists(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    response = await client.get("/api/catalog", headers=_auth(analyst_token))
    assert response.status_code == 200

    body = response.json()
    assert body["role"] == "analyst"
    assert body["total_rows"] > 0
    assert body["version"]

    by_name = {column["name"]: column for column in body["columns"]}
    distance = by_name["trip_distance"]
    assert distance["semantic_type"] == "numeric"
    assert distance["description"]
    # Provenance travels with the description: whether a line was written by a
    # human, generated, or derived from the profile is governance metadata.
    assert distance["description_source"] in {"seed", "llm", "derived"}
    assert 0.0 <= distance["null_fraction"] <= 1.0


async def test_a_viewer_sees_a_shorter_catalogue_than_an_analyst(
    client: httpx.AsyncClient, analyst_token: str, viewer_token: str
) -> None:
    analyst = await client.get("/api/catalog", headers=_auth(analyst_token))
    viewer = await client.get("/api/catalog", headers=_auth(viewer_token))

    analyst_names = {c["name"] for c in analyst.json()["columns"]}
    viewer_names = {c["name"] for c in viewer.json()["columns"]}

    assert viewer.json()["role"] == "viewer"
    assert viewer_names < analyst_names
    assert analyst_names - viewer_names == set(restricted_columns())


async def test_a_restricted_column_is_absent_from_the_whole_payload(
    client: httpx.AsyncClient, analyst_token: str, viewer_token: str
) -> None:
    """Not merely filtered out of the list -- absent from the bytes.

    A sample value or a description mentioning a hidden column would leak it
    just as effectively as listing it, and this route serves both.
    """
    response = await client.get("/api/catalog", headers=_auth(viewer_token))
    body = response.text
    for hidden in restricted_columns():
        assert hidden not in body, f"{hidden} appears in the viewer's catalogue"


async def test_the_internal_tenant_column_is_shown_to_nobody(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    response = await client.get("/api/catalog", headers=_auth(analyst_token))
    assert "tenant_id" not in {c["name"] for c in response.json()["columns"]}


async def test_the_catalogue_requires_a_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/catalog")
    assert response.status_code in {401, 403}
