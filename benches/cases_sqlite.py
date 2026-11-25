"""Benchmark cases on SQLite: raw stdlib `sqlite3` against korbase.

The raw side calls `sqlite3` synchronously inside the coroutine. korbase runs
each statement on a worker thread, so that thread hop is part of its overhead.

Insert cases are symmetric: each operation inserts its rows and then deletes
them with one `DELETE ... WHERE tag = 'tmp'`, on both sides, so the table
never grows.
"""

import asyncio
import contextlib
import sqlite3
import statistics
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated

import msgspec

from korbase import PK, Database, cols, count, delete, insert, param, select, table

Fn = Callable[[], Awaitable[object]]
Cases = dict[str, tuple[Fn, Fn]]

SEED_ROWS = 1000


@table("bench_items")
class Item(msgspec.Struct):
    """One benchmark row."""

    id: Annotated[int, PK()]
    grp: int
    n: int
    tag: str


C = cols(Item)
SELECT_PK = select(Item).where(id=param("id"))
SELECT_100 = select(Item).where(C.id <= 100).order_by(C.id)
AGGREGATE = select(Item).columns(C.grp, count(), C.n.sum()).group_by(C.grp).order_by(C.grp)
DELETE_TMP = delete(Item).where(tag="tmp")

DDL = "CREATE TABLE bench_items (id INTEGER PRIMARY KEY, grp INTEGER NOT NULL, n INTEGER NOT NULL, tag TEXT NOT NULL)"


def _open_raw(dsn: str) -> sqlite3.Connection:
    """Open a raw autocommit connection with the PRAGMAs korbase sets."""
    path = dsn.removeprefix("sqlite:///")
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _seed(con: sqlite3.Connection) -> None:
    """Create the table and fill it with `SEED_ROWS` rows."""
    con.execute(DDL)
    rows = [(i, i % 10, i, "seed") for i in range(1, SEED_ROWS + 1)]
    con.executemany("INSERT INTO bench_items VALUES (?, ?, ?, ?)", rows)


def _raw_cases(con: sqlite3.Connection) -> dict[str, Fn]:
    """Build the raw sqlite3 side of each case."""

    async def select_pk() -> object:
        return con.execute("SELECT id, grp, n, tag FROM bench_items WHERE id = ?", (500,)).fetchall()

    async def select_100() -> object:
        return con.execute("SELECT id, grp, n, tag FROM bench_items WHERE id <= 100 ORDER BY id").fetchall()

    async def insert_one() -> object:
        con.execute("INSERT INTO bench_items (grp, n, tag) VALUES (?, ?, ?)", (1, 1, "tmp"))
        return con.execute("DELETE FROM bench_items WHERE tag = 'tmp'").rowcount

    async def bulk_insert_1000() -> object:
        rows = [(i % 10, i, "tmp") for i in range(1000)]
        con.executemany("INSERT INTO bench_items (grp, n, tag) VALUES (?, ?, ?)", rows)
        return con.execute("DELETE FROM bench_items WHERE tag = 'tmp'").rowcount

    async def aggregate() -> object:
        return con.execute("SELECT grp, count(*), sum(n) FROM bench_items GROUP BY grp ORDER BY grp").fetchall()

    return {
        "select_pk": select_pk,
        "select_100": select_100,
        "insert_one": insert_one,
        "bulk_insert_1000": bulk_insert_1000,
        "aggregate": aggregate,
    }


def _korbase_cases(db: Database) -> dict[str, Fn]:
    """Build the korbase side of each case."""

    async def select_pk() -> object:
        return await db.run(SELECT_PK, id=500)

    async def select_100() -> object:
        return await db.run(SELECT_100)

    async def insert_one() -> object:
        await db.run(insert(Item).values(grp=1, n=1, tag="tmp"))
        return await db.run(DELETE_TMP)

    async def bulk_insert_1000() -> object:
        await db.run(insert(Item).rows([{"grp": i % 10, "n": i, "tag": "tmp"} for i in range(1000)]))
        return await db.run(DELETE_TMP)

    async def aggregate() -> object:
        return await db.run(AGGREGATE)

    return {
        "select_pk": select_pk,
        "select_100": select_100,
        "insert_one": insert_one,
        "bulk_insert_1000": bulk_insert_1000,
        "aggregate": aggregate,
    }


@contextlib.asynccontextmanager
async def cases(dsn: str) -> AsyncIterator[dict[str, tuple[Fn, Fn]]]:
    """Set up the schema and data, yield `{name: (raw_fn, korbase_fn)}`, then drop everything."""
    con = _open_raw(dsn)
    _seed(con)
    try:
        async with await Database.connect(dsn) as db:
            raw, kor = _raw_cases(con), _korbase_cases(db)
            yield {name: (raw[name], kor[name]) for name in raw}
    finally:
        con.execute("DROP TABLE bench_items")
        con.close()


async def _median_us(fn: Fn, runs: int) -> float:
    """Run `fn` `runs` times and return the median time in microseconds."""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        await fn()
        times.append((time.perf_counter() - start) * 1e6)
    return statistics.median(times)


async def main() -> None:
    """Print the median time of each case, raw then korbase."""
    with tempfile.TemporaryDirectory() as tmp:
        async with cases(f"sqlite:///{Path(tmp) / 'bench.db'}") as all_cases:
            for name, (raw, kor) in all_cases.items():
                print(
                    f"{name:18} raw {await _median_us(raw, 200):9.1f} us  korbase {await _median_us(kor, 200):9.1f} us"
                )


if __name__ == "__main__":
    asyncio.run(main())
