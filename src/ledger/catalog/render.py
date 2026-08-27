"""Rendering a scoped catalogue into system-prompt text.

This is what the model reads instead of the data. Two properties matter:

* **It is scoped.** Only columns the role may see appear, so a restricted column
  is not merely unmentioned -- the model has no reason to believe it exists.
* **It is byte-stable** for a given ``(role, catalog_version)``. The prompt sits
  behind a cache breakpoint, so a re-ordered dict or an interpolated timestamp
  would silently destroy the cache hit rate and nothing else would notice.
"""

from __future__ import annotations

from ledger.catalog.models import ColumnProfile, ScopedCatalog

#: Above this many visible columns, drop the statistics and let the model call
#: `describe_column` for detail. Implemented now, though the taxi dataset does
#: not reach it, so the branch is exercised rather than aspirational.
WIDE_CATALOG_THRESHOLD = 60

#: Sample values are truncated hard: they are orientation, not data.
MAX_SAMPLES = 6
MAX_SAMPLE_CHARS = 60


def _format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _range_or_samples(column: ColumnProfile) -> str:
    if column.min_value is not None and column.max_value is not None:
        lo, hi = _format_value(column.min_value), _format_value(column.max_value)
        if column.p50 is not None:
            return f"{lo} to {hi} (median {_format_value(column.p50)})"
        return f"{lo} to {hi}"
    if column.sample_values:
        rendered = ", ".join(_format_value(v) for v in column.sample_values[:MAX_SAMPLES])
        if len(rendered) > MAX_SAMPLE_CHARS:
            rendered = rendered[:MAX_SAMPLE_CHARS].rsplit(",", 1)[0] + ", ..."
        return rendered
    return ""


def _description_text(column: ColumnProfile) -> str:
    if column.description is None:
        return ""
    text = column.description.text
    if column.description.caveat:
        text = f"{text} Caveat: {column.description.caveat}"
    return text


def render_catalog(scope: ScopedCatalog) -> str:
    """Render the visible columns as a compact, stable table."""
    names = scope.names()
    wide = len(names) > WIDE_CATALOG_THRESHOLD

    header = (
        f"Dataset: NYC yellow taxi trips. {scope.stats.row_count:,} rows, "
        f"{len(names)} columns available to you."
    )

    lines: list[str] = [header, ""]
    if wide:
        lines.append("column | type | description")
        lines.append("--- | --- | ---")
        for name in names:
            column = scope.columns[name]
            lines.append(f"{name} | {column.semantic_type.value} | {_description_text(column)}")
        lines.append("")
        lines.append("Call describe_column for nulls, cardinality, ranges, and sample values.")
        return "\n".join(lines)

    lines.append("column | type | null% | approx distinct | range or samples | description")
    lines.append("--- | --- | --- | --- | --- | ---")
    for name in names:
        column = scope.columns[name]
        lines.append(
            " | ".join(
                (
                    name,
                    column.semantic_type.value,
                    f"{column.null_fraction * 100:.1f}",
                    f"{column.approx_distinct:,}",
                    _range_or_samples(column),
                    _description_text(column),
                )
            )
        )
    return "\n".join(lines)
