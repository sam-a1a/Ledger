"""Scoping is the single enforcement point, so it is asserted directly."""

from __future__ import annotations

from ledger.catalog.models import Catalog
from ledger.catalog.scope import scope_catalog
from ledger.security.policy import restricted_columns
from ledger.security.principal import Principal, Role


def test_viewer_scope_omits_every_restricted_column(catalog: Catalog) -> None:
    scope = scope_catalog(catalog, Principal(subject="v", role=Role.VIEWER))
    assert restricted_columns().isdisjoint(scope.columns)


def test_analyst_sees_restricted_but_never_internal(catalog: Catalog) -> None:
    scope = scope_catalog(catalog, Principal(subject="a", role=Role.ANALYST))
    assert "tip_amount" in scope.columns
    assert "cbd_congestion_fee" in scope.columns
    # The tenant key is plumbing, not a column, for every role.
    assert "tenant_id" not in scope.columns


def test_viewer_keeps_enough_columns_to_do_real_work(catalog: Catalog) -> None:
    """Restriction should narrow the analysis, not prevent it."""
    scope = scope_catalog(catalog, Principal(subject="v", role=Role.VIEWER))
    for column in ("fare_amount", "total_amount", "pickup_zone", "pickup_at", "trip_distance"):
        assert column in scope.columns, column


def test_scoping_is_a_strict_subset(catalog: Catalog) -> None:
    analyst = scope_catalog(catalog, Principal(subject="a", role=Role.ANALYST))
    viewer = scope_catalog(catalog, Principal(subject="v", role=Role.VIEWER))
    assert set(viewer.columns) < set(analyst.columns)


def test_helper_accessors_respect_the_scope(catalog: Catalog) -> None:
    viewer = scope_catalog(catalog, Principal(subject="v", role=Role.VIEWER))
    assert "tip_amount" not in viewer.numeric_names()
    assert viewer.get("tip_amount") is None
    assert "pickup_at" in viewer.temporal_names()
