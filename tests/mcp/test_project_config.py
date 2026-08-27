"""The committed `.mcp.json`, which Claude Code reads on opening the project.

A config file that is right on the day it is written and silently wrong three
commits later is worse than no config file, because it fails at the point where
someone is least equipped to debug it. Each field is therefore checked against
the thing it has to agree with.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MCP_CONFIG = REPO / ".mcp.json"


@pytest.fixture(scope="module")
def server() -> dict[str, Any]:
    config = json.loads(MCP_CONFIG.read_text())
    assert set(config) == {"mcpServers"}
    servers = config["mcpServers"]
    assert list(servers) == ["ledger"], "one server, named for the project"
    return dict(servers["ledger"])


def test_the_command_is_a_console_script_the_package_actually_declares(
    server: dict[str, Any],
) -> None:
    """Renaming the entry point without updating this would break on first use."""
    assert server["command"] == "uv"
    assert server["args"][-1] == "ledger-mcp"

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert "ledger-mcp" in pyproject["project"]["scripts"]


def test_it_runs_from_the_project_rather_than_wherever_it_was_launched(
    server: dict[str, Any],
) -> None:
    assert "--directory" in server["args"]


def test_the_role_matches_the_server_default(server: dict[str, Any]) -> None:
    """A stdio client has no auth layer, so both must default to the safe role."""
    from ledger.config import Settings

    assert server["env"]["LEDGER_MCP_ROLE"] == Settings().mcp_role == "viewer"


def test_the_broker_address_is_the_one_compose_publishes(server: dict[str, Any]) -> None:
    """The detail people get wrong.

    In-network services reach `kafka:9092`; anything on the host -- which an MCP
    server launched by an editor is -- needs the published listener. Reading it
    out of the Compose file means the two cannot drift apart.
    """
    configured = server["env"]["LEDGER_KAFKA_BOOTSTRAP_SERVERS"]
    host, _, port = configured.rpartition(":")
    assert host in ("localhost", "127.0.0.1")

    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    published = {p.split(":")[0] for p in compose["services"]["kafka"]["ports"]}
    assert port in published, f"{configured} is not published by Compose ({published})"


def test_it_does_not_try_to_build_a_catalogue_on_startup(server: dict[str, Any]) -> None:
    """An editor-launched server must start promptly and touch no API."""
    assert server["env"]["LEDGER_CATALOG_MODE"] == "offline"


def test_no_secret_is_committed_in_the_config(server: dict[str, Any]) -> None:
    raw = json.dumps(server)
    assert not re.search(r"sk-[a-zA-Z0-9_-]{8,}", raw)
    for key in server["env"]:
        assert "KEY" not in key.upper() or key == "LEDGER_MCP_ROLE"


def test_the_readme_documents_how_to_connect_it() -> None:
    readme = (REPO / "README.md").read_text()
    assert ".mcp.json" in readme
    assert "claude mcp" in readme or "Claude Code" in readme
