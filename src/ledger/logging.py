"""Logging setup.

The one rule that matters: under the MCP stdio transport, stdout *is* the
JSON-RPC channel. A single stray byte on it corrupts framing and the client
reports an opaque disconnect. So every handler writes to stderr, always.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install a stderr-only handler. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
