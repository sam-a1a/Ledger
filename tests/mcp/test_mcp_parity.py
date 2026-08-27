"""The MCP surface must not drift from the one the chat application advertises.

Eight hand-written wrappers is the readable choice, but hand-written means it
can drift. This is the guard: the tool names, and the arguments each accepts,
are compared against the registry rather than trusted.
"""

from __future__ import annotations

import inspect
import logging
import sys

import pytest

from ledger.mcp_server import server as mcp_server
from ledger.tools import registry

TOOL_FUNCTIONS = {
    "list_columns": mcp_server.list_columns,
    "describe_column": mcp_server.describe_column,
    "count_rows": mcp_server.count_rows,
    "aggregate": mcp_server.aggregate,
    "top_n": mcp_server.top_n,
    "timeseries": mcp_server.timeseries,
    "distribution": mcp_server.distribution,
    "plot": mcp_server.plot,
}


def _unwrap(function: object) -> object:
    """Reach the underlying function, whatever the decorator returned."""
    for attribute in ("fn", "func", "__wrapped__", "handler"):
        inner = getattr(function, attribute, None)
        if callable(inner):
            return inner
    return function


def test_mcp_exposes_exactly_the_registry_tools() -> None:
    assert set(TOOL_FUNCTIONS) == set(registry.names())


@pytest.mark.parametrize("name", sorted(TOOL_FUNCTIONS))
def test_each_wrapper_accepts_the_registry_arguments(name: str) -> None:
    """Every argument model field must be reachable through the wrapper."""
    spec = registry.get(name)
    assert spec is not None

    signature = inspect.signature(_unwrap(TOOL_FUNCTIONS[name]))  # type: ignore[arg-type]
    parameters = set(signature.parameters)
    expected = set(spec.args_model.model_fields)

    missing = expected - parameters
    assert not missing, f"{name} wrapper cannot express: {sorted(missing)}"

    extra = parameters - expected
    assert not extra, f"{name} wrapper accepts arguments the tool does not: {sorted(extra)}"


@pytest.mark.parametrize("name", sorted(TOOL_FUNCTIONS))
def test_required_arguments_match(name: str) -> None:
    """A required field must not be optional in the wrapper, or a call can 500."""
    spec = registry.get(name)
    assert spec is not None
    signature = inspect.signature(_unwrap(TOOL_FUNCTIONS[name]))  # type: ignore[arg-type]

    for field_name, field in spec.args_model.model_fields.items():
        parameter = signature.parameters[field_name]
        wrapper_required = parameter.default is inspect.Parameter.empty
        assert wrapper_required == field.is_required(), (
            f"{name}.{field_name}: registry required={field.is_required()}, "
            f"wrapper required={wrapper_required}"
        )


@pytest.mark.parametrize("name", sorted(TOOL_FUNCTIONS))
def test_every_wrapper_documents_itself_for_the_model(name: str) -> None:
    """`@mcp.tool()` publishes the docstring; an empty one is a silent regression."""
    doc = inspect.getdoc(_unwrap(TOOL_FUNCTIONS[name]))
    assert doc and len(doc) > 40


def test_nothing_logs_to_stdout() -> None:
    """Under the stdio transport, stdout is the JSON-RPC channel.

    A single stray byte corrupts framing, and the client reports an opaque
    disconnect with nothing pointing at the cause.
    """
    from ledger.logging import configure_logging

    configure_logging()
    for handler in logging.getLogger().handlers:
        stream = getattr(handler, "stream", None)
        assert stream is not sys.stdout


def test_the_mcp_role_defaults_to_the_restrictive_one() -> None:
    """A stdio client has no auth layer, so the default must be the safe one."""
    from ledger.config import Settings

    assert Settings().mcp_role == "viewer"
