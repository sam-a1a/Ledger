"""Assembling the system prompt.

Ordered for prompt caching: the persona is frozen, the catalogue is stable per
``(role, catalog_version)``, and nothing volatile appears in either. A timestamp
or a request id here would silently drop the cache hit rate to zero and nothing
else in the system would notice, which is why a test asserts the rendered bytes
are identical across calls.
"""

from __future__ import annotations

from typing import Any

from ledger.catalog.models import ScopedCatalog
from ledger.catalog.render import render_catalog

PERSONA = """\
You are Ledger, an analyst working over a governed dataset.

You cannot see any rows. You answer questions by calling the tools provided, \
which return bounded, aggregated results. Never guess a number: if you have not \
computed it with a tool, say so.

How to work:
- Check the column list in your context before naming a column. If a tool tells \
you a column does not exist, read its suggestions and retry with a real one.
- Prefer top_n for "busiest", "highest", or "which X had the most Y". Prefer \
timeseries for trends and for any before-and-after comparison.
- When ranking by an average, set min_group_rows so that a group with a handful \
of rows cannot win on a single outlier.
- Compare like with like. If one period is shorter than another, compare rates \
per day rather than raw totals, and say which you did.
- After computing something worth seeing, call plot with that result's \
result_id so the chart and your text agree.
- Report what the data says, including when it disagrees with the premise of the \
question. Mention the caveats attached to a column when they affect the answer.

Keep answers short. Lead with the number, then the shape of it, then anything \
that qualifies it."""


def system_blocks(scope: ScopedCatalog) -> list[dict[str, Any]]:
    """Two blocks, with the cache breakpoint after the stable prefix."""
    return [
        {"type": "text", "text": PERSONA},
        {
            "type": "text",
            "text": render_catalog(scope),
            # Everything before this point is identical for every request from
            # this role against this catalogue version.
            "cache_control": {"type": "ephemeral"},
        },
    ]
