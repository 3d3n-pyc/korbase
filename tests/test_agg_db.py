"""Aggregate and column queries on both backends."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

import msgspec
import pytest

from korbase import PK, Database, KorbaseError, cols, insert, select, table
from korbase._expr import count


@table("items")
class Item(msgspec.Struct):
    id: Annotated[int, PK()]
    grp: str
    qty: int
    day: dt.date
    ref: uuid.UUID


SETUP = """
DROP TABLE IF EXISTS items;
CREATE TABLE items (id int PRIMARY KEY, grp text NOT NULL, qty int NOT NULL, day date NOT NULL, ref uuid NOT NULL);
"""
U1, U2, U3 = (uuid.UUID(int=i) for i in (1, 2, 3))
ROWS = [
    Item(1, "a", 10, dt.date(2024, 1, 1), U2),
    Item(2, "a", 20, dt.date(2024, 3, 1), U1),
    Item(3, "b", 5, dt.date(2024, 2, 1), U3),
]
i = cols(Item)


def with_items(fn: Callable[[Database], Awaitable[None]], dsn: str, fill: bool = True) -> None:
    """Run `fn` against a fresh `items` table, filled unless `fill` is false."""

    async def main() -> None:
        async with await Database.connect(dsn) as db:
            async with db._pool.acquire() as conn:
                await conn.execute_script(SETUP)
            for r in ROWS if fill else ():
                await db.run(insert(Item).values(**msgspec.structs.asdict(r)))
            await fn(db)

    asyncio.run(main())


def test_count_where(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        assert await db.value(select(Item).columns(count()).where(grp="a")) == 2
        assert await db.run(select(Item).columns(count())) == [(3,)]

    with_items(body, backend_dsn)


def test_group_having(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        q = select(Item).columns(i.grp, count()).group_by(i.grp).having(count() > 1)
        assert await db.run(q) == [("a", 2)]

    with_items(body, backend_dsn)


def test_sum_min_max_int(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        row = await db.first(select(Item).columns(i.qty.sum(), i.qty.min(), i.qty.max()))
        assert row == (35, 5, 20)
        assert all(type(v) is int for v in row)

    with_items(body, backend_dsn)


def test_date_uuid_decode(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        row = await db.first(select(Item).columns(i.day.min(), i.day.max()))
        assert row == (dt.date(2024, 1, 1), dt.date(2024, 3, 1))
        assert await db.first(select(Item).columns(i.grp, i.ref).where(id=1)) == ("a", U2)
        assert await db.value(select(Item).columns(i.ref).where(id=3)) == U3

    with_items(body, backend_dsn)


def test_avg_is_float(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        v = await db.value(select(Item).columns(i.qty.avg()).where(grp="a"))
        assert v == 15.0
        assert type(v) is float

    with_items(body, backend_dsn)


def test_empty_table(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        assert await db.value(select(Item).columns(i.qty.max())) is None
        assert await db.value(select(Item).columns(i.qty.avg())) is None
        assert await db.value(select(Item).columns(count())) == 0
        assert await db.first(select(Item).columns(i.grp, count()).group_by(i.grp)) is None

    with_items(body, backend_dsn, fill=False)


def test_transaction_and_value_errors(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        async with db.transaction() as tx:
            assert await tx.value(select(Item).columns(count())) == 3
        with pytest.raises(KorbaseError, match="exactly one"):
            await db.value(select(Item).columns(count(), i.qty.max()))  # type: ignore[arg-type]

    with_items(body, backend_dsn)
