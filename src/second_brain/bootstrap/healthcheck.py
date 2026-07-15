"""Container healthcheck: a database ping with the application role.

The polling and voice-worker processes expose no HTTP server, so the compose
healthcheck cannot curl a `/health` endpoint. This console-script proves the
process is alive and the database is reachable with the application role by
running a single `SELECT 1` over ``DATABASE_URL`` and exiting 0 (healthy) or
1 (unhealthy). It never prints the connection URL or any other secret.
"""

import asyncio
import os
import sys

from sqlalchemy import text

from second_brain.slices.identity.adapters.persistence.database import (
    create_database_engine,
)


async def ping_database(database_url: str) -> bool:
    """Return True when ``SELECT 1`` succeeds over the given database URL."""
    engine = create_database_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    finally:
        await engine.dispose()
    return True


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("healthcheck failed: DATABASE_URL is not configured", file=sys.stderr)
        sys.exit(1)
    if asyncio.run(ping_database(database_url)):
        sys.exit(0)
    print("healthcheck failed: database is unreachable", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
