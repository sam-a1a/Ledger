"""Liveness and readiness, asserted as signals rather than as endpoints.

The reason these are worth testing: `/api/ready` once returned 200 with no
checks in it, and Compose gated on it. Readiness that always passes is worse
than none at all -- it is the signal an orchestrator trusts to decide that a
broken container is fit to receive traffic.

So each test asserts a check *fails* when the thing it names is broken, not
merely that it is present when everything works.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from ledger.api.routes import health

pytestmark = pytest.mark.kafka

#: Every dependency the API cannot serve without. Named here so adding one
#: without a check fails this file rather than passing silently.
EXPECTED_CHECKS = {"catalog", "engine", "auth", "database", "audit"}


async def test_liveness_does_not_touch_any_dependency(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """Liveness answering for a broken dependency is the point of it.

    A liveness probe that fails when the database is down gets the container
    restarted, which fixes nothing and loses the logs.
    """
    app.state.ledger.engine.close()

    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"]


async def test_readiness_reports_every_dependency(
    client: httpx.AsyncClient, analyst_token: str
) -> None:
    response = await client.get("/api/ready")
    assert response.status_code == 200

    body = response.json()
    assert body["ready"] is True
    checks = {check["name"]: check for check in body["checks"]}
    assert set(checks) == EXPECTED_CHECKS
    assert all(check["ok"] for check in checks.values())

    # Each detail names what was actually checked, so a failing probe says
    # which dependency and in what state rather than just "not ready".
    assert "rows" in checks["engine"]["detail"]
    assert "columns" in checks["catalog"]["detail"]
    assert checks["database"]["detail"]


async def test_a_broken_engine_makes_the_service_unready(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """503, and the failing check named. The endpoint that returned 200 with
    no checks in it passed every test that only asserted a status code."""
    app.state.ledger.engine.close()

    response = await client.get("/api/ready")
    assert response.status_code == 503

    body = response.json()
    assert body["ready"] is False
    failed = [check for check in body["checks"] if not check["ok"]]
    assert [check["name"] for check in failed] == ["engine"]
    assert failed[0]["detail"], "a failing check must say what went wrong"


async def test_an_unusable_signing_key_makes_the_service_unready(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this endpoint was written for.

    An empty `LEDGER_JWT_SECRET` failed every login with a 500 while the
    container reported itself healthy and Compose brought the web tier up on
    top of it.
    """

    def unusable(*args: Any, **kwargs: Any) -> tuple[str, int]:
        raise ValueError("HMAC key must not be empty")

    monkeypatch.setattr(health.jwt_helper, "issue", unusable)

    response = await client.get("/api/ready")
    assert response.status_code == 503
    failed = {c["name"] for c in response.json()["checks"] if not c["ok"]}
    assert failed == {"auth"}


async def test_a_missing_schema_makes_the_service_unready(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """A container whose migration never ran must not be declared ready.

    It answers `/api/health`, holds a live broker connection, and serves the
    catalogue -- everything except the requests people actually make.
    """
    await app.state.ledger.db_engine.dispose()

    def refuse() -> Any:
        raise RuntimeError('relation "users" does not exist')

    app.state.ledger.sessions = refuse

    response = await client.get("/api/ready")
    assert response.status_code == 503
    failed = {c["name"] for c in response.json()["checks"] if not c["ok"]}
    assert failed == {"database"}
