"""Executing one tool call.

This is where the governance contract is enforced, and the ordering is the
whole design:

1. Publish ``tool_call_requested`` and **wait for acknowledgement**. If it
   cannot be recorded, return ``audit_unavailable`` and stop -- the query never
   runs. That is the difference between no un-audited *result* and no un-audited
   *data access*.
2. Validate the arguments against the caller's scoped catalogue. This is the
   authoritative RBAC gate; the JSON Schema enums are advisory, and do not exist
   at all on the MCP surface.
3. Execute.
4. Publish ``tool_call_completed`` best-effort. A bookkeeping failure must not
   fail a call whose query already succeeded, and the consumer flags an
   unmatched request as orphaned rather than losing it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from pydantic import ValidationError

from ledger.errors import EventPublishError
from ledger.governance import events
from ledger.governance.events import Outcome, PrincipalRef
from ledger.logging import get_logger
from ledger.security.policy import hidden_from
from ledger.tools import registry
from ledger.tools.args import SCOPE_CONTEXT_KEY, ArgumentError
from ledger.tools.context import ToolContext
from ledger.tools.results import ErrorCode, ToolError, ToolOutcome, ToolResult

log = get_logger(__name__)


def new_call_id() -> str:
    return f"c_{uuid.uuid7().hex[:12]}"  # type: ignore[attr-defined,unused-ignore]


def _principal_ref(ctx: ToolContext) -> PrincipalRef:
    return PrincipalRef(
        subject=ctx.principal.subject,
        role=ctx.principal.role,
        tenant_id=ctx.principal.tenant_id,
    )


def _argument_error_to_tool_error(
    exc: ArgumentError, tool: str, scope_names: list[str]
) -> ToolError:
    """Translate a validation failure into something the model can act on."""
    if exc.code == "unknown_column" and exc.column is not None:
        # The offending name and the valid set are carried on the exception
        # rather than recovered from its message. Parsing the prose produced a
        # nonsense error whenever the valid set was not the dataset -- as it is
        # for `plot`, whose columns are those of a previously computed result.
        available = exc.available if exc.available is not None else scope_names
        return ToolError.unknown_column(
            exc.column,
            available=available,
            field=exc.field,
            tool=tool,
            of=exc.context_label,
        )
    code = {
        "type_mismatch": ErrorCode.TYPE_MISMATCH,
        "cardinality_exceeded": ErrorCode.CARDINALITY_EXCEEDED,
        "unknown_metric": ErrorCode.UNKNOWN_METRIC,
        "unknown_column": ErrorCode.UNKNOWN_COLUMN,
        "internal": ErrorCode.INTERNAL,
    }.get(exc.code, ErrorCode.INVALID_ARGUMENT)
    return ToolError(
        error=code,
        message=str(exc),
        field=exc.field,
        suggestions=exc.suggestions,
        tool=tool,
        retryable=code is not ErrorCode.INTERNAL,
    )


def _first_argument_error(exc: ValidationError) -> ArgumentError | None:
    for error in exc.errors():
        cause = error.get("ctx", {}).get("error")
        if isinstance(cause, ArgumentError):
            return cause
    return None


def _validation_error_to_tool_error(
    exc: ValidationError, tool: str, scope_names: list[str]
) -> ToolError:
    if (argument_error := _first_argument_error(exc)) is not None:
        return _argument_error_to_tool_error(argument_error, tool, scope_names)

    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    if first["type"] == "extra_forbidden":
        return ToolError.invalid_argument(
            f"{location!r} is not an argument of {tool}. Check the tool schema.",
            field=location,
        )
    return ToolError.invalid_argument(f"{first['msg']} (at {location}).", field=location)


def _attempted_hidden_columns(raw_args: Any, role: str) -> list[str]:
    """Find hidden column names anywhere in the arguments, at any depth.

    Used only to enrich the audit record. The caller is still told the column
    does not exist.
    """
    hidden = hidden_from(role)
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node in hidden:
            found.add(node)

    walk(raw_args)
    return sorted(found)


async def execute(
    tool_name: str,
    raw_args: dict[str, Any],
    ctx: ToolContext,
    *,
    call_id: str | None = None,
) -> ToolOutcome:
    """Run one tool call under the full governance contract."""
    call_id = call_id or new_call_id()
    spec = registry.get(tool_name)
    if spec is None:
        return ToolError.invalid_argument(
            f"{tool_name!r} is not a tool. Available: {', '.join(registry.names())}.",
            suggestions=registry.names(),
        )

    principal = _principal_ref(ctx)
    request = events.requested(
        principal=principal,
        channel=ctx.principal.channel,
        tool=tool_name,
        args=raw_args,
        call_id=call_id,
        conversation_id=ctx.conversation_id,
        message_id=ctx.message_id,
        catalog_version=ctx.catalog_version,
    )

    # --- phase one: no un-audited data access ---------------------------
    try:
        await ctx.publisher.publish(request)
    except EventPublishError as exc:
        log.error("refusing %s: audit log unreachable (%s)", tool_name, exc)
        return ToolError.audit_unavailable(str(exc))

    started = time.perf_counter()
    scope_names = ctx.scope.names()

    try:
        ctx.raise_if_cancelled()
        try:
            args = spec.args_model.model_validate(raw_args, context={SCOPE_CONTEXT_KEY: ctx.scope})
        except ValidationError as exc:
            error = _validation_error_to_tool_error(exc, tool_name, scope_names)
            await _record_denial_or_error(ctx, request, raw_args, error, started)
            return error
        except ArgumentError as exc:
            error = _argument_error_to_tool_error(exc, tool_name, scope_names)
            await _record_denial_or_error(ctx, request, raw_args, error, started)
            return error

        try:
            result = await spec.handler(args, ctx)
        except ArgumentError as exc:
            # Raised by handlers for checks that need the engine or the
            # catalogue, such as the cardinality guard.
            error = _argument_error_to_tool_error(exc, tool_name, scope_names)
            await _record_denial_or_error(ctx, request, raw_args, error, started)
            return error

    except asyncio.CancelledError:
        await ctx.publisher.publish_best_effort(
            events.completed(
                request,
                outcome=Outcome.CANCELLED,
                duration_ms=_elapsed_ms(started),
                error_code="cancelled",
            )
        )
        raise

    duration_ms = _elapsed_ms(started)
    result.duration_ms = duration_ms
    await ctx.publisher.publish_best_effort(
        events.completed(
            request,
            outcome=Outcome.ALLOW,
            duration_ms=duration_ms,
            row_count=result.row_count,
            result_id=result.result_id,
        )
    )
    return result


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def _record_denial_or_error(
    ctx: ToolContext,
    request: events.GovernanceEvent,
    raw_args: dict[str, Any],
    error: ToolError,
    started: float,
) -> None:
    """Record the outcome, distinguishing a refusal from a mistake.

    A caller who named a hidden column is told it does not exist, while the
    audit log records exactly what was reached for. That asymmetry is what makes
    the denial non-probeable without making it invisible to an operator.
    """
    attempted = _attempted_hidden_columns(raw_args, ctx.principal.role.value)
    if attempted:
        await ctx.publisher.publish_best_effort(
            events.access_denied(
                principal=_principal_ref(ctx),
                channel=ctx.principal.channel,
                tool=request.tool or "",
                attempted_columns=attempted,
                call_id=request.call_id or "",
                conversation_id=ctx.conversation_id,
            )
        )

    await ctx.publisher.publish_best_effort(
        events.completed(
            request,
            outcome=Outcome.DENY if attempted else Outcome.ERROR,
            duration_ms=_elapsed_ms(started),
            error_code=error.error.value,
        )
    )


__all__ = ["ToolError", "ToolResult", "execute", "new_call_id"]
