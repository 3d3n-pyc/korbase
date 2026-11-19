"""Korvex integration: open the pool at startup and inject it into handlers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from korbase._db import Database, Transaction


class KorvexDatabase:
    """The database of a Korvex app, with dependencies for handlers.

    Use `Depends(kdb.database)` for the `Database` and
    `Depends(kdb.transaction)` for a `Transaction` per request.
    """

    def __init__(self, app: Any) -> None:
        """Bind to `app`; use `install()` instead."""
        self._app = app

    async def open(self, dsn: str, **db_opts: Any) -> None:
        """Open the pool and keep it on `app.local`."""
        self._app.local.korbase_db = await Database.connect(dsn, **db_opts)

    async def close(self) -> None:
        """Close the pool."""
        await self._app.local.korbase_db.close()

    async def database(self) -> Database:
        """Dependency: the app's `Database`."""
        return self._app.local.korbase_db

    async def transaction(self) -> AsyncIterator[Transaction]:
        """Dependency: a `Transaction` that commits unless the handler raises."""
        async with self._app.local.korbase_db.transaction() as tx:
            yield tx


def install(app: Any, dsn: str, **db_opts: Any) -> KorvexDatabase:
    """Open a `Database` on `dsn` when `app` starts and close it when it stops."""
    kdb = KorvexDatabase(app)

    @app.on_startup
    async def _open() -> None:
        """Open the pool."""
        await kdb.open(dsn, **db_opts)

    @app.on_shutdown
    async def _close() -> None:
        """Close the pool."""
        await kdb.close()

    return kdb
