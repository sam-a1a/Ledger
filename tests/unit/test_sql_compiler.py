"""Properties of every statement the compiler can emit.

The compiler is a pure function with no database handle, so thousands of
argument combinations can be checked with zero I/O. These are the assertions the
"typed tools, not SQL generation" claim actually rests on -- a lint rule cannot
express them, and an example-based test would only cover the shapes someone
thought to write down.
"""

from __future__ import annotations

import random
import re
import string

import pytest

from ledger.catalog.models import ScopedCatalog, SemanticType
from ledger.engine import sql as sqlc
from ledger.security.policy import restricted_columns
from ledger.security.principal import Principal, Role
from ledger.tools.args import Filter, Having, Metric, OrderBy

SEED = 424242
FUZZ_CASES = 400

#: Strings a hostile or confused model might put in a value position.
HOSTILE_VALUES = [
    "'; DROP TABLE ledger.trips; --",
    '" OR 1=1 --',
    "' OR '1'='1",
    "%_%",
    "\\'; DELETE FROM ledger.trips WHERE '1'='1",
    "Manhattan'); SELECT * FROM ledger.trips WHERE ('1'='1",
    "\x00truncated",
]


def _rng() -> random.Random:
    return random.Random(SEED)  # noqa: S311 - fuzz corpus, not cryptography


def _random_filter(rng: random.Random, scope: ScopedCatalog) -> Filter:
    column = rng.choice(scope.names())
    profile = scope.columns[column]
    if profile.semantic_type is SemanticType.NUMERIC:
        op = rng.choice(["=", "!=", "<", "<=", ">", ">=", "between", "is_null"])
        value = (
            [1.0, 99.0] if op == "between" else (None if op == "is_null" else rng.random() * 100)
        )
    elif profile.semantic_type is SemanticType.TEMPORAL:
        op = rng.choice([">=", "<=", "between", "is_not_null"])
        value = (
            ["2025-01-01", "2025-02-01"]
            if op == "between"
            else (None if op == "is_not_null" else "2025-01-05")
        )
    else:
        text_stored = profile.duckdb_type.upper().startswith("VARCHAR")
        choices = ["=", "!=", "in", "not_in", "is_null"]
        if text_stored:
            choices.append("contains")
        op = rng.choice(choices)
        if op in ("in", "not_in"):
            value = [rng.choice(HOSTILE_VALUES), "Manhattan"]
        elif op == "is_null":
            value = None
        else:
            value = rng.choice(HOSTILE_VALUES)
    return Filter.model_construct(column=column, op=op, value=value)


def _random_metric(rng: random.Random, scope: ScopedCatalog) -> Metric:
    numeric = scope.numeric_names()
    if numeric and rng.random() < 0.7:
        return Metric.model_construct(
            op=rng.choice(["sum", "avg", "median", "p90", "stddev", "min", "max"]),
            column=rng.choice(numeric),
            alias=None,
        )
    return Metric.model_construct(op="count", column=None, alias=None)


def _random_aggregate(rng: random.Random, scope: ScopedCatalog, principal: Principal):
    group_by = rng.sample(scope.names(), k=rng.randint(0, 3))
    metrics = [_random_metric(rng, scope) for _ in range(rng.randint(1, 4))]
    filters = [_random_filter(rng, scope) for _ in range(rng.randint(0, 4))]
    order_by = (
        [OrderBy.model_construct(key=metrics[0].default_alias(), direction="desc")]
        if rng.random() < 0.5
        else []
    )
    having = (
        [Having.model_construct(metric=metrics[0].default_alias(), op=">", value=10.0)]
        if rng.random() < 0.3
        else []
    )
    return sqlc.compile_aggregate(
        metrics=metrics,
        group_by=group_by,
        filters=filters,
        having=having,
        order_by=order_by,
        limit=rng.randint(1, 500),
        scope=scope,
        principal=principal,
    )


# --------------------------------------------------------------------------
# The five properties
# --------------------------------------------------------------------------


def test_every_query_reads_only_the_trips_relation(
    analyst_scope: ScopedCatalog, analyst: Principal
) -> None:
    rng = _rng()
    for _ in range(FUZZ_CASES):
        compiled = _random_aggregate(rng, analyst_scope, analyst)
        assert compiled.sql.count("FROM ") == 1
        assert f"FROM {sqlc.RELATION}" in compiled.sql


def test_placeholder_count_always_matches_parameter_count(
    analyst_scope: ScopedCatalog, analyst: Principal
) -> None:
    """A mismatch here is how a value silently becomes part of the statement."""
    rng = _rng()
    for _ in range(FUZZ_CASES):
        compiled = _random_aggregate(rng, analyst_scope, analyst)
        assert compiled.sql.count("?") == len(compiled.params)


def test_hostile_values_never_reach_the_sql_text(
    analyst_scope: ScopedCatalog, analyst: Principal
) -> None:
    """Injection attempts must appear in params, never in the statement."""
    rng = _rng()
    for _ in range(FUZZ_CASES):
        compiled = _random_aggregate(rng, analyst_scope, analyst)
        for hostile in HOSTILE_VALUES:
            assert hostile not in compiled.sql
        # Word boundaries matter: `dropoff_at` contains "DROP", and a naive
        # substring check would fail on a perfectly correct statement.
        statements = re.findall(r"\b[A-Z]+\b", compiled.sql.upper())
        for keyword in ("DROP", "DELETE", "INSERT", "UPDATE", "ATTACH", "COPY", "MERGE"):
            assert keyword not in statements


