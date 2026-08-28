"""Empty the end-to-end database, leaving the schema intact.

Run before the Playwright suite. Account behaviour depends on how many accounts
exist -- the first one created becomes an analyst -- so a suite that inherited
rows from a previous run would pass or fail depending on what ran before it.

**Truncates rather than drops**, deliberately, and that ordering matters:
Playwright starts its web servers *before* `globalSetup`, so the API has
already migrated by the time this runs. Dropping the tables here pulled the
schema out from under a running application, and every request then failed with
`relation "users" does not exist` — which reads like a migration bug rather than
a test-harness one. Truncating is order-independent: it works whether the
schema was created a moment ago or a week ago.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from ledger.config import Settings, get_settings
from ledger.db.base import Base
from ledger.db.session import create_engine
from ledger.logging import configure_logging, get_logger

log = get_logger(__name__)


async def reset(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            # Create anything missing, so a first run on an empty database
            # works without waiting for the application to migrate.
            await connection.run_sync(Base.metadata.create_all)

            tables = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
            # RESTART IDENTITY and CASCADE together: the tables reference each
            # other, and a partial truncate would leave orphans.
            await connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()
    log.info("emptied %s", settings.database_url.rsplit("/", 1)[-1])


def main() -> int:
    configure_logging()
    asyncio.run(reset(get_settings()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
