"""Reading the materialised audit log.

Parquet read through DuckDB, rather than a database. The requirement was no
Postgres, and a DuckDB *file* would take an exclusive writer lock that stops the
API reading while the consumer writes. Append-only parquet has neither problem:
the consumer writes a temp file and renames it, and readers in other processes
see whole files or nothing.

Delivery is at-least-once, so the same event can be materialised twice after a
crash between flush and offset commit. Deduplicating on ``event_id`` at read
time makes the view effectively exactly-once without needing transactions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from ledger.governance.events import GovernanceEvent
from ledger.security.principal import Principal, Role

#: Requests with no completion after this are surfaced as `unknown` rather than
#: silently dropped -- a gap should be visible, not invisible.
ORPHAN_AFTER_SECONDS = 60


def events_dir(data_dir: Path) -> Path:
    return data_dir / "audit" / "events"


def _glob(data_dir: Path) -> str:
    return str(events_dir(data_dir) / "**" / "*.parquet")


def _authorisation_clause(principal: Principal) -> tuple[str, list[Any]]:
    """Who may read whose events.

    Enforced in SQL from the principal, exactly like the tool layer: an analyst
    sees their tenant's activity, anyone else sees only their own.
    """
    if principal.role is Role.ANALYST and principal.tenant_id is None:
        return "", []
    if principal.role is Role.ANALYST:
        return "AND (principal_tenant_id = ? OR principal_subject = ?)", [
            principal.tenant_id,
            principal.subject,
        ]
    return "AND principal_subject = ?", [principal.subject]


def read_events(
    connection: duckdb.DuckDBPyConnection,
    data_dir: Path,
    principal: Principal,
    *,
    conversation_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return deduplicated events, newest last, scoped to what may be read."""
    directory = events_dir(data_dir)
    if not directory.exists() or not any(directory.rglob("*.parquet")):
        return []

    auth_clause, auth_params = _authorisation_clause(principal)
    conversation_clause = "AND conversation_id = ?" if conversation_id else ""
    params: list[Any] = [_glob(data_dir), *auth_params]
    if conversation_id:
        params.append(conversation_id)
    params.append(limit)

    rows = connection.execute(
        f"""
        SELECT * FROM read_parquet(?, union_by_name => true)
        WHERE 1 = 1 {auth_clause} {conversation_clause}
        QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY occurred_at) = 1
        ORDER BY occurred_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    columns = [d[0] for d in (connection.description or [])]
    return [dict(zip(columns, row, strict=True)) for row in reversed(rows)]


def summarise(
    connection: duckdb.DuckDBPyConnection, data_dir: Path, principal: Principal
) -> list[dict[str, Any]]:
    """Counts by tool and outcome -- the shape an operator actually wants."""
    directory = events_dir(data_dir)
    if not directory.exists() or not any(directory.rglob("*.parquet")):
        return []

    auth_clause, auth_params = _authorisation_clause(principal)
    rows = connection.execute(
        f"""
        WITH deduped AS (
            SELECT * FROM read_parquet(?, union_by_name => true)
            WHERE 1 = 1 {auth_clause}
            QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY occurred_at) = 1
        )
        SELECT tool, outcome, count(*) AS calls,
               round(median(duration_ms), 1) AS p50_ms,
               round(quantile_cont(duration_ms, 0.95), 1) AS p95_ms
        FROM deduped
        WHERE event_type = 'tool_call_completed' AND tool IS NOT NULL
        GROUP BY 1, 2 ORDER BY calls DESC
        """,
        [_glob(data_dir), *auth_params],
    ).fetchall()
    columns = [d[0] for d in (connection.description or [])]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def flatten(event: GovernanceEvent) -> dict[str, Any]:
    """One flat row per event, so parquet stays queryable without unnesting."""
    payload = event.model_dump(mode="json")
    principal = payload.pop("principal")
    payload["principal_subject"] = principal["subject"]
    payload["principal_role"] = principal["role"]
    payload["principal_tenant_id"] = principal["tenant_id"]
    payload["args"] = None if event.args is None else event.model_dump_json(include={"args"})
    payload["attempted_columns"] = (
        None if event.attempted_columns is None else ",".join(event.attempted_columns)
    )
    return payload
