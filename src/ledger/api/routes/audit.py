"""Reading the governance log.

This is the *durable* record. The trace panel renders from the SSE stream while
an answer is being written -- consumer lag would otherwise race the answer -- and
reconciles against this afterwards. Same data, two paths, and the difference
between them is exactly what proves the log is real.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ledger.api.deps import PrincipalDep, StateDep
from ledger.governance import store

router = APIRouter(tags=["audit"])


class AuditResponse(BaseModel):
    events: list[dict[str, Any]]
    count: int


class SummaryResponse(BaseModel):
    by_tool: list[dict[str, Any]]


@router.get("/audit", response_model=AuditResponse)
async def audit(
    principal: PrincipalDep,
    state: StateDep,
    conversation_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> AuditResponse:
    """Events this caller is allowed to read.

    Authorisation is applied in the query from the principal, exactly as in the
    tool layer -- an analyst sees their tenant's activity, anyone else sees only
    their own.
    """
    with state.engine.cursor() as cursor:
        events = store.read_events(
            cursor,
            state.settings.data_dir,
            principal,
            conversation_id=conversation_id,
            limit=limit,
        )
    return AuditResponse(events=events, count=len(events))


@router.get("/audit/summary", response_model=SummaryResponse)
async def audit_summary(principal: PrincipalDep, state: StateDep) -> SummaryResponse:
    with state.engine.cursor() as cursor:
        return SummaryResponse(by_tool=store.summarise(cursor, state.settings.data_dir, principal))
