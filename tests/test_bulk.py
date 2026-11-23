"""Bulk insert and upsert with `.rows(...)`."""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional

import msgspec
import pytest

from korbase import PK, Database, KorbaseError, insert, table, upsert
from korbase._dialect import PG, SQLITE


@table("bulk_items")
class Item(msgspec.Struct):
    id: Annotated[int, PK()]
    title: str
    n: int
    note: Optional[str] = None


SETUP = """
DROP TABLE IF EXISTS bulk_items;
CREATE TABLE bulk_items (id integer PRIMARY KEY, title text NOT NULL, n integer NOT NULL DEFAULT 7, note text);
"""


def run(dsn: str, body):  # type: ignore[no-untyped-def]
    """Run `body(db)` against a fresh table."""

    async def main() -> None:
        async with await Database.connect(dsn) as db:
            async with db._pool.acquire() as conn:
                await conn.execute_script(SETUP)
            await body(db)

    asyncio.run(main())


def test_compile_both_dialects() -> None:
    q = insert(Item).rows([{"id": 1, "title": "a"}, {"id": 2, "title": "b"}])
    assert q.compile(PG).sql.startswith('INSERT INTO "bulk_items" ("id", "title") VALUES ($1, $2), ($3, $4) RETURNING')
    assert q.compile(SQLITE).sql.startswith('INSERT INTO "bulk_items" ("id", "title") VALUES (?1, ?2), (?3, ?4)')


def test_compile_upsert() -> None:
    q = upsert(Item).rows([{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]).on_conflict("id")
    assert 'ON CONFLICT ("id") DO UPDATE SET "title" = EXCLUDED."title"' in q.compile(PG).sql


def test_rows_and_values_exclusive() -> None:
    q = insert(Item).values(id=1, title="x").rows([{"id": 2, "title": "y"}])
    assert q.compile(PG).sql.count("$") == 2 and q._values is None
    q = q.values(id=3, title="z")
    assert q._rows is None
    q = q.rows([{"id": 4, "title": "a"}]).rows([{"id": 5, "title": "b"}, {"id": 6, "title": "c"}])
    assert q.compile(PG).sql.count("$") == 4


def test_refusals() -> None:
    with pytest.raises(KorbaseError):
        insert(Item).rows([])
    with pytest.raises(KorbaseError, match="same columns"):
        insert(Item).rows([{"id": 1, "title": "a"}, {"id": 2}])
    with pytest.raises(KorbaseError, match="no column"):
        insert(Item).rows([{"id": 1, "nope": "a"}])
    with pytest.raises(KorbaseError, match="smaller batches"):
        insert(Item).rows([{"id": i, "title": "a"} for i in range(16384)])
    q = upsert(Item).rows([{"id": 1, "title": "a"}, {"id": 1, "title": "b"}]).on_conflict("id")
    for d in (PG, SQLITE):
        with pytest.raises(KorbaseError, match="same id"):
            q.compile(d)


def test_insert_returns_in_order(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        rows = [{"id": i, "title": f"t{i}"} for i in (3, 1, 2)]
        got = await db.run(insert(Item).rows(rows))
        assert [r.id for r in got] == [3, 1, 2]
        assert [r.n for r in got] == [7, 7, 7]
        assert [r.note for r in got] == [None] * 3

    run(backend_dsn, body)


def test_upsert_updates_and_inserts(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.run(insert(Item).values(id=1, title="old"))
        q = upsert(Item).rows([{"id": 1, "title": "new"}, {"id": 2, "title": "two"}]).on_conflict("id")
        got = await db.run(q)
        assert sorted((r.id, r.title) for r in got) == [(1, "new"), (2, "two")]

    run(backend_dsn, body)


def test_thousand_rows(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        got = await db.run(insert(Item).rows([{"id": i, "title": "x", "n": i} for i in range(1000)]))
        assert [r.id for r in got] == list(range(1000))

    run(backend_dsn, body)
