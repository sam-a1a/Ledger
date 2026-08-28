"""Liveness and readiness.

``/api/health`` says the process is up. ``/api/ready`` says it can actually
serve a question, which is a different claim and the one Compose gates on.

Readiness that always returns 200 is worse than none: an empty JWT signing key
once made every login fail with a 500 while this endpoint reported the container
healthy and Compose brought the web tier up on top of it. So each check here
exercises the thing it names.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select

from ledger import __version__
from ledger.api.deps import StateDep
from ledger.config import get_settings
from ledger.db.base import User
from ledger.engine.duck import run_scalar
from ledger.security import jwt as jwt_helper
from ledger.security.principal import Role

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ReadyCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class ReadyResponse(BaseModel):
    ready: bool
    checks: list[ReadyCheck]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness only. Deliberately does not touch any dependency."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=ReadyResponse)
async def ready(response: Response, state: StateDep) -> ReadyResponse:
    checks: list[ReadyCheck] = []

    # The catalogue: without it the model has nothing to reason about.
    checks.append(
        ReadyCheck(
            name="catalog",
            ok=bool(state.catalog.columns),
            detail=f"{len(state.catalog.columns)} columns, version {state.catalog.version}",
        )
    )

    # The engine: a real query, not a handle check.
    try:
        with state.engine.cursor() as cursor:
            rows = await run_scalar(cursor, "SELECT count(*) FROM ledger.trips")
        checks.append(ReadyCheck(name="engine", ok=True, detail=f"{int(rows or 0):,} rows"))
    except Exception as exc:
        checks.append(ReadyCheck(name="engine", ok=False, detail=str(exc)[:200]))

    # Token signing: an empty or unusable key fails every login with a 500,
    # which is precisely the failure that made this endpoint worth writing.
    settings = get_settings()
    try:
        token, _ = jwt_helper.issue(settings, subject="readiness", role=Role.VIEWER)
        jwt_helper.verify(settings, token)
        checks.append(ReadyCheck(name="auth", ok=True, detail="signing key usable"))
    except Exception as exc:
        checks.append(ReadyCheck(name="auth", ok=False, detail=str(exc)[:200]))

    # The database: queried, not merely connected. Accounts and conversations
    # live here, and the application cannot serve a single request without the
    # schema -- so readiness that ignores it can report healthy on a container
    # whose migration never ran. The release smoke job passed exactly that way
    # before accounts landed.
    try:
        async with state.sessions() as session:
            await session.execute(select(func.count()).select_from(User))
        checks.append(ReadyCheck(name="database", ok=True, detail="schema present"))
    except Exception as exc:
        checks.append(ReadyCheck(name="database", ok=False, detail=str(exc)[:200]))

    # The broker: connected at startup, and the producer must still be live.
    producer_ok = getattr(state.producer, "_closed", False) is not True
    checks.append(
        ReadyCheck(
            name="audit",
            ok=producer_ok,
            detail=settings.kafka_bootstrap_servers,
        )
    )

    all_ok = all(check.ok for check in checks)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(ready=all_ok, checks=checks)
