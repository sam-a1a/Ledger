"""Phase two of enrichment: resolving each column's one-line description.

A four-step chain rather than an LLM call with a fallback, because the project
has to work with no API key configured and that must not be a degraded path:

1. **Generated cache** -- reuse if it was written against this column's current
   profile fingerprint. Free, needs no key.
2. **LLM** -- one batched request for every column at once, and only when
   enrichment was explicitly asked for *and* a key exists.
3. **Seed file** -- hand-written, committed, complete. The default today and the
   guaranteed floor forever.
4. **Derived** -- a sentence assembled from the profile. Only reachable for a
   column nobody has described yet, and it logs a warning naming the gap.

Every column records which step produced it, so the distinction is visible
rather than hidden.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ledger.catalog.models import (
    Catalog,
    ColumnProfile,
    Description,
    DescriptionSource,
    SemanticType,
)
from ledger.errors import CatalogUnavailableError
from ledger.logging import get_logger

log = get_logger(__name__)

#: Bumped when the enrichment prompt changes, invalidating generated entries.
DESCRIBE_PROMPT_VERSION = 1

SYSTEM_PROMPT = (
    "You write one-line data-dictionary entries for a governed analytics catalogue. "
    "You are given only statistical profiles of columns -- never any rows. "
    "Describe what each column means for an analyst who has not seen this dataset. "
    "State only what the profile and the column name support; never speculate. "
    "Keep each description under 140 characters and write it as a single sentence."
)


def load_seed(path: Path) -> dict[str, Description]:
    """Read the committed hand-written descriptions."""
    if not path.exists():
        return {}
    raw: dict[str, dict[str, Any]] = yaml.safe_load(path.read_text()) or {}
    return {
        name: Description(
            text=entry["text"],
            source=DescriptionSource.SEED,
            unit=entry.get("unit"),
            caveat=entry.get("caveat"),
        )
        for name, entry in raw.items()
    }


def load_generated(path: Path) -> dict[str, Description]:
    """Read the LLM-generated cache, keyed by column name."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {name: Description.model_validate(entry) for name, entry in raw.items()}


def save_generated(path: Path, descriptions: dict[str, Description]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        name: json.loads(d.model_dump_json(exclude_none=True))
        for name, d in sorted(descriptions.items())
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def derive(column: ColumnProfile) -> Description:
    """Last resort: assemble a sentence from the profile itself."""
    bits: list[str] = [f"{column.semantic_type.value.capitalize()} column"]
    if column.null_fraction:
        bits.append(f"{column.null_fraction:.1%} null")
    if column.semantic_type is SemanticType.NUMERIC and column.min_value is not None:
        bits.append(f"range {column.min_value:g} to {column.max_value:g}")
        if column.p50 is not None:
            bits.append(f"median {column.p50:g}")
    elif column.approx_distinct:
        bits.append(f"{column.approx_distinct:,} distinct values")
    return Description(text=", ".join(bits) + ".", source=DescriptionSource.DERIVED)


def profile_payload(catalog: Catalog) -> list[dict[str, Any]]:
    """The only thing the enrichment model is shown. Profiles, never rows."""
    return [
        {
            "column": c.name,
            "type": c.duckdb_type,
            "semantic_type": c.semantic_type.value,
            "null_fraction": round(c.null_fraction, 4),
            "approx_distinct": c.approx_distinct,
            "min": str(c.min_value) if c.min_value is not None else None,
            "max": str(c.max_value) if c.max_value is not None else None,
            "sample_values": [str(v) for v in c.sample_values[:5]],
        }
        for c in sorted(catalog.columns.values(), key=lambda c: c.name)
    ]


def resolve(
    catalog: Catalog,
    *,
    seed_path: Path,
    generated_path: Path,
    generated_override: dict[str, Description] | None = None,
) -> dict[str, Description]:
    """Run the resolution chain for every column in ``catalog``."""
    generated = (
        generated_override if generated_override is not None else load_generated(generated_path)
    )
    seed = load_seed(seed_path)

    resolved: dict[str, Description] = {}
    derived_columns: list[str] = []

    for name, column in catalog.columns.items():
        fingerprint = column.fingerprint()
        cached = generated.get(name)
        if cached is not None and cached.profile_fingerprint == fingerprint:
            resolved[name] = cached
        elif name in seed:
            resolved[name] = seed[name]
        else:
            resolved[name] = derive(column)
            derived_columns.append(name)

    if derived_columns:
        log.warning(
            "no written description for %d column(s); using derived text: %s",
            len(derived_columns),
            ", ".join(sorted(derived_columns)),
        )
    return resolved


def apply(catalog: Catalog, descriptions: dict[str, Description]) -> Catalog:
    """Attach resolved descriptions to a catalogue."""
    for name, column in catalog.columns.items():
        column.description = descriptions.get(name)
    return catalog


async def enrich_with_llm(catalog: Catalog, *, model: str, api_key: str) -> dict[str, Description]:
    """Generate descriptions for every column in a single batched request.

    One request, not one per column: the enrichment model sees the whole profile
    table at once, which is both cheaper and produces more consistent phrasing
    because it can see how columns relate.
    """
    from anthropic import AsyncAnthropic
    from pydantic import BaseModel

    class ColumnDescription(BaseModel):
        column: str
        description: str
        unit: str | None = None
        caveat: str | None = None

    class Descriptions(BaseModel):
        columns: list[ColumnDescription]

    client = AsyncAnthropic(api_key=api_key)
    response = await client.messages.parse(
        model=model,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Write a data-dictionary entry for each column below. "
                    "This is the NYC TLC yellow taxi trip record dataset.\n\n"
                    + json.dumps(profile_payload(catalog), indent=2)
                ),
            }
        ],
        output_format=Descriptions,
    )

    parsed = response.parsed_output
    if parsed is None:
        raise CatalogUnavailableError(
            "enrichment model returned no parsed output; descriptions were not generated"
        )
    now = datetime.now(UTC)
    return {
        entry.column: Description(
            text=entry.description,
            source=DescriptionSource.LLM,
            unit=entry.unit,
            caveat=entry.caveat,
            model=model,
            generated_at=now,
            profile_fingerprint=catalog.columns[entry.column].fingerprint(),
        )
        for entry in parsed.columns
        if entry.column in catalog.columns
    }
