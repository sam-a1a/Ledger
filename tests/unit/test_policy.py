"""The sensitivity declaration itself."""

from __future__ import annotations

from ledger.security.policy import (
    COLUMN_SENSITIVITY,
    ROLE_GRANTS,
    Sensitivity,
    hidden_from,
    internal_columns,
    restricted_columns,
    sensitivity_of,
    visible_to,
)


def test_unlisted_columns_default_to_public() -> None:
    assert sensitivity_of("trip_distance") is Sensitivity.PUBLIC
    assert sensitivity_of("a_column_that_does_not_exist") is Sensitivity.PUBLIC


def test_fee_breakdown_is_restricted_but_headline_fare_is_not() -> None:
    """The policy has a shape: what a fare is *made of* is restricted; the fare is not."""
    for column in ("tip_amount", "cbd_congestion_fee", "congestion_surcharge", "airport_fee"):
        assert sensitivity_of(column) is Sensitivity.RESTRICTED, column
    for column in ("fare_amount", "total_amount", "trip_distance", "pickup_zone"):
        assert sensitivity_of(column) is Sensitivity.PUBLIC, column


def test_tenant_key_is_internal_and_visible_to_nobody() -> None:
    """The compiler applies the tenant predicate; no role may name the column."""
    assert sensitivity_of("tenant_id") is Sensitivity.INTERNAL
    for role in ROLE_GRANTS:
        assert not visible_to("tenant_id", role)


def test_analyst_sees_restricted_and_viewer_does_not() -> None:
    assert visible_to("tip_amount", "analyst")
    assert not visible_to("tip_amount", "viewer")


def test_no_role_is_granted_internal() -> None:
    for role, grants in ROLE_GRANTS.items():
        assert Sensitivity.INTERNAL not in grants, role


def test_the_two_hidden_sets_are_distinct_and_together_cover_non_public() -> None:
    """`restricted` is analyst-only; `internal` is nobody-at-all.

    They must not be conflated: the SQL compiler legitimately names the internal
    tenant key when it injects the row-level predicate, so a leakage assertion
    written against the union would fail on correct behaviour.
    """
    assert restricted_columns().isdisjoint(internal_columns())
    non_public = {n for n, s in COLUMN_SENSITIVITY.items() if s is not Sensitivity.PUBLIC}
    assert restricted_columns() | internal_columns() == non_public


def test_hidden_from_viewer_includes_both_kinds() -> None:
    hidden = hidden_from("viewer")
    assert "tip_amount" in hidden  # analyst-only
    assert "tenant_id" in hidden  # internal
    assert "fare_amount" not in hidden


def test_hidden_from_analyst_is_internal_only() -> None:
    assert hidden_from("analyst") == internal_columns()
