"""Process configuration, read once from the environment.

Every knob the deployment can turn lives here. Nothing else reads ``os.environ``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    """How much a password-reset response is allowed to give away.

    In `dev` the reset token comes back in the response body, so a reset can be
    completed with no mail server in the loop. In `strict` it is logged and
    nothing else, and the signing key must not be the built-in one.
    """

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
    months: Annotated[tuple[str, ...], NoDecode] = ("2024-12", "2025-01", "2025-02")
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

    @field_validator("analyst_emails", "cors_origins", "months", mode="before")
    @classmethod
    def _split_commas(cls, value: object) -> object:
        """Accept `a@x.com,@y.com` as well as a JSON list.

        Pydantic parses a tuple field from the environment as JSON, and a
        container environment or `.env` line is written as a plain comma list
        by anyone who has not read that far. Failing on it is a startup crash
        with a JSON parse error, which names the wrong problem.

        `NoDecode` on the fields is what makes this validator reachable at
        all: without it the JSON decode happens in the settings source, before
        any validator runs, and the crash arrives with no way to intercept it.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            # Decoded here rather than handed back: `NoDecode` means nothing
            # else will, and returning the raw string produced a "should be a
            # valid tuple" error on the form that used to be the only one
            # accepted.
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"expected a JSON list or a comma-separated list: {exc}") from exc
        return tuple(part.strip() for part in text.split(",") if part.strip())

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
    cors_origins: Annotated[tuple[str, ...], NoDecode] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    )
    #: Addresses granted the analyst role on signup. An entry is either a full
    #: address (`ops@example.com`) or a domain (`@example.com`), matched
    #: case-insensitively. Without it the only way to promote someone is the
    #: first-account rule below, which forces a deployment to care about who
    #: signs up first and gives the test suite an order dependency it should
    #: not have.
    analyst_emails: Annotated[tuple[str, ...], NoDecode] = ()
    # --- oauth ------------------------------------------------------------
    # Absent by default. A provider without both halves is not advertised and
    # its endpoints 404, so a deployment that has not set one up does not show
    # a button that cannot work.
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = ""
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    #: Where the provider sends the browser back to. Built from configuration
    #: rather than from the request: the `Host` header is attacker-controlled,
    #: and deriving a callback URL from it is how a flow gets redirected
    #: off-site. Must match what is registered with the provider.
    public_api_base: str = "http://127.0.0.1:8077"
    #: Where to land after signing in. Also the fallback when a requested
    #: destination is not one of the trusted origins.
    public_web_base: str = "http://127.0.0.1:5173"

    mcp_role: str = "viewer"
    mcp_tenant: str | None = None

    # --- database ---------------------------------------------------------
    #: Accounts and conversations. Separate from the analytical store by
    #: design: that one is read-only, in-memory, and shared between processes.
    database_url: str = "postgresql+asyncpg://ledger:ledger@localhost:5455/ledger"
    database_echo: bool = False
    #: Applied at startup in development so a clean clone works; production
    #: runs `alembic upgrade head` as a deliberate step.
    database_auto_migrate: bool = True

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
    #: Where unpublished events are journalled. Operational state, not dataset,
    #: so it is configured separately: the API serves its data read-only and
    #: must still be able to write here when the broker is unreachable.
    state_dir: Path | None = None
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

    @property
    def state_path(self) -> Path:
        """Writable operational state: journal, avatars, anything mutable."""
        return self.state_dir if self.state_dir is not None else self.data_dir / "audit"

    @property
    def journal_path(self) -> Path:
        base = self.state_dir if self.state_dir is not None else self.data_dir / "audit"
        return base / "journal.ndjson"

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
        if not self.months:
            # An empty comma list reads as "not configured", which is the right
            # reading for the optional lists and the wrong one here: the engine
            # would come up over no parquet at all and every query would return
            # nothing, healthily.
            raise ConfigurationError(
                "LEDGER_MONTHS is empty, so there is no data to serve. "
                'Set it to a comma-separated list, for example "2024-12,2025-01".'
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
