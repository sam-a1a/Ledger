"""Column sensitivity, and the one function that applies it.

The policy is coherent rather than arbitrary: **the breakdown of what a fare is
made of is finance-restricted; the headline fare and trip operations are open.**
A viewer can answer "busiest pickup zones" and "average fare by borough" but not
"what share of the fare is tip" or "what is congestion pricing collecting".

Since ``cbd_congestion_fee`` is restricted, the congestion-pricing question is an
analyst-only answer -- which is the demonstration worth having.
"""

from __future__ import annotations

from enum import StrEnum


class Sensitivity(StrEnum):
    """How guarded a column is."""

    #: Anyone authenticated may see it.
    PUBLIC = "public"
    #: Finance-grade detail. Analysts only.
    RESTRICTED = "restricted"
    #: Never exposed to any role. Plumbing the model must not know exists.
    INTERNAL = "internal"


#: Anything absent from this map is PUBLIC. Declared as data so the policy is
#: diffable in review, and mirrored onto every ColumnProfile so a drift between
#: the two fails at startup rather than silently widening access.
COLUMN_SENSITIVITY: dict[str, Sensitivity] = {
    # --- fee-level revenue breakdown: analyst only ------------------------
    "tip_amount": Sensitivity.RESTRICTED,
    "extra": Sensitivity.RESTRICTED,
    "mta_tax": Sensitivity.RESTRICTED,
    "tolls_amount": Sensitivity.RESTRICTED,
    "improvement_surcharge": Sensitivity.RESTRICTED,
    "congestion_surcharge": Sensitivity.RESTRICTED,
    "airport_fee": Sensitivity.RESTRICTED,
    "cbd_congestion_fee": Sensitivity.RESTRICTED,
    # --- plumbing: nobody ---------------------------------------------------
    # The tenant key is applied by the SQL compiler from the principal. If the
    # model could name it, it could try to filter on it -- so it is not a column
    # as far as any caller is concerned.
    "tenant_id": Sensitivity.INTERNAL,
}

#: Which sensitivities each role may see.
ROLE_GRANTS: dict[str, frozenset[Sensitivity]] = {
    "analyst": frozenset({Sensitivity.PUBLIC, Sensitivity.RESTRICTED}),
    "viewer": frozenset({Sensitivity.PUBLIC}),
}


def sensitivity_of(column: str) -> Sensitivity:
    return COLUMN_SENSITIVITY.get(column, Sensitivity.PUBLIC)


def visible_to(column: str, role: str) -> bool:
    """Whether ``role`` may know that ``column`` exists at all."""
    return sensitivity_of(column) in ROLE_GRANTS[role]


def restricted_columns() -> frozenset[str]:
    """Columns an analyst may see and a viewer may not.

    Distinct from :func:`internal_columns`: an internal column is never
    selectable by anyone, but the SQL compiler still names the tenant key when
    it injects the row-level predicate. Conflating the two makes a leakage
    assertion fail on the compiler doing exactly its job.
    """
    return frozenset(
        name for name, level in COLUMN_SENSITIVITY.items() if level is Sensitivity.RESTRICTED
    )


def internal_columns() -> frozenset[str]:
    """Columns no role may name. Plumbing, applied by the compiler."""
    return frozenset(
        name for name, level in COLUMN_SENSITIVITY.items() if level is Sensitivity.INTERNAL
    )


def hidden_from(role: str) -> frozenset[str]:
    """Every column ``role`` must not learn exists, for whatever reason."""
    return frozenset(name for name in COLUMN_SENSITIVITY if not visible_to(name, role))
