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
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.api.state import AppState
from ledger.catalog.scope import scope_catalog
from ledger.config import Settings, get_settings
from ledger.db.base import User
from ledger.security import jwt as jwt_helper
from ledger.security.principal import Channel, Principal, Role
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
    principal: Annotated[Principal, Depends(get_account_principal)],
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


async def get_session(
    state: Annotated[AppState, Depends(get_state)],
) -> AsyncIterator[AsyncSession]:
    """A session per request, committed on success and rolled back otherwise."""
    from ledger.db.session import session_scope

    async with session_scope(state.sessions) as session:
        yield session


async def get_current_user(
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """The account behind the token.

    A token that decodes but names no live account is rejected: deleting an
    account must actually end its sessions, and an unexpired token outliving
    the account it belonged to is the whole reason to look the account up
    rather than trust the claims.
    """
    from ledger.accounts import service as accounts

    user = await accounts.get_by_id(session, principal.subject)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="account no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_account_principal(
    user: Annotated[User, Depends(get_current_user)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """A principal whose role comes from the account, not from the token.

    The token carries a role because the dev-login endpoint issues one, but a
    stale token must never outrank the account it names -- so the account wins.
    """
    return principal.model_copy(update={"role": Role(user.role), "tenant_id": user.tenant_id})


PrincipalDep = Annotated[Principal, Depends(get_principal)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]
StateDep = Annotated[AppState, Depends(get_state)]
ToolContextDep = Annotated[ToolContext, Depends(get_tool_context)]
