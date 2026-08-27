"""The prompt a viewer's model sees must not contain a restricted name anywhere.

This is a single assertion over the whole rendered surface. It fails the moment
someone adds a field to the renderer that forgets to consult the scope.
"""

from __future__ import annotations

from ledger.catalog.models import ScopedCatalog
from ledger.catalog.render import render_catalog
from ledger.security.policy import restricted_columns


def test_viewer_prompt_mentions_no_restricted_column(viewer_scope: ScopedCatalog) -> None:
    rendered = render_catalog(viewer_scope)
    for column in restricted_columns():
        assert column not in rendered, f"{column} leaked into the viewer's prompt"


def test_analyst_prompt_does_include_them(analyst_scope: ScopedCatalog) -> None:
    """The negative test is only meaningful if the positive one holds."""
    rendered = render_catalog(analyst_scope)
    assert "tip_amount" in rendered
    assert "cbd_congestion_fee" in rendered


def test_render_is_byte_stable(analyst_scope: ScopedCatalog) -> None:
    """The catalogue sits behind a prompt-cache breakpoint.

    A re-ordered dict or an interpolated timestamp would silently drop the cache
    hit rate to zero and nothing else in the system would notice.
    """
    assert render_catalog(analyst_scope) == render_catalog(analyst_scope)


def test_render_carries_the_caveats_that_prevent_wrong_answers(
    analyst_scope: ScopedCatalog,
) -> None:
    rendered = render_catalog(analyst_scope)
    # Cash tips are never recorded; a model that does not know this will report
    # a confidently wrong average tip.
    assert "cash tips" in rendered.lower()
