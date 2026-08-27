"""Liveness and readiness.

``/api/health`` says the process is up. ``/api/ready`` says it can actually
serve a question — catalogue loaded, DuckDB answering, broker reachable. Compose
gates the web container on readiness, not liveness.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from ledger import __version__

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
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=ReadyResponse)
async def ready(response: Response) -> ReadyResponse:
    # Real checks are wired in as their subsystems land (M1 engine, M2 catalogue,
    # M5 broker). Until then readiness is honest about having nothing to check.
    checks: list[ReadyCheck] = []
    all_ok = all(c.ok for c in checks)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(ready=all_ok, checks=checks)
