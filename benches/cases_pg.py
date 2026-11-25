"""Postgres bench cases: raw asyncpg against korbase, one operation each."""

from __future__ import annotations

import asyncio
import contextlib
import os
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

import asyncpg
import msgspec

from korbase import PK, Database, cols, count, insert, param, select, table

MIN_SIZE = 2
MAX_SIZE = 4
SEED_ROWS = 1000
BULK = 1000

Fn = Callable[[], Awaitable[object]]


@table("bench_items")
class Item(msgspec.Struct):
    """A row of the bench table."""

    id: Annotated[int, PK()]
    grp: int
    name: str
    n: int


C = cols(Item)
SELECT_PK = select(Item).where(id=param("id"))
SELECT_100 = select(Item).where(grp=param("grp")).order_by(C.id.asc()).limit(100)
INSERT_ONE = insert(Item).values(grp=param("grp"), name=param("name"), n=param("n"))
AGGREGATE = select(Item).columns(C.grp, count(), C.n.sum()).group_by(C.grp)

_RAW_PK = "SELECT id, grp, name, n FROM bench_items WHERE id = $1"
_RAW_100 = "SELECT id, grp, name, n FROM bench_items WHERE grp = $1 ORDER BY id LIMIT 100"
_RAW_ONE = "INSERT INTO bench_items (grp, name, n) VALUES ($1, $2, $3) RETURNING id, grp, name, n"
_RAW_AGG = "SELECT grp, count(*), sum(n) FROM bench_items GROUP BY grp"
_DDL = "CREATE TABLE bench_items (id serial PRIMARY KEY, grp int NOT NULL, name text NOT NULL, n int NOT NULL)"
_SEED = "INSERT INTO bench_items (grp, name, n) SELECT i % 10, 'item' || i, i FROM generate_series(1, $1) i"


class _Rollback(Exception):
    """Raised inside a transaction to undo the insert."""


def _bulk_rows() -> list[dict[str, object]]:
    """Build the rows both sides insert in the bulk case."""
    return [{"grp": i % 10, "name": f"bulk{i}", "n": i} for i in range(BULK)]


def _raw_bulk_sql() -> str:
    """Return one multi-row INSERT ... RETURNING, the equivalent of `rows()`."""
    marks = ",".join(f"(${3 * i + 1},${3 * i + 2},${3 * i + 3})" for i in range(BULK))
    return f"INSERT INTO bench_items (grp, name, n) VALUES {marks} RETURNING id, grp, name, n"


def _raw_cases(pool: asyncpg.Pool) -> dict[str, Fn]:
    """Build the raw asyncpg side."""
    bulk_sql = _raw_bulk_sql()
    bulk_args = [v for r in _bulk_rows() for v in r.values()]

    async def select_pk() -> object:
        """Fetch one row by primary key."""
        return await pool.fetchrow(_RAW_PK, 500)

    async def select_100() -> object:
        """Fetch 100 rows of one group."""
        return await pool.fetch(_RAW_100, 3)

    async def insert_one() -> object:
        """Insert one row, rolled back so the table stays the same size."""
        with contextlib.suppress(_Rollback):
            async with pool.acquire() as conn, conn.transaction():
                await conn.fetchrow(_RAW_ONE, 1, "x", 1)
                raise _Rollback

    async def bulk() -> object:
        """Insert 1000 rows in one statement, then roll back."""
        with contextlib.suppress(_Rollback):
            async with pool.acquire() as conn, conn.transaction():
                await conn.fetch(bulk_sql, *bulk_args)
                raise _Rollback

    async def aggregate() -> object:
        """Count and sum per group."""
        return await pool.fetch(_RAW_AGG)

    return {
        "select_pk": select_pk,
        "select_100": select_100,
        "insert_one": insert_one,
        "bulk_insert_1000": bulk,
        "aggregate": aggregate,
    }


def _korbase_cases(db: Database) -> dict[str, Fn]:
    """Build the korbase side, same work as the raw side."""
    rows = _bulk_rows()

    async def select_pk() -> object:
        """Fetch one row by primary key."""
        return await db.first(SELECT_PK, id=500)

    async def select_100() -> object:
        """Fetch 100 rows of one group."""
        return await db.run(SELECT_100, grp=3)

    async def insert_one() -> object:
        """Insert one row, rolled back so the table stays the same size."""
        with contextlib.suppress(_Rollback):
            async with db.transaction() as tx:
                await tx.first(INSERT_ONE, grp=1, name="x", n=1)
                raise _Rollback

    async def bulk() -> object:
        """Insert 1000 rows with `rows()`, then roll back."""
        with contextlib.suppress(_Rollback):
            async with db.transaction() as tx:
                await tx.run(insert(Item).rows(rows))
                raise _Rollback

    async def aggregate() -> object:
        """Count and sum per group."""
        return await db.run(AGGREGATE)

    return {
        "select_pk": select_pk,
        "select_100": select_100,
        "insert_one": insert_one,
        "bulk_insert_1000": bulk,
        "aggregate": aggregate,
    }


@contextlib.asynccontextmanager
async def cases(dsn: str) -> AsyncIterator[dict[str, tuple[Fn, Fn]]]:
    """Create and fill the table, yield `{name: (raw_fn, korbase_fn)}`, drop the table."""
    pool = await asyncpg.create_pool(dsn, min_size=MIN_SIZE, max_size=MAX_SIZE)
    db = await Database.connect(dsn, min_size=MIN_SIZE, max_size=MAX_SIZE)
    try:
        await pool.execute("DROP TABLE IF EXISTS bench_items")
        await pool.execute(_DDL)
        await pool.execute(_SEED, SEED_ROWS)
        raw, kor = _raw_cases(pool), _korbase_cases(db)
        yield {name: (raw[name], kor[name]) for name in raw}
    finally:
        await pool.execute("DROP TABLE IF EXISTS bench_items")
        await db.close()
        await pool.close()


async def _median_us(fn: Fn, runs: int) -> float:
    """Return the median time of `runs` calls of `fn`, in microseconds."""
    await fn()
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        await fn()
        times.append((time.perf_counter() - start) * 1e6)
    return statistics.median(times)


async def _main() -> None:
    """Run every case 200 times and print the medians."""
    async with cases(os.environ["KORBASE_TEST_DSN"]) as found:
        for name, (raw, kor) in found.items():
            r, k = await _median_us(raw, 200), await _median_us(kor, 200)
            print(f"{name:18} raw {r:9.1f} us  korbase {k:9.1f} us")


if __name__ == "__main__":
    asyncio.run(_main())
