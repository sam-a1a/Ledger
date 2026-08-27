"""Who is asking.

A ``Principal`` is constructed at the edge -- from a verified JWT over HTTP, or
from configuration for the MCP server -- and threaded through the tool layer.
Nothing below the edge reads a request, a header, or an environment variable to
decide what someone may see.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Role(StrEnum):
    """What a caller is allowed to see. Deliberately two values."""

    ANALYST = "analyst"
    VIEWER = "viewer"


class Channel(StrEnum):
    """How the request arrived.

    Recorded on every governance event, so "everything that came in over MCP"
    is a query rather than an investigation.
    """

    HTTP = "http"
    MCP = "mcp"


class Principal(BaseModel):
    """An authenticated caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    role: Role
    channel: Channel = Channel.HTTP
    #: ``None`` means "all tenants" -- the demo analyst. Any other value is
    #: injected as a mandatory WHERE predicate by the SQL compiler.
    tenant_id: int | None = None

    def __str__(self) -> str:
        tenant = "all" if self.tenant_id is None else str(self.tenant_id)
        return f"{self.subject}({self.role}, tenant={tenant}, via={self.channel})"
