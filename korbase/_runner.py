"""Apply reviewed SQL migration files, one connection, one lock."""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from korbase._backend import Conn
from korbase._db import Database
from korbase._dialect import PG, SQLITE, Dialect
from korbase._ident import KorbaseError
from korbase._sqlite import _split_statements

__all__ = ["apply", "status"]

_NAME = re.compile(r"(\d{4,})_[a-z0-9_]+\.sql")
_LOCK_KEY = int.from_bytes(hashlib.sha256(b"korbase.migrate").digest()[:8], "big", signed=True)
_REVIEW = "-- korbase:review"
_NO_TX = "-- korbase:no-transaction"
_TABLE = (
    "CREATE TABLE IF NOT EXISTS korbase_migrations ("
    "name text PRIMARY KEY, checksum text NOT NULL, "
    "applied_at timestamptz NOT NULL DEFAULT now())"
)
_TABLE_SQLITE = (
    "CREATE TABLE IF NOT EXISTS korbase_migrations ("
    "name TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
    "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)
_EXISTS_SQLITE = "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'korbase_migrations'"


@dataclass(frozen=True)
class _File:
    """One migration file: its name, text and SHA-256 checksum."""

    name: str
    text: str
    checksum: str


def _order(name: str) -> tuple[int, str]:
    """Return the sort key of a migration name: its number, then the name."""
    return int(name.split("_", 1)[0]), name


def _read_files(directory: str | os.PathLike[str]) -> list[_File]:
    """Read the migration files directly inside `directory`, in order."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise KorbaseError(f"not a directory: {directory}")
    files = []
    for entry in os.scandir(root):
        if not entry.name.endswith(".sql"):
            continue
        if _NAME.fullmatch(entry.name) is None:
            raise KorbaseError(f"bad migration file name: {entry.name!r}")
        path = (root / entry.name).resolve()
        if path.parent != root or not path.is_file():
            raise KorbaseError(f"migration file escapes the directory: {entry.name!r}")
        data = path.read_bytes()
        try:
            text = data.decode()
        except UnicodeDecodeError as exc:
            raise KorbaseError(f"migration file is not UTF-8: {entry.name!r}") from exc
        files.append(_File(entry.name, text, hashlib.sha256(data).hexdigest()))
    return sorted(files, key=lambda f: _order(f.name))


async def _applied(conn: Conn) -> dict[str, str]:
    """Return the applied migrations as name to checksum."""
    rows = await conn.fetch("SELECT name, checksum FROM korbase_migrations", [], tuple)
    return {name: checksum for name, checksum in rows}


def _classify(files: list[_File], applied: dict[str, str]) -> list[tuple[str, str]]:
    """Return (name, state) for every known migration, in order."""
    by_name = {f.name: f for f in files}
    result = []
    for name in sorted(by_name.keys() | applied.keys(), key=_order):
        if name not in applied:
            state = "pending"
        elif name not in by_name:
            state = "missing"
        elif by_name[name].checksum != applied[name]:
            state = "changed"
        else:
            state = "applied"
        result.append((name, state))
    return result


def _check(files: list[_File], states: list[tuple[str, str]], applied: dict[str, str]) -> list[_File]:
    """Refuse unsafe states and return the pending files."""
    for name, state in states:
        if state in ("missing", "changed"):
            raise KorbaseError(f"applied migration {name} is {state}; nothing applied")
    last = max((_order(n) for n in applied), default=None)
    pending = [f for f in files if f.name not in applied]
    for f in pending:
        if last is not None and _order(f.name) < last:
            raise KorbaseError(f"pending migration {f.name} sorts before the last applied one")
        if _REVIEW in f.text:
            raise KorbaseError(f"migration {f.name} still has a {_REVIEW} marker")
    return pending


async def _record(conn: Conn, f: _File, dialect: Dialect) -> None:
    """Insert the version row of `f`."""
    marks = f"{dialect.placeholder(1)}, {dialect.placeholder(2)}"
    await conn.execute(f"INSERT INTO korbase_migrations (name, checksum) VALUES ({marks})", [f.name, f.checksum])


async def _body(conn: Conn, f: _File, dialect: Dialect) -> bool:
    """Run the script of `f` and record it; on SQLite skip a file another apply recorded first."""
    if dialect.name == "sqlite":
        mark = dialect.placeholder(1)
        if await conn.fetch(f"SELECT 1 FROM korbase_migrations WHERE name = {mark}", [f.name], tuple):
            return False
    await conn.execute_script(f.text)
    await _record(conn, f, dialect)
    return True


async def _claim(conn: Conn, f: _File) -> bool:
    """Insert the version row of `f` in a short write transaction; False if another apply holds it."""
    async with conn.transaction():
        mark = SQLITE.placeholder(1)
        if await conn.fetch(f"SELECT 1 FROM korbase_migrations WHERE name = {mark}", [f.name], tuple):
            return False
        await _record(conn, f, SQLITE)
    return True


async def _run_checked(conn: Conn, text: str) -> None:
    """Run a SQLite script statement by statement; rows from `PRAGMA foreign_key_check` fail it."""
    for stmt in _split_statements(text):
        rows = await conn.fetch(stmt, [], tuple)
        if rows and "foreign_key_check" in stmt:
            broken = ", ".join(f"{t} ({n})" for t, n in Counter(r[0] for r in rows).items())
            raise KorbaseError(f"foreign key check failed, broken rows in: {broken}")


async def _undo_claimed(conn: Conn, f: _File) -> None:
    """After a failure: roll back, turn foreign keys on again and delete the claim row."""
    with suppress(Exception):
        await conn.execute("ROLLBACK", [])
    await conn.execute("PRAGMA foreign_keys=ON", [])
    async with conn.transaction():
        mark = SQLITE.placeholder(1)
        await conn.execute(f"DELETE FROM korbase_migrations WHERE name = {mark}", [f.name])


async def _run_claimed(conn: Conn, f: _File) -> bool:
    """Run a no-transaction file on SQLite after claiming it; undo and release the claim if it fails."""
    if not await _claim(conn, f):
        return False
    try:
        await _run_checked(conn, f.text)
        await conn.execute("PRAGMA foreign_keys=ON", [])
    except BaseException:
        await _undo_claimed(conn, f)
        raise
    return True


async def _run_file(conn: Conn, f: _File, dialect: Dialect) -> bool:
    """Run one file, in a transaction unless it opts out; False when it was already applied."""
    try:
        if f.text.startswith(_NO_TX) and dialect.name == "sqlite":
            return await _run_claimed(conn, f)
        if f.text.startswith(_NO_TX):
            return await _body(conn, f, dialect)
        async with conn.transaction():
            return await _body(conn, f, dialect)
    except KorbaseError:
        raise
    except Exception as exc:
        raise KorbaseError(f"migration {f.name} failed: {exc}") from exc


async def _apply_pg(conn: Conn, files: list[_File]) -> list[str]:
    """Apply the pending files on Postgres under an advisory lock."""
    done: list[str] = []
    await conn.execute("SELECT pg_advisory_lock($1)", [_LOCK_KEY])
    before = None
    try:
        before = (await conn.fetch("SELECT current_setting('standard_conforming_strings')", [], tuple))[0][0]
        await conn.execute("SELECT set_config('standard_conforming_strings', 'on', false)", [])
        await conn.execute_script(_TABLE)
        applied = await _applied(conn)
        for f in _check(files, _classify(files, applied), applied):
            await _run_file(conn, f, PG)
            done.append(f.name)
    finally:
        if before is not None:
            await conn.execute("SELECT set_config('standard_conforming_strings', $1, false)", [before])
        await conn.execute("SELECT pg_advisory_unlock($1)", [_LOCK_KEY])
    return done


async def _apply_sqlite(conn: Conn, files: list[_File]) -> list[str]:
    """Apply the pending files on SQLite; each file's `BEGIN IMMEDIATE` is the lock."""
    await conn.execute_script(_TABLE_SQLITE)
    applied = await _applied(conn)
    pending = _check(files, _classify(files, applied), applied)
    return [f.name for f in pending if await _run_file(conn, f, SQLITE)]


async def apply(db: Database, directory: str | os.PathLike[str]) -> list[str]:
    """Apply the pending migration files in `directory` and return their names.

    Runs on one connection, so concurrent callers apply each file once: on
    Postgres under an advisory lock, on SQLite because each file runs in a
    write transaction that first checks the version table. Refuses, applying
    nothing, when an applied file is missing or changed, when a pending file
    sorts before the last applied one, or when a pending file contains
    `-- korbase:review`. A file whose first line is `-- korbase:no-transaction`
    runs outside a transaction. It stops at the first failing file; earlier
    files stay applied.

    On SQLite a no-transaction file is claimed first: its version row is
    inserted in a short write transaction, then the file runs. If it fails,
    the row is deleted. If the process crashes in between, the row stays and
    the file counts as applied; check what the file did, then delete its row
    from `korbase_migrations` to run it again.
    """
    files = _read_files(directory)
    async with db._conn() as conn:
        if db._dialect.name == "sqlite":
            return await _apply_sqlite(conn, files)
        return await _apply_pg(conn, files)


async def status(db: Database, directory: str | os.PathLike[str]) -> list[tuple[str, str]]:
    """Return (name, state) per migration: applied, pending, changed or missing."""
    files = _read_files(directory)
    async with db._conn() as conn:
        if db._dialect.name == "sqlite":
            found = await conn.fetch(_EXISTS_SQLITE, [], tuple)
        else:
            found = await conn.fetch("SELECT to_regclass('korbase_migrations') IS NOT NULL", [], tuple)
        applied = await _applied(conn) if found and found[0][0] else {}
    return _classify(files, applied)
