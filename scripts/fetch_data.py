"""Download the NYC TLC yellow-taxi dataset into ``data/raw``.

Idempotent: a file whose size already matches the server's ``Content-Length``
is left alone, so re-running after a partial download resumes cheaply and
``docker compose up`` on a warm volume costs one HEAD request per month.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from ledger.config import get_settings
from ledger.logging import configure_logging, get_logger

log = get_logger(__name__)

TRIP_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_{month}.parquet"
ZONES_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"

_CHUNK = 1 << 20


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def download(client: httpx.Client, url: str, dest: Path, *, force: bool = False) -> bool:
    """Fetch ``url`` to ``dest``. Returns True if bytes were transferred."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    head = client.head(url, follow_redirects=True)
    head.raise_for_status()
    expected = int(head.headers.get("content-length", 0))

    if not force and dest.exists() and expected and dest.stat().st_size == expected:
        log.info("%-34s up to date (%s)", dest.name, _human(expected))
        return False

    # Write to a sibling temp file and rename, so an interrupted run never
    # leaves a truncated parquet that looks complete to the next invocation.
    tmp = dest.with_suffix(dest.suffix + ".part")
    written = 0
    with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes(_CHUNK):
                handle.write(chunk)
                written += len(chunk)

    if expected and written != expected:
        tmp.unlink(missing_ok=True)
        raise OSError(f"{dest.name}: expected {expected} bytes, received {written}")

    tmp.replace(dest)
    log.info("%-34s downloaded (%s)", dest.name, _human(written))
    return True


def fetch(months: tuple[str, ...], raw_dir: Path, *, force: bool = False) -> None:
    with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        download(client, ZONES_URL, raw_dir / "taxi_zone_lookup.csv", force=force)
        for month in months:
            download(
                client,
                TRIP_URL.format(month=month),
                raw_dir / f"yellow_tripdata_{month}.parquet",
                force=force,
            )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    settings = get_settings()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--months",
        default=",".join(settings.months),
        help="Comma-separated YYYY-MM list (default: %(default)s)",
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if present")
    args = parser.parse_args(argv)

    months = tuple(m.strip() for m in args.months.split(",") if m.strip())
    log.info("fetching %d month(s) into %s", len(months), settings.raw_dir)
    try:
        fetch(months, settings.raw_dir, force=args.force)
    except (httpx.HTTPError, OSError) as exc:
        log.error("download failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
