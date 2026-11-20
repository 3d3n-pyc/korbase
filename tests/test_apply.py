"""Tests for applying SQL migration files."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from korbase import Database
from korbase._ident import KorbaseError
from korbase._runner import apply, status

TABLES = ("korbase_migrations", "m_a", "m_b", "m_c", "m_scs")


def _write(directory: Path, name: str, text: str) -> None:
    """Write a migration file."""
    (directory / name).write_text(text)


async def _clean(db: Database) -> None:
    """Drop the tables the tests use."""
    for table in TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table}")


def _run(dsn: str, body: Callable[[Database], Awaitable[Any]]) -> Any:
    """Run `body` on a cleaned database."""

    async def main() -> Any:
        """Connect, clean, run, clean."""
        async with await Database.connect(dsn) as db:
            await _clean(db)
            try:
                return await body(db)
            finally:
                await _clean(db)

    return asyncio.run(main())


def test_apply_in_order_and_idempotent(dsn: str, tmp_path: Path) -> None:
    """Files apply in order, once."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE m_a (id int);")
    _write(tmp_path, "0002_b.sql", "CREATE TABLE m_b (id int);")
    _write(tmp_path, "notes.txt", "ignored")

    async def body(db: Database) -> None:
        """Apply twice."""
        assert await apply(db, tmp_path) == ["0001_a.sql", "0002_b.sql"]
        assert await apply(db, tmp_path) == []
        assert await status(db, tmp_path) == [("0001_a.sql", "applied"), ("0002_b.sql", "applied")]

    _run(dsn, body)


def test_bad_name(dsn: str, tmp_path: Path) -> None:
    """A malformed .sql name is an error."""
    _write(tmp_path, "1_x.sql", "SELECT 1;")

    async def body(db: Database) -> None:
        """Apply."""
        with pytest.raises(KorbaseError):
            await apply(db, tmp_path)

    _run(dsn, body)


def test_tampered_and_missing(dsn: str, tmp_path: Path) -> None:
    """A changed or missing applied file is refused."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE m_a (id int);")

    async def body(db: Database) -> None:
        """Apply, tamper, delete."""
        await apply(db, tmp_path)
        _write(tmp_path, "0002_b.sql", "CREATE TABLE m_b (id int);")
        _write(tmp_path, "0001_a.sql", "CREATE TABLE m_a (id int, x int);")
        with pytest.raises(KorbaseError, match="changed"):
            await apply(db, tmp_path)
        assert dict(await status(db, tmp_path)) == {"0001_a.sql": "changed", "0002_b.sql": "pending"}
        (tmp_path / "0001_a.sql").unlink()
        with pytest.raises(KorbaseError, match="missing"):
            await apply(db, tmp_path)
        assert dict(await status(db, tmp_path))["0001_a.sql"] == "missing"

    _run(dsn, body)


def test_out_of_order(dsn: str, tmp_path: Path) -> None:
    """A pending file before the last applied one is refused."""
    _write(tmp_path, "0002_b.sql", "CREATE TABLE m_b (id int);")

    async def body(db: Database) -> None:
        """Apply 2, then add 1."""
        await apply(db, tmp_path)
        _write(tmp_path, "0001_a.sql", "CREATE TABLE m_a (id int);")
        with pytest.raises(KorbaseError, match="before"):
            await apply(db, tmp_path)

    _run(dsn, body)


def test_review_marker(dsn: str, tmp_path: Path) -> None:
    """A review marker blocks everything."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE m_a (id int);")
    _write(tmp_path, "0002_b.sql", "-- korbase:review\nCREATE TABLE m_b (id int);")

    async def body(db: Database) -> None:
        """Apply."""
        with pytest.raises(KorbaseError, match="review"):
            await apply(db, tmp_path)
        assert await status(db, tmp_path) == [("0001_a.sql", "pending"), ("0002_b.sql", "pending")]

    _run(dsn, body)


