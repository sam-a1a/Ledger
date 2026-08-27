"""Cache keys for the catalogue.

Two fingerprints with deliberately different lifetimes:

* :func:`dataset_fingerprint` covers the *data* and invalidates the whole
  profile. It changes whenever a file or the view changes.
* ``ColumnProfile.fingerprint`` covers one column's *shape* and is coarsely
  bucketed, so a data refresh reuses descriptions at zero cost.

Descriptions almost never invalidate; profiles do. That split is what stops
enrichment becoming a recurring bill.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ledger.catalog.models import CATALOG_SCHEMA_VERSION

BOOTSTRAP_SQL = Path(__file__).resolve().parents[1] / "engine" / "bootstrap.sql"


def dataset_fingerprint(raw_dir: Path, described: Sequence[Sequence[Any]]) -> str:
    """Hash the inputs that would change what profiling produces."""
    parts: list[str] = [f"schema_version={CATALOG_SCHEMA_VERSION}"]

    for path in sorted(raw_dir.glob("*")):
        if path.suffix in (".parquet", ".csv"):
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_size}")

    parts.extend(f"{name}:{dtype}" for name, dtype, *_ in described)
    parts.append(hashlib.sha256(BOOTSTRAP_SQL.read_bytes()).hexdigest()[:16])

    return hashlib.sha256("|".join(parts).encode()).hexdigest()
