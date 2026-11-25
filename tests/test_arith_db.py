"""Arithmetic in updates, filters and columns on both backends."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Any

import msgspec
import pytest

from korbase import PK, Database, DatabaseError, KorbaseError, cols, insert, select, table, update
from korbase._expr import param


@table("accts")
class Acct(msgspec.Struct):
    id: Annotated[int, PK()]
    balance: int
    qty: int
    price: int
    ratio: float


@table("amounts")
class Amount(msgspec.Struct):
    id: Annotated[int, PK()]
    amount: Decimal


SETUP = """
DROP TABLE IF EXISTS accts;
CREATE TABLE accts (id int PRIMARY KEY, balance int NOT NULL, qty int NOT NULL, price int NOT NULL, ratio double precision NOT NULL);
"""
DEC_SETUP = """
DROP TABLE IF EXISTS amounts;
CREATE TABLE amounts (id int PRIMARY KEY, amount numeric(10, 2) NOT NULL);
"""
c: Any = cols(Acct)


def run_db(fn: Callable[[Database], Awaitable[None]], dsn: str, script: str = SETUP) -> None:
    """Run `fn` against a fresh table; `script` creates it, `accts` is filled."""

    async def main() -> None:
        async with await Database.connect(dsn) as db:
            async with db._pool.acquire() as conn:
                await conn.execute_script(script)
            if script == SETUP:
                await db.run(insert(Acct).values(id=1, balance=100, qty=7, price=20, ratio=2.0))
                await db.run(insert(Acct).values(id=2, balance=50, qty=3, price=10, ratio=4.0))
            await fn(db)

    asyncio.run(main())


def test_transfer_in_transaction(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        async with db.transaction() as tx:
            assert await tx.run(update(Acct).set(balance=c.balance - 10).where(id=1)) == 1
            assert await tx.run(update(Acct).set(balance=c.balance + 10).where(id=2)) == 1
        rows = await db.run(select(Acct).order_by(c.id))
        assert [r.balance for r in rows] == [90, 60]

    run_db(body, backend_dsn)


def test_update_returning(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        rows = await db.run(update(Acct).set(balance=c.balance - 10).where(id=1).returning())
        assert [(r.id, r.balance) for r in rows] == [(1, 90)]

    run_db(body, backend_dsn)


def test_set_with_param(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.run(update(Acct).set(balance=c.balance + param("n")).where(id=1), n=5)
        assert await db.value(select(Acct).columns(c.balance).where(id=1)) == 105

    run_db(body, backend_dsn)


def test_where_arith(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        rows = await db.run(select(Acct).where(c.qty * c.price > 100))
        assert [r.id for r in rows] == [1]
        rows = await db.run(select(Acct).where(100 - c.qty > 95))
        assert [r.id for r in rows] == [2]

    run_db(body, backend_dsn)


def test_columns_types(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        rows = await db.run(select(Acct).columns(c.id, c.qty * 2).order_by(c.id))
        assert [tuple(r) for r in rows] == [(1, 14), (2, 6)]
        assert all(type(r[1]) is int for r in rows)
        rows = await db.run(select(Acct).columns(c.ratio * 1.5).order_by(c.id))
        assert [r[0] for r in rows] == [3.0, 6.0]
        assert all(type(r[0]) is float for r in rows)

    run_db(body, backend_dsn)


def test_integer_division_and_modulo(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        assert await db.value(select(Acct).columns(c.qty / 2).where(id=1)) == 3
        assert await db.value(select(Acct).columns(c.qty % 2).where(id=1)) == 1

    run_db(body, backend_dsn)


def test_division_by_zero(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        query = select(Acct).columns(c.qty / (c.qty - c.qty)).where(id=1)
        if backend_dsn.startswith("postgres"):
            with pytest.raises(DatabaseError):
                await db.run(query)
        else:
            assert await db.value(query) is None

    run_db(body, backend_dsn)


def test_float_added_to_int(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        value = await db.value(select(Acct).columns(c.qty + 0.5).where(id=1))
        assert value == 7.5
        assert type(value) is float

    run_db(body, backend_dsn)


def test_decimal_arithmetic(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        d: Any = cols(Amount)
        if backend_dsn.startswith("postgres"):
            await db.run(insert(Amount).values(id=1, amount=Decimal("1.10")))
            value = await db.value(select(Amount).columns(d.amount * 2))
            assert value == Decimal("2.20")
            assert str(value) == "2.20"
        else:
            with pytest.raises(KorbaseError):
                await db.run(insert(Amount).values(id=1, amount=Decimal("1.10")))
                await db.value(select(Amount).columns(d.amount * 2))

    run_db(body, backend_dsn, DEC_SETUP)
