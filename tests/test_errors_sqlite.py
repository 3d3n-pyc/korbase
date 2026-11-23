"""SQLite errors are re-raised as Korbase errors."""

from __future__ import annotations

import asyncio
import functools
import sqlite3
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from korbase._errors import (
    CheckViolation,
    ConnectionFailed,
    DatabaseBusy,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
)
from korbase._sqlite import SqliteBackend

SCHEMA = """
CREATE TABLE parent (id INTEGER PRIMARY KEY, email TEXT UNIQUE);
CREATE TABLE child (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES parent(id),
    name TEXT NOT NULL,
    age INTEGER CHECK (age >= 0)
);
CREATE TABLE deferred (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED
);
"""


def _sync(fn: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an async test body with `asyncio.run`."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(fn(*args, **kwargs))

    return run


async def _pool(tmp_path: Path, **opts: Any) -> Any:
    """Open a pool on the test file."""
    return await SqliteBackend().connect(f"sqlite:///{tmp_path / 'e.db'}", **opts)


async def _with_schema(tmp_path: Path) -> Any:
    """Open a pool and create the test schema."""
    pool = await _pool(tmp_path)
    async with pool.acquire() as c:
        await c.execute_script(SCHEMA)
    return pool


@_sync
async def test_unique_violation(tmp_path: Path) -> None:
    """A repeated unique or primary key value raises UniqueViolation."""
    pool = await _with_schema(tmp_path)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO parent (id, email) VALUES (1, 'a')", [])
        with pytest.raises(UniqueViolation) as info:
            await c.execute("INSERT INTO parent (id, email) VALUES (2, 'a')", [])
        assert info.value.constraint == "parent.email"
        assert isinstance(info.value.__cause__, sqlite3.IntegrityError)
        with pytest.raises(UniqueViolation):
            await c.execute("INSERT INTO parent (id, email) VALUES (1, 'b')", [])
    await pool.close()


@_sync
async def test_foreign_key_violation(tmp_path: Path) -> None:
    """A missing parent raises ForeignKeyViolation."""
    pool = await _with_schema(tmp_path)
    async with pool.acquire() as c:
        with pytest.raises(ForeignKeyViolation) as info:
            await c.execute("INSERT INTO child (parent_id, name) VALUES (9, 'x')", [])
    assert info.value.constraint is None
    await pool.close()


@_sync
async def test_not_null_violation(tmp_path: Path) -> None:
    """A missing required value raises NotNullViolation."""
    pool = await _with_schema(tmp_path)
    async with pool.acquire() as c:
        with pytest.raises(NotNullViolation) as info:
            await c.execute("INSERT INTO child (name) VALUES (NULL)", [])
    assert info.value.constraint == "child.name"
    await pool.close()


@_sync
async def test_check_violation(tmp_path: Path) -> None:
    """A failing CHECK raises CheckViolation."""
    pool = await _with_schema(tmp_path)
    async with pool.acquire() as c:
        with pytest.raises(CheckViolation):
            await c.execute("INSERT INTO child (name, age) VALUES ('x', -1)", [])
    await pool.close()


@_sync
async def test_deferred_foreign_key_fails_at_commit(tmp_path: Path) -> None:
    """A deferred foreign key is reported when the transaction commits."""
    pool = await _with_schema(tmp_path)
    async with pool.acquire() as c:
        with pytest.raises(ForeignKeyViolation) as info:
            async with c.transaction():
                await c.execute("INSERT INTO deferred (parent_id) VALUES (9)", [])
    assert isinstance(info.value.__cause__, sqlite3.IntegrityError)
    await pool.close()


@_sync
async def test_busy(tmp_path: Path) -> None:
    """A second writer gets DatabaseBusy while the file is write-locked."""
    first = await _pool(tmp_path, busy_timeout_ms=0)
    second = await _pool(tmp_path, busy_timeout_ms=0)
    async with first.acquire() as a, second.acquire() as b, a.transaction():
        with pytest.raises(DatabaseBusy) as info:
            async with b.transaction():
                pass  # pragma: no cover
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
    await first.close()
    await second.close()


@_sync
async def test_bad_path(tmp_path: Path) -> None:
    """A path that cannot be opened raises ConnectionFailed."""
    with pytest.raises(ConnectionFailed) as info:
        await SqliteBackend().connect(f"sqlite:///{tmp_path / 'missing' / 'x.db'}")
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