def test_failure_rolls_back_only_itself(dsn: str, tmp_path: Path) -> None:
    """The failing file leaves no trace; earlier files stay."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE m_a (id int);")
    _write(tmp_path, "0002_b.sql", "CREATE TABLE m_b (id int); SELECT 1/0;")
    _write(tmp_path, "0003_c.sql", "CREATE TABLE m_c (id int);")

    async def body(db: Database) -> None:
        """Apply and inspect."""
        with pytest.raises(KorbaseError, match="0002_b.sql"):
            await apply(db, tmp_path)
        assert await db.fetch("SELECT to_regclass('m_a') IS NOT NULL") == [(True,)]
        assert await db.fetch("SELECT to_regclass('m_b') IS NOT NULL") == [(False,)]
        assert dict(await status(db, tmp_path)) == {
            "0001_a.sql": "applied",
            "0002_b.sql": "pending",
            "0003_c.sql": "pending",
        }
        # The lock was released: a fixed file now applies.
        _write(tmp_path, "0002_b.sql", "CREATE TABLE m_b (id int);")
        assert await apply(db, tmp_path) == ["0002_b.sql", "0003_c.sql"]

    _run(dsn, body)


def test_no_transaction(dsn: str, tmp_path: Path) -> None:
    """A no-transaction file can run CREATE INDEX CONCURRENTLY."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE m_a (id int);")
    _write(tmp_path, "0002_b.sql", "-- korbase:no-transaction\nCREATE INDEX CONCURRENTLY m_a_id ON m_a (id);")

    async def body(db: Database) -> None:
        """Apply."""
        assert await apply(db, tmp_path) == ["0001_a.sql", "0002_b.sql"]

    _run(dsn, body)


def test_concurrent_apply(dsn: str, tmp_path: Path) -> None:
    """Two concurrent runs apply each file exactly once."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE m_a (id int);")
    _write(tmp_path, "0002_b.sql", "CREATE TABLE m_b (id int);")

    async def body(db: Database) -> None:
        """Race two databases."""
        async with await Database.connect(dsn) as other:
            first, second = await asyncio.gather(apply(db, tmp_path), apply(other, tmp_path))
        assert sorted(first + second) == ["0001_a.sql", "0002_b.sql"]

    _run(dsn, body)


def test_standard_conforming_strings_forced_on(dsn: str, tmp_path: Path) -> None:
    """Files run with standard_conforming_strings on, whatever the role default."""
    _write(
        tmp_path,
        "0001_a.sql",
        "CREATE TABLE m_scs (v text);\nINSERT INTO m_scs SELECT current_setting('standard_conforming_strings');",
    )

    async def body(db: Database) -> None:
        """Set the role default off, apply, read the recorded value."""
        await db.execute("ALTER ROLE CURRENT_USER SET standard_conforming_strings = off")
        try:
            await apply(db, tmp_path)
        finally:
            await db.execute("ALTER ROLE CURRENT_USER RESET standard_conforming_strings")
        rows = await db.fetch("SELECT v FROM m_scs")
        assert rows == [("on",)]

    _run(dsn, body)


def _sqlite_run(tmp_path: Path, body: Callable[[Database, Database], Awaitable[Any]]) -> Any:
    """Run `body` with two Database objects on one SQLite file."""

    async def main() -> Any:
        """Connect twice, run."""
        dsn = f"sqlite:///{tmp_path / 'ap.db'}"
        async with await Database.connect(dsn) as a, await Database.connect(dsn) as b:
            return await body(a, b)

    return asyncio.run(main())


def test_sqlite_apply_status_refusals(tmp_path: Path) -> None:
    """On SQLite: apply once, no-transaction file, tampered and missing refusals."""
    d = tmp_path / "mig"
    d.mkdir()
    _write(d, "0001_a.sql", "CREATE TABLE m_a (id int);")
    _write(d, "0002_b.sql", "-- korbase:no-transaction\nCREATE TABLE m_b (id int);")

    async def body(db: Database, _other: Database) -> None:
        """Apply, tamper, delete."""
        assert await status(db, d) == [("0001_a.sql", "pending"), ("0002_b.sql", "pending")]
        assert await apply(db, d) == ["0001_a.sql", "0002_b.sql"]
        assert await apply(db, d) == []
        _write(d, "0001_a.sql", "CREATE TABLE m_a (id int, x int);")
        with pytest.raises(KorbaseError, match="changed"):
            await apply(db, d)
        (d / "0001_a.sql").unlink()
        with pytest.raises(KorbaseError, match="missing"):
            await apply(db, d)

    _sqlite_run(tmp_path, body)


def test_sqlite_concurrent_apply(tmp_path: Path) -> None:
    """Two applies on one file apply each migration once."""
    d = tmp_path / "mig"
    d.mkdir()
    for i in range(1, 6):
        _write(d, f"000{i}_t{i}.sql", f"CREATE TABLE t{i} (id int);")

    async def body(a: Database, b: Database) -> None:
        """Race the two."""
        first, second = await asyncio.gather(apply(a, d), apply(b, d), return_exceptions=True)
        ran = [x for x in (first, second) if isinstance(x, list)]
        assert ran
        assert sorted(n for x in ran for n in x) == sorted(f"000{i}_t{i}.sql" for i in range(1, 6))

    _sqlite_run(tmp_path, body)
