"""Process configuration, read once from the environment.

Every knob the deployment can turn lives here. Nothing else reads ``os.environ``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ledger.errors import ConfigurationError

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Placeholder signing key. Safe only because `AuthMode.STRICT` rejects it.
DEV_JWT_SECRET = "dev-secret-not-for-production"  # noqa: S105 - guarded, see Settings


class ModelBackend(StrEnum):
    """Which implementation of ``ModelClient`` to construct.

    ``AUTO`` resolves to :attr:`ANTHROPIC` when an API key is present and
    :attr:`FAKE` otherwise, so a clean clone runs with zero configuration.
    """

    AUTO = "auto"
    ANTHROPIC = "anthropic"
    FAKE = "fake"
    REPLAY = "replay"


class AuthMode(StrEnum):
    """Whether the unauthenticated dev-login endpoint is available."""

    DEV = "dev"
    STRICT = "strict"


class CatalogMode(StrEnum):
    """How the column catalogue is obtained at startup."""

    AUTO = "auto"
    REBUILD = "rebuild"
    OFFLINE = "offline"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LEDGER_",
        env_file=".env",
        extra="ignore",
    )

    # --- data -------------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"
    months: tuple[str, ...] = ("2024-12", "2025-01", "2025-02")
    materialize: bool = False
    duckdb_threads: int = 4
    duckdb_memory_limit: str = "2GB"

    # --- catalogue --------------------------------------------------------
    catalog_mode: CatalogMode = CatalogMode.AUTO
    profile_months: int = 3
    #: Overrides for the committed description files. Tests point these at
    #: temporary copies; nothing else should need them.
    seed_descriptions_path: Path | None = None
    generated_descriptions_path: Path | None = None

    # --- model ------------------------------------------------------------
    model_backend: ModelBackend = Field(default=ModelBackend.AUTO, alias="LEDGER_MODEL")
    anthropic_model: str = "claude-opus-5"
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    max_turns: int = 8
    show_thinking: bool = False

    # --- auth -------------------------------------------------------------
    # A known, obviously-fake default so a clean clone runs. `auth_mode=strict`
    # refuses to start with it still in place -- see `validate_for_startup`.
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 60 * 60 * 12
    auth_mode: AuthMode = AuthMode.DEV
    mcp_role: str = "viewer"
    mcp_tenant: str | None = None

    # --- kafka ------------------------------------------------------------
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_tool_calls: str = "ledger.tool-calls"
    kafka_topic_access_denied: str = "ledger.access-denied"
    kafka_client_id: str = "ledger-api"
    kafka_consumer_group: str = "ledger-audit"
    kafka_request_timeout_ms: int = 5_000
    #: How long to keep retrying a broker at startup before exiting non-zero.
    #: Bounded retry, then fail -- not a degraded mode.
    kafka_bootstrap_timeout_s: float = 30.0

    # --- limits -----------------------------------------------------------
    model_max_rows: int = 200
    cache_max_rows: int = 2_000
    max_group_cardinality: int = 1_000

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def catalog_dir(self) -> Path:
        return self.data_dir / "catalog"

    def resolved_backend(self) -> ModelBackend:
        """Collapse ``AUTO`` into a concrete backend."""
        if self.model_backend is not ModelBackend.AUTO:
            return self.model_backend
        return ModelBackend.ANTHROPIC if self.anthropic_api_key else ModelBackend.FAKE

    def validate_for_startup(self) -> None:
        """Refuse configurations that are unsafe outside a demo.

        Called from the app lifespan, not from ``__init__``: importing the
        settings must never raise, or the CLI and the test suite become
        hostage to a deployment concern.
        """
        if self.auth_mode is AuthMode.STRICT and self.jwt_secret == DEV_JWT_SECRET:
            raise ConfigurationError(
                "LEDGER_AUTH_MODE=strict requires a real LEDGER_JWT_SECRET; "
                "the built-in development key is still in place."
            )

    @property
    def demo_mode(self) -> bool:
        """True when answers come from the scripted fake rather than a real model."""
        return self.resolved_backend() in (ModelBackend.FAKE, ModelBackend.REPLAY)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
