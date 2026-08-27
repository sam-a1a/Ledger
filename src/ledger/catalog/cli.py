"""``ledger catalog`` subcommands."""

from __future__ import annotations

import argparse
import asyncio
import sys

from ledger.catalog import describe as describe_mod
from ledger.catalog import store
from ledger.catalog.models import Catalog, Description
from ledger.catalog.profile import profile_dataset
from ledger.catalog.render import render_catalog
from ledger.catalog.scope import scope_catalog
from ledger.config import Settings, get_settings
from ledger.engine.duck import Engine
from ledger.errors import ConfigurationError
from ledger.logging import get_logger
from ledger.security.principal import Principal, Role

log = get_logger(__name__)


def build_catalog(settings: Settings, *, enrich: bool = False) -> Catalog:
    """Profile the dataset and resolve every column's description."""
    engine = Engine.create(settings)
    try:
        with engine.cursor() as cursor:
            catalog = profile_dataset(cursor, raw_dir=settings.raw_dir)
    finally:
        engine.close()

    generated: dict[str, Description] | None = None
    if enrich:
        if not settings.anthropic_api_key:
            raise ConfigurationError(
                "--enrich needs ANTHROPIC_API_KEY. Without it, descriptions resolve "
                "from the committed seed file, which is complete."
            )
        log.info("generating descriptions with %s", settings.anthropic_model)
        fresh = asyncio.run(
            describe_mod.enrich_with_llm(
                catalog,
                model=settings.anthropic_model,
                api_key=settings.anthropic_api_key,
            )
        )
        cached = describe_mod.load_generated(store.generated_path(settings))
        cached.update(fresh)
        describe_mod.save_generated(store.generated_path(settings), cached)
        generated = cached

    descriptions = describe_mod.resolve(
        catalog,
        seed_path=store.seed_path(settings),
        generated_path=store.generated_path(settings),
        generated_override=generated,
    )
    return describe_mod.apply(catalog, descriptions)


def cmd_build(args: argparse.Namespace) -> int:
    settings = get_settings()
    catalog = build_catalog(settings, enrich=args.enrich)
    store.save(catalog, store.catalog_path(settings))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print the catalogue exactly as a given role would see it in the prompt."""
    settings = get_settings()
    catalog = store.load(store.catalog_path(settings))
    principal = Principal(subject="cli", role=Role(args.role))
    sys.stdout.write(render_catalog(scope_catalog(catalog, principal)) + "\n")
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    catalog = subparsers.add_parser("catalog", help="Build or inspect the column catalogue")
    actions = catalog.add_subparsers(dest="catalog_command", required=True)

    build = actions.add_parser("build", help="Profile the dataset and write catalog.json")
    build.add_argument(
        "--enrich",
        action="store_true",
        help="Generate descriptions with the LLM (needs ANTHROPIC_API_KEY)",
    )
    build.set_defaults(func=cmd_build)

    show = actions.add_parser("show", help="Print the catalogue as a role sees it")
    show.add_argument("--role", choices=[r.value for r in Role], default=Role.ANALYST.value)
    show.set_defaults(func=cmd_show)
