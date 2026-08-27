"""``ledger`` command-line entry point.

Deliberately thin. It exists so a milestone can be verified from a shell without
booting the API, not to become a second front end.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ledger import __version__
from ledger.config import get_settings
from ledger.errors import LedgerError
from ledger.logging import configure_logging, get_logger

log = get_logger(__name__)


def _cmd_query(args: argparse.Namespace) -> int:
    """Run raw SQL against the normalised views.

    An operator affordance, not a product surface: nothing the model can reach
    goes through here. The tool layer is the only path the model gets.
    """
    from ledger.engine.duck import Engine

    settings = get_settings()
    engine = Engine.create(settings)
    try:
        with engine.cursor() as cursor:
            cursor.execute(args.sql)
            columns = [d[0] for d in (cursor.description or [])]
            rows = cursor.fetchall()
    finally:
        engine.close()

    if not columns:
        return 0
    widths = [
        max(len(c), *(len(str(r[i])) for r in rows)) if rows else len(c)
        for i, c in enumerate(columns)
    ]
    sys.stdout.write("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)) + "\n")
    sys.stdout.write("  ".join("-" * w for w in widths) + "\n")
    for row in rows[: args.limit]:
        sys.stdout.write(
            "  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)) + "\n"
        )
    if len(rows) > args.limit:
        sys.stdout.write(f"... {len(rows) - args.limit} more row(s)\n")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    """Print the resolved configuration -- the fastest way to see why a run behaved."""
    settings = get_settings()
    for key, value in (
        ("version", __version__),
        ("data_dir", settings.data_dir),
        ("months", ",".join(settings.months)),
        ("model backend", f"{settings.model_backend} -> {settings.resolved_backend()}"),
        ("demo mode", settings.demo_mode),
        ("catalog mode", settings.catalog_mode),
        ("auth mode", settings.auth_mode),
        ("kafka", settings.kafka_bootstrap_servers),
    ):
        sys.stdout.write(f"{key:>16}: {value}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description="Ledger admin CLI.")
    parser.add_argument("--version", action="version", version=f"ledger {__version__}")
    sub = parser.add_subparsers(dest="command")

    query = sub.add_parser("query", help="Run SQL against the normalised views")
    query.add_argument("sql")
    query.add_argument("--limit", type=int, default=50, help="Rows to print (default: %(default)s)")
    query.set_defaults(func=_cmd_query)

    info = sub.add_parser("info", help="Show resolved configuration")
    info.set_defaults(func=_cmd_info)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    try:
        result: int = args.func(args)
    except LedgerError as exc:
        log.error("%s", exc)
        return 1
    return result


if __name__ == "__main__":
    sys.exit(main())
