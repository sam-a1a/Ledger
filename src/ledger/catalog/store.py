"""Loading and saving the catalogue.

Startup never profiles and never calls an LLM. It *loads*, and fails fast if
what it loads is missing or stale. Building the catalogue is an explicit CLI
step, which keeps startup deterministic, keeps CI free, and stops a broken API
key from becoming a startup failure.
"""

from __future__ import annotations

from pathlib import Path

from ledger.catalog.models import Catalog
from ledger.config import REPO_ROOT, CatalogMode, Settings
from ledger.errors import CatalogUnavailableError
from ledger.logging import get_logger

log = get_logger(__name__)

#: Descriptions live with the code; the profiled catalogue lives with the data.
REPO_CATALOG_DIR = REPO_ROOT / "data" / "catalog"

CATALOG_FILENAME = "catalog.json"
SEED_FILENAME = "descriptions.seed.yaml"
GENERATED_FILENAME = "descriptions.generated.json"


def catalog_path(settings: Settings) -> Path:
    return settings.catalog_dir / CATALOG_FILENAME


def seed_path(settings: Settings) -> Path:
    """Hand-written descriptions ship with the code, not with the data.

    They describe the *schema*, which is fixed by ``bootstrap.sql`` -- so they
    apply equally to the real download and to the test fixture. Resolving them
    relative to the data directory would silently fall through to derived text
    whenever anyone pointed the app at a different dataset.
    """
    return settings.seed_descriptions_path or (REPO_CATALOG_DIR / SEED_FILENAME)


def generated_path(settings: Settings) -> Path:
    """The LLM cache is likewise committed alongside the code."""
    return settings.generated_descriptions_path or (REPO_CATALOG_DIR / GENERATED_FILENAME)


def save(catalog: Catalog, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(catalog.model_dump_json(indent=2) + "\n")
    counts = catalog.description_counts()
    log.info(
        "wrote %s (%d columns; descriptions: %s)",
        path,
        len(catalog.columns),
        ", ".join(f"{k}={v}" for k, v in counts.items() if v),
    )


def load(path: Path) -> Catalog:
    if not path.exists():
        raise CatalogUnavailableError(f"no catalogue at {path}. Run `ledger catalog build` first.")
    return Catalog.model_validate_json(path.read_text())


def load_for_startup(settings: Settings, expected_fingerprint: str | None = None) -> Catalog:
    """Load the catalogue according to ``LEDGER_CATALOG_MODE``.

    ``offline`` -- the mode CI, Docker, and Playwright use -- refuses to profile
    or call an LLM under any circumstance, so a missing file is a loud failure
    rather than a slow silent rebuild.
    """
    path = catalog_path(settings)
    catalog = load(path)

    if (
        settings.catalog_mode is CatalogMode.OFFLINE
        and expected_fingerprint is not None
        and catalog.dataset_fingerprint != expected_fingerprint
    ):
        raise CatalogUnavailableError(
            f"catalogue at {path} was built for a different dataset "
            f"({catalog.dataset_fingerprint[:12]} != {expected_fingerprint[:12]}). "
            "Run `ledger catalog build`, or point LEDGER_DATA_DIR at the matching data."
        )

    log.info(
        "catalogue %s loaded (%d columns, %s rows)",
        catalog.version,
        len(catalog.columns),
        f"{catalog.stats.row_count:,}",
    )
    return catalog
