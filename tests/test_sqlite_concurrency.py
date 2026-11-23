"""Concurrency tests for the SQLite backend: WAL, busy timeout, savepoints, pool."""

from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from korbase import Database
from korbase._errors import DatabaseBusy

_ROWS = 25


def _dsn(path: Path) -> str:
    """Return the DSN of a file database."""
    return f"sqlite:///{path}"


async def _setup(path: Path) -> None:
    """Create the `t` table."""
    async with await Database.connect(_dsn(path)) as db:
        await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who TEXT)")


async def _insert_rows(dsn: str, who: str, barrier: object) -> None:
    """Insert `_ROWS` rows, one transaction each."""
    async with await Database.connect(dsn) as db:
        barrier.wait(10)  # type: ignore[attr-defined]
        for _ in range(_ROWS):
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO t (who) VALUES (?)", who)


def _worker(dsn: str, who: str, barrier: object) -> None:
    """Process entry point: run the inserts."""
    asyncio.run(_insert_rows(dsn, who, barrier))


def test_two_processes_write(tmp_path: Path) -> None:
    """Two processes writing at once lose no row and never see a lock error."""
    path = tmp_path / "a.db"
    asyncio.run(_setup(path))
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    procs = [ctx.Process(target=_worker, args=(_dsn(path), w, barrier)) for w in ("a", "b")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    assert [p.exitcode for p in procs] == [0, 0]
    con = sqlite3.connect(path)
    assert con.execute("SELECT count(*) FROM t").fetchone() == (2 * _ROWS,)
    con.close()


def test_reader_not_blocked_sees_committed_only(tmp_path: Path) -> None:
    asyncio.run(_reader_not_blocked(tmp_path))


async def _reader_not_blocked(tmp_path: Path) -> None:
    """A reader runs during a write transaction and sees only committed rows."""
    path = tmp_path / "a.db"
    await _setup(path)
    async with await Database.connect(_dsn(path)) as w, await Database.connect(_dsn(path), busy_timeout_ms=50) as r:
        await w.execute("INSERT INTO t (who) VALUES ('old')")
        async with w.transaction() as tx:
            await tx.execute("INSERT INTO t (who) VALUES ('new')")
            assert await r.fetch("SELECT who FROM t") == [("old",)]
        assert await r.fetch("SELECT who FROM t ORDER BY id") == [("old",), ("new",)]


def test_savepoint_rollback_keeps_outer(tmp_path: Path) -> None:
    asyncio.run(_savepoint(tmp_path))


async def _savepoint(tmp_path: Path) -> None:
    """Rolling back an inner transaction keeps the outer work."""
    path = tmp_path / "a.db"
    await _setup(path)
    async with await Database.connect(_dsn(path)) as db:
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t (who) VALUES ('outer')")
            with pytest.raises(ZeroDivisionError):
                async with tx.transaction() as inner:
                    await inner.execute("INSERT INTO t (who) VALUES ('inner')")
                    raise ZeroDivisionError
            await tx.execute("INSERT INTO t (who) VALUES ('outer2')")
        assert await db.fetch("SELECT who FROM t ORDER BY id") == [("outer",), ("outer2",)]


def test_busy_timeout_fails_clearly(tmp_path: Path) -> None:
    asyncio.run(_busy_timeout(tmp_path))


async def _busy_timeout(tmp_path: Path) -> None:
    """A second writer fails with a clear error once the busy timeout passes."""
    path = tmp_path / "a.db"
    await _setup(path)
    async with await Database.connect(_dsn(path)) as a, await Database.connect(_dsn(path), busy_timeout_ms=50) as b:
        async with a.transaction() as tx:
            await tx.execute("INSERT INTO t (who) VALUES ('a')")
            with pytest.raises(DatabaseBusy):
                async with b.transaction():
                    pass
            with pytest.raises(DatabaseBusy):
                await b.execute("INSERT INTO t (who) VALUES ('b')")
        await b.execute("INSERT INTO t (who) VALUES ('b')")
        assert await a.fetch("SELECT who FROM t ORDER BY id") == [("a",), ("b",)]


def test_pool_concurrent_reads(tmp_path: Path) -> None:
    asyncio.run(_pool_reads(tmp_path))


async def _pool_reads(tmp_path: Path) -> None:
    """Many tasks read at once through a pool of two connections."""
    path = tmp_path / "a.db"
    await _setup(path)
    async with await Database.connect(_dsn(path), size=2) as db:
        for i in range(10):
            await db.execute("INSERT INTO t (who) VALUES (?)", str(i))
        results = await asyncio.gather(*(db.fetch("SELECT count(*) FROM t") for _ in range(20)))
        assert results == [[(10,)]] * 20