def test_every_aggregate_is_bounded_by_a_limit(
    analyst_scope: ScopedCatalog, analyst: Principal
) -> None:
    rng = _rng()
    for _ in range(FUZZ_CASES):
        compiled = _random_aggregate(rng, analyst_scope, analyst)
        assert compiled.sql.rstrip().endswith("LIMIT ?")


def test_tenant_predicate_is_present_and_unremovable(
    analyst_scope: ScopedCatalog,
) -> None:
    """No combination of arguments drops the row-level isolation predicate.

    The predicate is emitted by the compiler from the principal, never by a
    tool, so this is a property of the compiler rather than of any caller.
    """
    tenant_bound = Principal(subject="t", role=Role.ANALYST, tenant_id=2)
    rng = _rng()
    for _ in range(FUZZ_CASES):
        compiled = _random_aggregate(rng, analyst_scope, tenant_bound)
        assert '"tenant_id" = ?' in compiled.sql
        assert compiled.params[0] == 2


def test_unscoped_principal_gets_no_tenant_predicate(
    analyst_scope: ScopedCatalog, analyst: Principal
) -> None:
    """The demo analyst spans tenants; the predicate is absent rather than true."""
    compiled = sqlc.compile_count(filters=[], scope=analyst_scope, principal=analyst)
    assert "tenant_id" not in compiled.sql


# --------------------------------------------------------------------------
# Scope enforcement at the compiler, with no model in the loop
# --------------------------------------------------------------------------


def test_viewer_sql_never_names_a_restricted_column(
    viewer_scope: ScopedCatalog, viewer: Principal
) -> None:
    """Analyst-only columns must not appear in a viewer's SQL.

    Deliberately excludes the internal tenant key: the compiler names that one
    on purpose when it injects the row-level predicate, which the next test
    asserts positively.
    """
    rng = _rng()
    for _ in range(FUZZ_CASES):
        compiled = _random_aggregate(rng, viewer_scope, viewer)
        for column in restricted_columns():
            assert column not in compiled.sql, column


def test_compiler_refuses_an_out_of_scope_column_outright(
    viewer_scope: ScopedCatalog, viewer: Principal
) -> None:
    """The last line of defence, exercised with no validation and no model.

    This is the MCP threat model: an arbitrary JSON-RPC client hand-writing a
    call. If the compiler had a fallback that quoted the raw name, the whole
    governance claim would be decorative.
    """
    bad = Filter.model_construct(column="tip_amount", op="=", value=1)
    with pytest.raises(KeyError):
        sqlc.compile_count(filters=[bad], scope=viewer_scope, principal=viewer)


def test_column_expr_has_no_fallback_for_unknown_names(
    analyst_scope: ScopedCatalog,
) -> None:
    with pytest.raises(KeyError):
        sqlc.column_expr(analyst_scope, "no_such_column")


# --------------------------------------------------------------------------
# Identifier quoting
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", '"plain"'),
        ('has"quote', '"has""quote"'),
        ("spaced name", '"spaced name"'),
    ],
)
def test_identifier_quoting_escapes_embedded_quotes(raw: str, expected: str) -> None:
    assert sqlc.quote_ident(raw) == expected


def test_contains_compiles_to_strpos_not_like(
    analyst_scope: ScopedCatalog, analyst: Principal
) -> None:
    """LIKE would give '%' and '_' wildcard meaning in a model-supplied value."""
    predicate = Filter.model_construct(column="pickup_zone", op="contains", value="%_%")
    compiled = sqlc.compile_count(filters=[predicate], scope=analyst_scope, principal=analyst)
    assert "strpos(" in compiled.sql
    assert "LIKE" not in compiled.sql.upper()
    assert compiled.params == ["%_%"]


def test_group_by_uses_ordinals_so_identifiers_appear_once(
    analyst_scope: ScopedCatalog, analyst: Principal
) -> None:
    compiled = sqlc.compile_aggregate(
        metrics=[Metric.model_construct(op="count", column=None, alias=None)],
        group_by=["pickup_borough", "payment_type_label"],
        filters=[],
        having=[],
        order_by=[],
        limit=10,
        scope=analyst_scope,
        principal=analyst,
    )
    assert "GROUP BY 1, 2" in compiled.sql
    assert compiled.sql.count('"pickup_borough"') == 1


def test_fingerprint_is_stable_and_value_independent(
    analyst_scope: ScopedCatalog, analyst: Principal
) -> None:
    """The audit event carries this instead of the SQL, so filter values stay off the topic."""

    def build(value: str) -> str:
        predicate = Filter.model_construct(column="pickup_zone", op="=", value=value)
        return sqlc.compile_count(
            filters=[predicate], scope=analyst_scope, principal=analyst
        ).fingerprint()

    assert build("Manhattan") == build("Queens")
    assert len(build("Manhattan")) == 16


def test_no_stray_characters_from_hostile_column_names() -> None:
    """Even a name full of punctuation cannot escape the quoting."""
    nasty = '"; DROP TABLE x; --' + string.punctuation
    quoted = sqlc.quote_ident(nasty)
    assert quoted.startswith('"') and quoted.endswith('"')
    assert quoted.count('"') % 2 == 0
