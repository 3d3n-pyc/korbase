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
from korbase.migrate import apply, collect_models, create_all, dev, make, status

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


def test_cli_dsn_flag_wins(dsn: str, tmp_path: Path) -> None:
    """--dsn beats KORBASE_DSN."""
    (tmp_path / "mig").mkdir()
    env = {**os.environ, "KORBASE_DSN": "postgresql://u:pw@127.0.0.1:1/x"}
    cmd = [sys.executable, "-m", "korbase.migrate", "status", "--dir", "mig", "--dsn", dsn]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr


def test_make_no_models(dsn: str, tmp_path: Path) -> None:
    """An empty model list is an error, in the API and the CLI."""
    (tmp_path / "mg_empty.py").write_text("X = 1\n")

    async def body(db: Database) -> None:
        with pytest.raises(KorbaseError, match="no models"):
            await make(db, [], tmp_path / "mig", "x")

    _run(dsn, body)
    r = _cli(dsn, tmp_path, "make", "--models", "mg_empty", "--dir", "mig")
    assert r.returncode == 1
    assert "no models found in mg_empty" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_errors_are_one_line(dsn: str, tmp_path: Path) -> None:
    """Unexpected errors give one line and exit 1, never a traceback or the DSN."""
    (tmp_path / "mig").write_text("not a directory")
    r = _cli(dsn, tmp_path, "apply", "--dir", "mig")
    assert r.returncode == 1
    assert r.stderr.startswith("error: ") and "Traceback" not in r.stderr
    assert dsn not in r.stderr


def test_make_scopes_to_models(dsn: str, tmp_path: Path) -> None:
    """Unrelated tables are kept by default; all_tables proposes a flagged drop."""
    d = tmp_path / "mig"

    async def body(db: Database) -> None:
        """Create an unrelated table, then compare both ways."""
        await db.execute("CREATE TABLE mg_other (id int PRIMARY KEY)")
        m = _models(tmp_path, "mg_s1", V1)
        f = await make(db, m, d, "init")
        assert f is not None and "mg_other" not in f.read_text()
        await apply(db, d)
        assert await make(db, m, d, "again") is None
        g = await make(db, m, d, "all", all_tables=True)
        assert g is not None
        text = g.read_text()
        assert "DROP TABLE" in text and "mg_other" in text and "-- korbase:review" in text

    _run(dsn, body)


def test_removed_model_dropped_with_all_tables(dsn: str, tmp_path: Path) -> None:
    """Removing a model and using all_tables proposes dropping its table."""
    d = tmp_path / "mig"
    other = V1.replace("mg_item", "mg_gone")

    async def body(db: Database) -> None:
        """Create two models' tables, then keep only one."""
        both = _models(tmp_path, "mg_r1", V1) + _models(tmp_path, "mg_r2", other)
        await make(db, both, d, "init")
        await apply(db, d)
        keep = _models(tmp_path, "mg_r3", V1)
        assert await make(db, keep, d, "x") is None
        f = await make(db, keep, d, "drop", all_tables=True)
        assert f is not None and "DROP TABLE" in f.read_text() and "mg_gone" in f.read_text()

    _run(dsn, body)


def test_dev_applies_safe_change(dsn: str, tmp_path: Path) -> None:
    """dev writes and applies a safe change; a second run finds nothing."""
    d = tmp_path / "mig"

    async def body(db: Database) -> None:
        """Run dev twice."""
        m = _models(tmp_path, "mg_d1", V1)
        f = await dev(db, m, d)
        assert f is not None and await status(db, d) == [(f.name, "applied")]
        assert await db.fetch("SELECT 1 FROM pg_tables WHERE tablename = 'mg_item'")
        assert await dev(db, m, d) is None

    _run(dsn, body)


def test_dev_stops_on_review(dsn: str, tmp_path: Path) -> None:
    """dev refuses a flagged change and applies nothing."""
    d = tmp_path / "mig"

    async def body(db: Database) -> None:
        """Create, then drop a column through dev."""
        await dev(db, _models(tmp_path, "mg_e1", V1), d)
        with pytest.raises(KorbaseError, match="korbase:review"):
            await dev(db, _models(tmp_path, "mg_e3", V3), d)
        assert [s for _, s in await status(db, d)] == ["applied", "pending"]

    _run(dsn, body)


def _sqlite_run(tmp_path: Path, body: Callable[[Database], Awaitable[Any]]) -> Any:
    """Run `body` on a fresh SQLite file."""

    async def main_() -> Any:
        """Connect and run."""
        async with await Database.connect(f"sqlite:///{tmp_path / 'mg.db'}") as db:
            return await body(db)

    return asyncio.run(main_())


def test_sqlite_end_to_end(tmp_path: Path) -> None:
    """make, apply, status, no changes, then an added nullable column."""
    d = tmp_path / "mig"

    async def body(db: Database) -> None:
        """Walk through the workflow."""
        first = await make(db, _models(tmp_path, "ms_v1", V1), d, "init")
        assert first is not None and first.name == "0001_init.sql"
        assert await apply(db, d) == [first.name]
        assert await status(db, d) == [(first.name, "applied")]
        assert await make(db, _models(tmp_path, "ms_v1", V1), d, "again") is None
        second = await make(db, _models(tmp_path, "ms_v2", V2), d, "more")
        assert second is not None and "ADD COLUMN" in second.read_text()
        assert await apply(db, d) == [second.name]
        assert await make(db, _models(tmp_path, "ms_v2", V2), d, "again") is None

    _sqlite_run(tmp_path, body)


def test_sqlite_type_change_needs_review(tmp_path: Path) -> None:
    """A type change is flagged for review and apply refuses until it is removed."""
    d = tmp_path / "mig"
    changed = V1.replace("name: str", "name: int")

    async def body(db: Database) -> None:
        """Create, then change a type."""
        await make(db, _models(tmp_path, "ms_t1", V1), d, "init")
        await apply(db, d)
        f = await make(db, _models(tmp_path, "ms_t2", changed), d, "retype")
        assert f is not None and "-- korbase:review" in f.read_text()
        with pytest.raises(KorbaseError):
            await apply(db, d)
        f.write_text(f.read_text().replace("-- korbase:review", "-- reviewed"))
        assert await apply(db, d) == [f.name]

    _sqlite_run(tmp_path, body)


def test_sqlite_cli(tmp_path: Path) -> None:
    """make, apply and status work from the shell on a sqlite DSN."""
    (tmp_path / "ms_cli.py").write_text(V1)
    dsn = f"sqlite:///{tmp_path / 'cli.db'}"
    r = _cli(dsn, tmp_path, "make", "--models", "ms_cli", "--dir", "mig", "-m", "init")
    assert r.returncode == 0, r.stderr
    assert _cli(dsn, tmp_path, "apply", "--dir", "mig").returncode == 0
    assert _cli(dsn, tmp_path, "status", "--dir", "mig").stdout.strip() == "applied 0001_init.sql"
    assert _cli(dsn, tmp_path, "make", "--models", "ms_cli", "--dir", "mig").stdout.strip() == "no changes"
