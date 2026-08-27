"""The four-step description chain, and the provenance it records.

The chain exists because the project has to work with no API key, and that must
be an ordinary path rather than a degraded one.
"""

from __future__ import annotations

import json
from pathlib import Path

from ledger.catalog import describe
from ledger.catalog.models import Catalog, Description, DescriptionSource
from ledger.config import Settings


def test_seed_file_covers_every_column(catalog: Catalog, settings: Settings) -> None:
    """The seed is the guaranteed floor, so a gap in it is a real defect."""
    seed = describe.load_seed(describe_seed_path(settings))
    missing = set(catalog.columns) - set(seed)
    assert not missing, f"no hand-written description for: {sorted(missing)}"


def describe_seed_path(settings: Settings) -> Path:
    from ledger.catalog.store import seed_path

    return seed_path(settings)


def test_without_a_key_everything_resolves_from_seed(
    catalog: Catalog, settings: Settings, tmp_path: Path
) -> None:
    resolved = describe.resolve(
        catalog,
        seed_path=describe_seed_path(settings),
        generated_path=tmp_path / "absent.json",
    )
    assert {d.source for d in resolved.values()} == {DescriptionSource.SEED}


def test_generated_cache_wins_when_the_fingerprint_matches(
    catalog: Catalog, settings: Settings, tmp_path: Path
) -> None:
    column = catalog.columns["trip_distance"]
    generated = tmp_path / "generated.json"
    generated.write_text(
        json.dumps(
            {
                "trip_distance": {
                    "text": "Generated text.",
                    "source": "llm",
                    "model": "claude-opus-5",
                    "profile_fingerprint": column.fingerprint(),
                }
            }
        )
    )
    resolved = describe.resolve(
        catalog, seed_path=describe_seed_path(settings), generated_path=generated
    )
    assert resolved["trip_distance"].source is DescriptionSource.LLM
    assert resolved["trip_distance"].text == "Generated text."
    # Everything else still comes from the seed.
    assert resolved["fare_amount"].source is DescriptionSource.SEED


def test_stale_generated_cache_falls_back_rather_than_lying(
    catalog: Catalog, settings: Settings, tmp_path: Path
) -> None:
    """A description written against a different profile is not reused."""
    generated = tmp_path / "generated.json"
    generated.write_text(
        json.dumps(
            {
                "trip_distance": {
                    "text": "Written against an older dataset.",
                    "source": "llm",
                    "profile_fingerprint": "0000000000000000",
                }
            }
        )
    )
    resolved = describe.resolve(
        catalog, seed_path=describe_seed_path(settings), generated_path=generated
    )
    assert resolved["trip_distance"].source is DescriptionSource.SEED


def test_derived_is_the_last_resort_and_is_still_informative(
    catalog: Catalog, tmp_path: Path
) -> None:
    resolved = describe.resolve(
        catalog,
        seed_path=tmp_path / "no-seed.yaml",
        generated_path=tmp_path / "no-generated.json",
    )
    assert {d.source for d in resolved.values()} == {DescriptionSource.DERIVED}
    text = resolved["fare_amount"].text
    assert "Numeric" in text and "range" in text


def test_enrichment_input_contains_profiles_and_never_rows(catalog: Catalog) -> None:
    """The enrichment model is governed by the same rule as the chat model."""
    payload = describe.profile_payload(catalog)
    allowed = {
        "column",
        "type",
        "semantic_type",
        "null_fraction",
        "approx_distinct",
        "min",
        "max",
        "sample_values",
    }
    for entry in payload:
        assert set(entry) == allowed
        # Samples are bounded and only present for low-cardinality columns.
        assert len(entry["sample_values"]) <= 5


def test_description_counts_report_provenance(catalog: Catalog) -> None:
    counts = catalog.description_counts()
    assert counts["missing"] == 0
    assert counts[DescriptionSource.SEED.value] == len(catalog.columns)


def test_round_trip_of_a_generated_cache(tmp_path: Path) -> None:
    path = tmp_path / "generated.json"
    original = {"x": Description(text="t", source=DescriptionSource.LLM, profile_fingerprint="abc")}
    describe.save_generated(path, original)
    assert describe.load_generated(path)["x"].text == "t"
