"""Tests for korbase.migrate: make, apply, status and the command line."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from korbase import Database
from korbase._ident import KorbaseError
from korbase.migrate import apply, collect_models, create_all, make, status

V1 = """
from typing import Annotated
import msgspec
from korbase import PK, table

@table("mg_item")
class Item(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str
"""
V2 = V1 + "    note: str | None = None\n"
V2 = V2.replace('@table("mg_item")', '@table("mg_item", indexes=(Index("name"),))').replace(
    "import PK, table", "import PK, Index, table"
)
V3 = V1.replace("    name: str\n", "")


def _models(tmp_path: Path, name: str, source: str) -> list[type]:
    """Write a models module under a unique name and return its tables."""
    (tmp_path / f"{name}.py").write_text(source)
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    return collect_models(name)


async def _clean(db: Database) -> None:
    """Drop every table in public, so the diff starts from an empty schema."""
    rows = await db.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    for (name,) in rows:
        await db.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')


def _run(dsn: str, body: Callable[[Database], Awaitable[Any]]) -> Any:
    """Run `body` on a cleaned database."""

    async def main_() -> Any:
        """Connect, clean, run, clean."""
        async with await Database.connect(dsn) as db:
            await _clean(db)
            try:
                return await body(db)
            finally:
                await _clean(db)

    return asyncio.run(main_())


def test_end_to_end(dsn: str, tmp_path: Path) -> None:
    """make, apply, status, no-change make, then a changed model."""
    d = tmp_path / "mig"

    async def body(db: Database) -> None:
        """Walk through the workflow."""
        m1 = _models(tmp_path, "mg_v1", V1)
        first = await make(db, m1, d, "Initial Setup!")
        assert first is not None and first.name == "0001_initial_setup.sql"
        assert await apply(db, d) == [first.name]
        assert await status(db, d) == [(first.name, "applied")]
        assert await make(db, m1, d, "again") is None
        m2 = _models(tmp_path, "mg_v2", V2)
        second = await make(db, m2, d, "more")
        assert second is not None and second.name == "0002_more.sql"
        text = second.read_text()
        assert "ADD COLUMN" in text and "CREATE INDEX" in text
        assert await apply(db, d) == [second.name]
        rows = await db.fetch("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'korbase_scratch_%'")
        assert rows == []

    _run(dsn, body)


def test_destructive_needs_review(dsn: str, tmp_path: Path) -> None:
    """A drop column is flagged and apply refuses until the flag is removed."""
    d = tmp_path / "mig"

    async def body(db: Database) -> None:
        """Create, then drop a column."""
        await make(db, _models(tmp_path, "mg_w1", V1), d, "init")
        await apply(db, d)
        f = await make(db, _models(tmp_path, "mg_w3", V3), d, "drop")
        assert f is not None and "-- korbase:review" in f.read_text()
        with pytest.raises(KorbaseError):
            await apply(db, d)
        f.write_text(f.read_text().replace("-- korbase:review", "-- reviewed"))
        assert await apply(db, d) == [f.name]

    _run(dsn, body)


def test_create_all_convenience(dsn: str, tmp_path: Path) -> None:
    """create_all(db, models) creates the table."""

    async def body(db: Database) -> None:
        """Create and check."""
        await create_all(db, _models(tmp_path, "mg_c1", V1))
        assert await db.fetch("SELECT to_regclass('mg_item')::text") == [("mg_item",)]

    _run(dsn, body)


def _cli(dsn: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the command line with the DSN in the environment."""
    env = {**os.environ, "KORBASE_DSN": dsn, "PYTHONPATH": str(tmp_path)}
    cmd = [sys.executable, "-m", "korbase.migrate", *args]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=tmp_path)


def test_cli(dsn: str, tmp_path: Path) -> None:
    """make, apply and status work from the shell."""
    (tmp_path / "mg_cli.py").write_text(V1)
    _run(dsn, lambda db: asyncio.sleep(0))
    try:
        r = _cli(dsn, tmp_path, "make", "--models", "mg_cli", "--dir", "mig", "-m", "init")
        assert r.returncode == 0, r.stderr
        assert "0001_init.sql" in r.stdout
        assert _cli(dsn, tmp_path, "apply", "--dir", "mig").returncode == 0
        r = _cli(dsn, tmp_path, "status", "--dir", "mig")
        assert r.stdout.strip() == "applied 0001_init.sql"
        r = _cli(dsn, tmp_path, "make", "--models", "mg_cli", "--dir", "mig")
        assert r.stdout.strip() == "no changes"
    finally:
        _run(dsn, lambda db: asyncio.sleep(0))


def test_cli_bad_dsn_hidden(tmp_path: Path) -> None:
    """A bad DSN fails with exit code 1 and is not printed."""
    bad = "postgresql://user:s3cretpw@127.0.0.1:1/nope"
    r = _cli(bad, tmp_path, "status", "--dir", "mig")
    assert r.returncode == 1
    assert "s3cretpw" not in r.stderr and "s3cretpw" not in r.stdout
