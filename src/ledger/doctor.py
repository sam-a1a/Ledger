"""``ledger doctor`` -- check the configuration before anything tries to use it.

Each dependency is checked independently and reported with what to do about it.
The alternative, which this replaces, is a thirty-second startup hang followed
by a stack trace that names the last thing to fail rather than the thing that
is actually wrong.

Nothing here raises. A check that cannot run is a finding, not a crash.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ledger.config import DEV_JWT_SECRET, AuthMode, CatalogMode, ModelBackend, Settings


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(slots=True)
class Finding:
    name: str
    status: Status
    detail: str
    #: What to actually do. Omitted when there is nothing to fix.
    fix: str | None = None


def _check_data(settings: Settings) -> Finding:
    raw = settings.raw_dir
    parquet = sorted(raw.glob("yellow_tripdata_*.parquet")) if raw.exists() else []
    zones = raw / "taxi_zone_lookup.csv"

    if not parquet or not zones.exists():
        return Finding(
            "dataset",
            Status.FAIL,
            f"no dataset in {raw}",
            "make fetch   (~180 MB; or point LEDGER_DATA_DIR at tests/fixtures/data)",
        )
    size = sum(p.stat().st_size for p in parquet) / 1e6
    return Finding("dataset", Status.OK, f"{len(parquet)} file(s), {size:,.0f} MB in {raw}")


def _check_catalog(settings: Settings) -> Finding:
    from ledger.catalog import store

    path = store.catalog_path(settings)
    if not path.exists():
        status = Status.FAIL if settings.catalog_mode is CatalogMode.OFFLINE else Status.WARN
        return Finding(
            "catalogue",
            status,
            f"not built ({path})",
            "ledger catalog build",
        )
    try:
        catalog = store.load(path)
    except Exception as exc:
        return Finding("catalogue", Status.FAIL, str(exc)[:120], "ledger catalog build")

    counts = catalog.description_counts()
    provenance = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    return Finding(
        "catalogue",
        Status.OK,
        f"{len(catalog.columns)} columns, {catalog.stats.row_count:,} rows ({provenance})",
    )


def _check_auth(settings: Settings) -> Finding:
    from ledger.security import jwt as jwt_helper
    from ledger.security.principal import Role

    try:
        token, _ = jwt_helper.issue(settings, subject="doctor", role=Role.VIEWER)
        jwt_helper.verify(settings, token)
    except Exception as exc:
        return Finding(
            "auth",
            Status.FAIL,
            str(exc)[:120],
            'set LEDGER_JWT_SECRET (python -c "import secrets; print(secrets.token_urlsafe(48))")',
        )

    if settings.jwt_secret == DEV_JWT_SECRET:
        if settings.auth_mode is AuthMode.STRICT:
            return Finding(
                "auth",
                Status.FAIL,
                "strict mode with the built-in development key",
                "set LEDGER_JWT_SECRET to a real value",
            )
        return Finding(
            "auth",
            Status.WARN,
            "signing with the built-in development key (fine locally)",
            "set LEDGER_JWT_SECRET before exposing this anywhere",
        )
    return Finding("auth", Status.OK, f"{settings.auth_mode} mode, signing key configured")


def _check_model(settings: Settings) -> Finding:
    backend = settings.resolved_backend()
    key = settings.anthropic_api_key

    # Order matters: `resolved_backend()` honours an explicit LEDGER_MODEL even
    # without a key, so the missing-key case has to be tested before the
    # happy one or a broken configuration reports itself as fine.
    if backend is ModelBackend.ANTHROPIC and not key:
        return Finding(
            "model",
            Status.FAIL,
            "LEDGER_MODEL=anthropic but ANTHROPIC_API_KEY is not set",
            "set ANTHROPIC_API_KEY, or unset LEDGER_MODEL to use the scripted model",
        )
    if backend is ModelBackend.ANTHROPIC and key:
        return Finding(
            "model",
            Status.OK,
            f"{settings.anthropic_model} (key ...{key[-4:]}, effort {settings.anthropic_effort})",
        )
    return Finding(
        "model",
        Status.WARN,
        "scripted -- answers are canned, everything under them is real",
        "set ANTHROPIC_API_KEY to use a real model (https://platform.claude.com/)",
    )


def _check_kafka(settings: Settings) -> Finding:
    """Kafka is a hard dependency, so this is the check most worth running early."""

    async def probe() -> str | None:
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            request_timeout_ms=3000,
        )
        try:
            await asyncio.wait_for(producer.start(), timeout=8.0)
            await producer.stop()
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"[:120]
        return None

    error = asyncio.run(probe())
    if error:
        return Finding(
            "audit log",
            Status.FAIL,
            f"no broker at {settings.kafka_bootstrap_servers} ({error})",
            "docker compose up -d kafka   (then use localhost:29092 from the host)",
        )
    return Finding(
        "audit log", Status.OK, f"broker reachable at {settings.kafka_bootstrap_servers}"
    )


CHECKS: tuple[Callable[[Settings], Finding], ...] = (
    _check_data,
    _check_catalog,
    _check_model,
    _check_auth,
    _check_kafka,
)

_SYMBOL = {Status.OK: "ok  ", Status.WARN: "warn", Status.FAIL: "FAIL"}


def run(settings: Settings, *, out: object = None) -> list[Finding]:
    return [check(settings) for check in CHECKS]


def report(findings: list[Finding]) -> str:
    width = max(len(f.name) for f in findings)
    lines: list[str] = []
    for finding in findings:
        lines.append(f"  [{_SYMBOL[finding.status]}] {finding.name:<{width}}  {finding.detail}")
        if finding.fix and finding.status is not Status.OK:
            lines.append(f"           {' ' * width}  -> {finding.fix}")
    return "\n".join(lines)


def main(settings: Settings | None = None) -> int:
    from ledger.config import get_settings

    settings = settings or get_settings()
    findings = run(settings)

    env_file = Path(".env")
    source = "and .env" if env_file.exists() else "(no .env file; using defaults)"
    sys.stdout.write(f"Ledger configuration -- from the environment {source}\n\n")
    sys.stdout.write(report(findings) + "\n\n")

    failed = [f for f in findings if f.status is Status.FAIL]
    warned = [f for f in findings if f.status is Status.WARN]

    if failed:
        sys.stdout.write(f"{len(failed)} check(s) failed. Ledger will not start until they pass.\n")
        return 1
    if warned:
        sys.stdout.write(f"Ready, with {len(warned)} thing(s) worth knowing about.\n")
        return 0
    sys.stdout.write("Ready.\n")
    return 0
