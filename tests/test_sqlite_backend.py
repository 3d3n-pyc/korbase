"""Tests for the SQLite backend."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from korbase._sqlite import SqliteBackend


def _row(r):  # type: ignore[no-untyped-def]
    return tuple(r)


async def _file_pool(tmp_path: Path, size: int = 1):  # type: ignore[no-untyped-def]
    return await SqliteBackend().connect(f"sqlite:///{tmp_path / 'a.db'}", size=size)


def test_memory_connect_and_wal(tmp_path: Path) -> None:
    asyncio.run(_test_memory_connect_and_wal(tmp_path))


async def _test_memory_connect_and_wal(tmp_path: Path) -> None:
    mem = await SqliteBackend().connect("sqlite::memory:", size=3)
    async with mem.acquire() as c:
        assert await c.fetch("SELECT 1", [], _row) == [(1,)]
    await mem.close()
    pool = await _file_pool(tmp_path)
    async with pool.acquire() as c:
        assert await c.fetch("PRAGMA journal_mode", [], _row) == [("wal",)]
    await pool.close()


def test_foreign_keys_and_tags(tmp_path: Path) -> None:
    asyncio.run(_test_foreign_keys_and_tags(tmp_path))


async def _test_foreign_keys_and_tags(tmp_path: Path) -> None:
    pool = await _file_pool(tmp_path)
    async with pool.acquire() as c:
        await c.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)", [])
        await c.execute("CREATE TABLE ch (pid INTEGER REFERENCES p(id))", [])
        assert await c.execute("INSERT INTO p (id) VALUES (?1)", [1]) == "INSERT 0 1"
        assert await c.execute("UPDATE p SET id = ?1 WHERE id = ?2", [1, 1]) == "UPDATE 1"
        with pytest.raises(sqlite3.IntegrityError):
            await c.execute("INSERT INTO ch (pid) VALUES (?1)", [99])
        assert await c.fetchrow("SELECT id FROM p WHERE id = ?1", [1], _row) == (1,)
        assert await c.fetchrow("SELECT id FROM p WHERE id = ?1", [5], _row) is None
        assert await c.execute("DELETE FROM p", []) == "DELETE 1"
    await pool.close()


def test_fetch_columns_empty() -> None:
    asyncio.run(_test_fetch_columns_empty())


async def _test_fetch_columns_empty() -> None:
    pool = await SqliteBackend().connect("sqlite::memory:")
    async with pool.acquire() as c:
        await c.execute("CREATE TABLE t (a, b)", [])
        names, rows = await c.fetch_columns("SELECT a, b FROM t", [])
    assert list(names) == ["a", "b"]
    assert rows == []
    await pool.close()


def test_execute_script_literal_and_trigger() -> None:
    asyncio.run(_test_execute_script_literal_and_trigger())


async def _test_execute_script_literal_and_trigger() -> None:
    pool = await SqliteBackend().connect("sqlite::memory:")
    script = """
    CREATE TABLE t (v TEXT);
    CREATE TABLE log (n INTEGER);
    CREATE TRIGGER trg AFTER INSERT ON t BEGIN
        INSERT INTO log VALUES (1);
        INSERT INTO log VALUES (2);
    END;
    INSERT INTO t VALUES ('a;b');
    """
    async with pool.acquire() as c:
        await c.execute_script(script)
        assert await c.fetch("SELECT v FROM t", [], _row) == [("a;b",)]
        assert await c.fetch("SELECT n FROM log", [], _row) == [(1,), (2,)]
    await pool.close()


def test_transactions() -> None:
    asyncio.run(_test_transactions())


async def _test_transactions() -> None:
    pool = await SqliteBackend().connect("sqlite::memory:")
    async with pool.acquire() as c:
        await c.execute("CREATE TABLE t (v INTEGER)", [])
        async with c.transaction():
            await c.execute("INSERT INTO t VALUES (1)", [])
        with pytest.raises(ValueError):
            async with c.transaction():
                await c.execute("INSERT INTO t VALUES (2)", [])
                raise ValueError
        async with c.transaction():
            await c.execute("INSERT INTO t VALUES (3)", [])
            with pytest.raises(ValueError):
                async with c.transaction():
                    await c.execute("INSERT INTO t VALUES (4)", [])
                    raise ValueError
        assert await c.fetch("SELECT v FROM t ORDER BY v", [], _row) == [(1,), (3,)]
    await pool.close()


def test_pool_two_readers(tmp_path: Path) -> None:
    asyncio.run(_test_pool_two_readers(tmp_path))


async def _test_pool_two_readers(tmp_path: Path) -> None:
    pool = await _file_pool(tmp_path, size=2)
    async with pool.acquire() as a, pool.acquire() as b:
        assert a is not b
        assert await a.fetch("SELECT 1", [], _row) == [(1,)]
        assert await b.fetch("SELECT 2", [], _row) == [(2,)]
    await pool.close()


def test_old_version_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_test_old_version_refused(monkeypatch))


async def _test_old_version_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 34, 0))
    with pytest.raises(RuntimeError, match="3.35"):
        await SqliteBackend().connect("sqlite::memory:")


def test_close_stops_threads(tmp_path: Path) -> None:
    asyncio.run(_test_close_stops_threads(tmp_path))


async def _test_close_stops_threads(tmp_path: Path) -> None:
    pool = await _file_pool(tmp_path, size=2)
    async with pool.acquire() as c:
        await c.fetch("SELECT 1", [], _row)
    await pool.close()
    assert not [t for t in threading.enumerate() if t.name.startswith("korbase-sqlite")]
