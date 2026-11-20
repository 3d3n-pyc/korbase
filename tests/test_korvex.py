"""Tests for the Korvex integration."""

import asyncio
from typing import Annotated

import pytest

pytest.importorskip("korvex")

import asyncpg  # noqa: E402
from korvex import App, Depends, HTTPException, Response  # noqa: E402
from korvex.testing import TestClient  # noqa: E402

from korbase import Database, Transaction  # noqa: E402
from korbase.korvex import install  # noqa: E402


def _connections(dsn: str) -> int:
    """Count the connections to the current database."""

    async def count() -> int:
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchval(
                "select count(*) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid()"
            )
        finally:
            await conn.close()

    return asyncio.run(count())


def _build(dsn: str) -> App:
    """Build an app with routes using both dependencies."""
    app = App()
    kdb = install(app, dsn, min_size=1, max_size=2)

    @app.post("/ok", status=201)
    async def ok(tx: Annotated[Transaction, Depends(kdb.transaction)]) -> None:
        await tx.execute("insert into kv_items values ('ok')")

    @app.post("/bad")
    async def bad(tx: Annotated[Transaction, Depends(kdb.transaction)]) -> None:
        await tx.execute("insert into kv_items values ('bad')")
        raise HTTPException(400)

    @app.get("/items")
    async def items(db: Annotated[Database, Depends(kdb.database)]) -> Response:
        rows = await db.fetch("select name from kv_items order by name")
        return Response.json([r[0] for r in rows])

    return app


def test_transaction_commit_rollback_and_pool(dsn: str) -> None:
    """Commit on success, roll back on error, and close the pool."""

    async def setup() -> None:
        conn = await asyncpg.connect(dsn)
        await conn.execute("drop table if exists kv_items; create table kv_items (name text)")
        await conn.close()

    asyncio.run(setup())
    baseline = _connections(dsn)
    with TestClient(_build(dsn)) as client:
        assert client.post("/ok").status == 201
        assert client.post("/bad").status == 400
        assert client.get("/items").json() == ["ok"]
        assert _connections(dsn) > baseline
    assert _connections(dsn) == baseline
