"""Request-scoped dependencies.

``get_tool_context`` is the only place a :class:`ToolContext` is built for an
HTTP request, and it cannot build one without a verified principal and a scoped
catalogue. That is what makes "forgot to scope it" impossible rather than
merely unlikely.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ledger.api.state import AppState
from ledger.catalog.scope import scope_catalog
from ledger.config import Settings, get_settings
from ledger.security import jwt as jwt_helper
from ledger.security.principal import Channel, Principal
from ledger.tools.context import ToolContext

bearer = HTTPBearer(auto_error=False)


def get_state(request: Request) -> AppState:
    state: AppState | None = getattr(request.app.state, "ledger", None)
    if state is None:  # pragma: no cover - only if lifespan did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service is still starting",
        )
    return state


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token; POST /api/auth/login to obtain one",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return jwt_helper.verify(settings, credentials.credentials, channel=Channel.HTTP)
    except jwt_helper.TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_tool_context(
    principal: Annotated[Principal, Depends(get_principal)],
    state: Annotated[AppState, Depends(get_state)],
) -> AsyncIterator[ToolContext]:
    """Build a per-request context, with its own DuckDB cursor."""
    with state.engine.cursor() as cursor:
        yield ToolContext(
            principal=principal,
            scope=scope_catalog(state.catalog, principal),
            cursor=cursor,
            publisher=state.publisher,
            settings=state.settings,
        )


PrincipalDep = Annotated[Principal, Depends(get_principal)]
StateDep = Annotated[AppState, Depends(get_state)]
ToolContextDep = Annotated[ToolContext, Depends(get_tool_context)]
