"""Process configuration, read once from the environment.

Every knob the deployment can turn lives here. Nothing else reads ``os.environ``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ledger.errors import ConfigurationError

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Placeholder signing key. Safe only because `AuthMode.STRICT` rejects it.
#: At least 32 bytes so HS256 meets RFC 7518 section 3.2 -- a shorter key works
#: but PyJWT warns, and a warning nobody can act on is worse than none.
DEV_JWT_SECRET = "ledger-development-signing-key-not-for-production"  # noqa: S105

#: RFC 7518 section 3.2: an HMAC key should be at least as long as the digest.
MIN_JWT_SECRET_BYTES = 32


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
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        # Fields with an explicit alias are otherwise settable only by that
        # alias, which makes `Settings(catalog_mode=...)` in a test read as
        # working while doing nothing.
        populate_by_name=True,
        # `model_backend` collides with pydantic's protected `model_` namespace,
        # and the collision is silent: the keyword is accepted and discarded.
        # The field name is the right one for what it holds, so clear the
        # namespace rather than rename it to something vaguer.
        protected_namespaces=(),
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
    #: low | medium | high | xhigh | max. Medium suits a tool-calling loop
    #: over a small closed tool surface; raise it if answers get sloppy.
    anthropic_effort: str = "medium"
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    @field_validator("anthropic_api_key", "mcp_tenant", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty value as absent rather than as an override.

        `.env` files and container environments both express "not configured"
        as an empty string, and taking that literally is how an empty value
        silently replaces a working default.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("jwt_secret", mode="before")
    @classmethod
    def _blank_secret_falls_back(cls, value: object) -> object:
        """A blank signing key means "not configured", not "sign with nothing".

        Taking it literally is exactly how every login came to fail with
        "HMAC key must not be empty" while the container reported itself
        healthy. Falling back keeps the type honest; `AuthMode.STRICT` still
        refuses to start on the development key.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEV_JWT_SECRET
        return value

    max_turns: int = 8
    #: Milliseconds between token chunks from the scripted model. Zero in tests,
    #: where speed matters; a small value for the demo and for the one E2E spec
    #: that proves the answer renders incrementally rather than in one blob.
    fake_token_delay_ms: int = 0
    show_thinking: bool = False

    # --- auth -------------------------------------------------------------
    # A known, obviously-fake default so a clean clone runs. `auth_mode=strict`
    # refuses to start with it still in place -- see `validate_for_startup`.
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 60 * 60 * 12
    auth_mode: AuthMode = AuthMode.DEV
    #: Browser origins allowed to call the API cross-origin. Needed only in
    #: development, where the app is served by Vite and talks to the API
    #: directly -- in production nginx serves both from one origin and this
    #: stays empty.
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    )
    mcp_role: str = "viewer"
    mcp_tenant: str | None = None

    # --- kafka ------------------------------------------------------------
    #: The host-facing listener, because that is what a developer running the
    #: API outside Docker reaches. Compose overrides this with `kafka:9092`
    #: for in-network services. Defaulting to the in-network address is the
    #: single most common way this is misconfigured.
    kafka_bootstrap_servers: str = "localhost:29092"
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
        if not self.jwt_secret.strip():
            # Compose's `KEY: ${VAR:-}` form sets an empty string rather than
            # leaving the variable unset, which silently overrides the default.
            raise ConfigurationError(
                "LEDGER_JWT_SECRET is empty. If this came from a container "
                "environment, note that `KEY: ${VAR:-}` sets an empty value "
                "rather than leaving the variable unset; use the list form."
            )
        if self.auth_mode is AuthMode.STRICT:
            if self.jwt_secret == DEV_JWT_SECRET:
                raise ConfigurationError(
                    "LEDGER_AUTH_MODE=strict requires a real LEDGER_JWT_SECRET; "
                    "the built-in development key is still in place."
                )
            if len(self.jwt_secret.encode()) < MIN_JWT_SECRET_BYTES:
                raise ConfigurationError(
                    f"LEDGER_JWT_SECRET must be at least {MIN_JWT_SECRET_BYTES} bytes "
                    f"for HS256 (RFC 7518 section 3.2); got "
                    f"{len(self.jwt_secret.encode())}."
                )

    @property
    def demo_mode(self) -> bool:
        """True when answers come from the scripted fake rather than a real model."""
        return self.resolved_backend() in (ModelBackend.FAKE, ModelBackend.REPLAY)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
