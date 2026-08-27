"""``ledger`` command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ledger import __version__
from ledger.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description="Ledger admin CLI.")
    parser.add_argument("--version", action="version", version=f"ledger {__version__}")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
