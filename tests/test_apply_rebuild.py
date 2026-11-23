"""Tests for applying generated SQLite table rebuilds."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from korbase import Database
from korbase._ident import KorbaseError
from korbase.migrate import apply, make

PARENT = """
from typing import Annotated
import msgspec
from korbase import PK, table

@table("rb_parent")
class Parent(msgspec.Struct):
    id: Annotated[int, PK()]
"""
CHILD_V1 = """
from korbase import Index, FK

@table("rb_child", indexes=(Index("label"),))
class Child(msgspec.Struct):
    id: Annotated[int, PK()]
    parent_id: Annotated[int, FK("rb_parent.id")]
    label: str | None = None
"""
CHILD_V2 = CHILD_V1.replace("str | None = None", "str = ''")


def _models(tmp_path: Path, name: str, child: str) -> list[type]:
    """Write a models module and return its tables."""
    (tmp_path / f"{name}.py").write_text(PARENT + child.replace("from korbase import", "from korbase import FK,", 1))
    sys.path.insert(0, str(tmp_path))
    mod = __import__(name)
    return [mod.Parent, mod.Child]


def _run(tmp_path: Path, body: Callable[[Database], Awaitable[Any]]) -> None:
    """Run `body` on a fresh SQLite file."""

    async def main() -> None:
        """Connect and run."""
        async with await Database.connect(f"sqlite:///{tmp_path / 'rb.db'}") as db:
            await body(db)

    asyncio.run(main())


async def _setup(db: Database, tmp_path: Path, tag: str) -> Path:
    """Apply the first version and insert one parent and one child."""
    d = tmp_path / "mig"
    await make(db, _models(tmp_path, f"rb_a{tag}", CHILD_V1), d, "init")
    await apply(db, d)
    async with db._conn() as conn:
        await conn.execute("INSERT INTO rb_parent (id) VALUES (1)", [])
        await conn.execute("INSERT INTO rb_child (id, parent_id, label) VALUES (1, 1, 'x')", [])
    return d


def _reviewed(f: Path | None) -> Path:
    """Accept the review marker of a generated file and return the file."""
    assert f is not None
    f.write_text(f.read_text().replace("-- korbase:review", "-- reviewed"))
    return f


async def _fk_on(db: Database) -> tuple[object, ...]:
    """Return PRAGMA foreign_keys and whether a transaction is open, on the pooled connection."""
    async with db._conn() as conn:
        rows = await conn.fetch("PRAGMA foreign_keys", [], tuple)
        return rows[0][0], conn._db.in_transaction


def test_rebuild_keeps_rows_and_indexes(tmp_path: Path) -> None:
    """A nullability rebuild keeps the rows and the indexes; foreign keys end on."""

    async def body(db: Database) -> None:
        """Rebuild the child table."""
        d = await _setup(db, tmp_path, "1")
        f = _reviewed(await make(db, _models(tmp_path, "rb_b1", CHILD_V2), d, "notnull"))
        assert "no-transaction" in f.read_text()
        assert await apply(db, d) == [f.name]
        async with db._conn() as conn:
            assert await conn.fetch("SELECT id, parent_id FROM rb_child", [], tuple) == [(1, 1)]
            idx = await conn.fetch(
                "SELECT name FROM sqlite_schema WHERE type='index' AND tbl_name='rb_child'", [], tuple
            )
            assert any("label" in n for (n,) in idx)
        assert await _fk_on(db) == (1, False)

    _run(tmp_path, body)


def test_rebuild_fk_violation_fails(tmp_path: Path) -> None:
    """Broken foreign keys fail the rebuild, roll it back and leave foreign keys on."""

    async def body(db: Database) -> None:
        """Orphan a child, then rebuild."""
        d = await _setup(db, tmp_path, "2")
        _reviewed(await make(db, _models(tmp_path, "rb_b2", CHILD_V2), d, "notnull"))
        con = sqlite3.connect(tmp_path / "rb.db", isolation_level=None)
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("DELETE FROM rb_parent")
        con.close()
        with pytest.raises(KorbaseError, match="rb_child"):
            await apply(db, d)
        async with db._conn() as conn:
            assert await conn.fetch("SELECT name FROM korbase_migrations", [], tuple) == [("0001_init.sql",)]
            cols = await conn.fetch(
                "SELECT \"notnull\" FROM pragma_table_info('rb_child') WHERE name='label'", [], tuple
            )
            assert cols == [(0,)]
        assert await _fk_on(db) == (1, False)

    _run(tmp_path, body)


def test_rebuild_midway_failure_rolls_back(tmp_path: Path) -> None:
    """A bad statement in the middle rolls everything back and leaves foreign keys on."""

    async def body(db: Database) -> None:
        """Break the generated file, then apply."""
        d = await _setup(db, tmp_path, "3")
        f = _reviewed(await make(db, _models(tmp_path, "rb_b3", CHILD_V2), d, "notnull"))
        f.write_text(f.read_text().replace("COMMIT;", "SELECT * FROM nope;\nCOMMIT;"))
        with pytest.raises(KorbaseError):
            await apply(db, d)
        async with db._conn() as conn:
            assert await conn.fetch("SELECT name FROM korbase_migrations", [], tuple) == [("0001_init.sql",)]
            tables = await conn.fetch("SELECT name FROM sqlite_schema WHERE name LIKE '%__new'", [], tuple)
            assert tables == []
        assert await _fk_on(db) == (1, False)

    _run(tmp_path, body)
