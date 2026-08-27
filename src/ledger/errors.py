"""Exception types raised outside the tool layer.

Tool-layer failures are *values* (``ToolError``), not exceptions — the model has
to read them. These are the ones that mean the process is misconfigured.
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for Ledger's own failures."""


class ConfigurationError(LedgerError):
    """The process cannot start with the configuration it was given."""


class CatalogUnavailableError(LedgerError):
    """The column catalogue could not be loaded or built."""


class DataUnavailableError(LedgerError):
    """The parquet dataset is missing or unreadable."""


class EventPublishError(LedgerError):
    """A governance event could not be published.

    This fails the tool call closed: we do not serve data we could not audit.
    """
