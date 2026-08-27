"""The single enforcement point for column-level access.

``scope_catalog`` is the only producer of a :class:`ScopedCatalog`, and every
model-facing surface takes one as an argument. There is no module-level
catalogue reachable from the tool layer, so "forgot to scope it" is a type
error rather than a silent leak.
"""

from __future__ import annotations

from ledger.catalog.models import Catalog, ScopedCatalog
from ledger.security.policy import ROLE_GRANTS, sensitivity_of
from ledger.security.principal import Principal


def scope_catalog(catalog: Catalog, principal: Principal) -> ScopedCatalog:
    """Filter ``catalog`` to the columns ``principal`` may know exist.

    Restricted columns are *removed*, not masked. A viewer asking for
    ``tip_amount`` therefore gets the same ``unknown_column`` a typo gets --
    the error cannot be used as an oracle to enumerate what is hidden.
    """
    grants = ROLE_GRANTS[principal.role.value]
    visible = {
        name: column for name, column in catalog.columns.items() if sensitivity_of(name) in grants
    }
    return ScopedCatalog(
        version=catalog.version,
        role=principal.role.value,
        columns=visible,
        stats=catalog.stats,
    )
