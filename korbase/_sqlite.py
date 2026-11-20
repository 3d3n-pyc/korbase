"""The SQLite backend, on the standard library `sqlite3` module."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, TypeVar

from korbase._backend import M, Pool, RowBuilder

__all__ = ["SqliteBackend"]

T = TypeVar("T")
_MIN_VERSION = (3, 35)
_MEMORY = ":memory:"


def _parse_dsn(dsn: str) -> str:
    """Return the database path of a `sqlite:` DSN, or `:memory:`."""
    if not dsn.startswith("sqlite:"):
        raise ValueError(f"not a sqlite DSN: {dsn!r}")
    rest = dsn[len("sqlite:") :]
    if rest.startswith("//"):
        rest = rest[2:]
        # `sqlite:///x` -> "/x" -> relative "x"; `sqlite:////x` -> "//x" -> absolute "/x"
        rest = rest[1:]
    if not rest:
        raise ValueError("empty sqlite database path")
    return rest


def _tag(sql: str, rowcount: int) -> str:
    """Build a Postgres-like status tag from the statement kind and row count."""
    words = sql.split(None, 1)
    kind = words[0].upper() if words else ""
    n = max(rowcount, 0)
    if kind == "INSERT":
        return f"INSERT 0 {n}"
    if kind in ("UPDATE", "DELETE"):
        return f"{kind} {n}"
    if kind in ("SELECT", "WITH", "VALUES"):
        return f"SELECT {n}"
    return "OK"


def _split_statements(sql: str) -> list[str]:
    """Split a SQL text into complete statements (triggers and string literals stay whole)."""
    statements: list[str] = []
    current = ""
    for line in sql.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            statements.append(current)
            current = ""
    if current.strip():
        statements.append(current)
    return statements


class _SqliteConn:
    """A `Conn` over one sqlite3 connection that lives on its own thread."""

    def __init__(self, path: str, busy_timeout_ms: int) -> None:
        """Remember the settings; the connection opens on the worker thread."""
        self._path = path
        self._busy = int(busy_timeout_ms)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="korbase-sqlite")
        self._db: sqlite3.Connection | None = None
        self._depth = 0

    async def _run(self, fn: Callable[[], T]) -> T:
        """Run `fn` on this connection's thread."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn)

    async def open(self) -> None:
        """Open the database and apply the settings."""
        await self._run(self._open)

    def _open(self) -> None:
        """Open the sqlite3 connection (worker thread)."""
        db = sqlite3.connect(self._path, isolation_level=None)
        if self._path != _MEMORY:
            db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA busy_timeout={self._busy}")
        self._db = db

    def _conn(self) -> sqlite3.Connection:
        """Return the open connection (worker thread)."""
        if self._db is None:
            raise RuntimeError("connection is closed")
        return self._db

    async def fetch(self, sql: str, args: Sequence[object], builder: RowBuilder[M]) -> list[M]:
        """Run `sql` and build one model per row."""
        _, rows = await self.fetch_columns(sql, args)
        return [builder(r) for r in rows]

    async def fetchrow(self, sql: str, args: Sequence[object], builder: RowBuilder[M]) -> M | None:
        """Run `sql` and build the first row, or return `None`."""
        _, rows = await self.fetch_columns(sql, args)
        return builder(rows[0]) if rows else None

    async def fetch_columns(self, sql: str, args: Sequence[object]) -> tuple[Sequence[str], list[Sequence[object]]]:
        """Run `sql`; return the column names and the rows as tuples."""

        def work() -> tuple[Sequence[str], list[Sequence[object]]]:
            cur = self._conn().execute(sql, tuple(args))
            names = [d[0] for d in cur.description or ()]
            return names, cur.fetchall()

        return await self._run(work)

    async def execute(self, sql: str, args: Sequence[object]) -> str:
        """Run `sql` and return a Postgres-like status tag."""

        def work() -> str:
            cur = self._conn().execute(sql, tuple(args))
            cur.fetchall()
            return _tag(sql, cur.rowcount)

        return await self._run(work)

    async def execute_script(self, sql: str) -> None:
        """Run a multi-statement text one statement at a time, without committing."""

        def work() -> None:
            db = self._conn()
            for stmt in _split_statements(sql):
                db.execute(stmt).fetchall()

        await self._run(work)

    def transaction(self) -> AbstractAsyncContextManager[None]:
        """Open a transaction, or a savepoint when already inside one."""
        return self._transaction()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """Run `BEGIN IMMEDIATE` or `SAVEPOINT`, then commit or roll back."""
        nested = self._depth > 0
        name = f"k_{self._depth}"
        await self._run(lambda: self._conn().execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE"))
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            await self._finish(nested, name, commit=False)
            raise
        self._depth -= 1
        await self._finish(nested, name, commit=True)

    async def _finish(self, nested: bool, name: str, *, commit: bool) -> None:
        """Commit, release or roll back the current transaction level."""
        if nested:
            stmts = [f"RELEASE {name}"] if commit else [f"ROLLBACK TO {name}", f"RELEASE {name}"]
        else:
            stmts = ["COMMIT" if commit else "ROLLBACK"]

        def work() -> None:
            for s in stmts:
                self._conn().execute(s)

        await self._run(work)

    async def close(self) -> None:
        """Close the connection and stop its thread."""
        if self._db is not None:
            await self._run(self._db.close)
            self._db = None
        self._executor.shutdown(wait=True)


class _SqlitePool:
    """A `Pool` of `_SqliteConn`, handed out through a queue."""

    def __init__(self, conns: list[_SqliteConn]) -> None:
        """Fill the queue with the open connections."""
        self._conns = conns
        self._queue: asyncio.Queue[_SqliteConn] = asyncio.Queue()
        for c in conns:
            self._queue.put_nowait(c)

    def acquire(self) -> AbstractAsyncContextManager[_SqliteConn]:
        """Borrow a connection for an `async with` block."""
        return self._acquire()

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[_SqliteConn]:
        """Take a connection from the queue and give it back after."""
        conn = await self._queue.get()
        try:
            yield conn
        finally:
            self._queue.put_nowait(conn)

    async def close(self) -> None:
        """Close every connection and stop the threads."""
        for c in self._conns:
            await c.close()


class SqliteBackend:
    """Backend on the standard library `sqlite3`. Needs SQLite 3.35 or later."""

    async def connect(self, dsn: str, *, size: int = 1, busy_timeout_ms: int = 5000, **opts: Any) -> Pool:
        """Open a pool on `sqlite:///path.db` or `sqlite::memory:`.

        `:memory:` always uses one connection: sharing one in-memory
        database between connections is not supported.
        """
        if opts:
            raise TypeError(f"unknown sqlite options: {', '.join(sorted(opts))}")
        if sqlite3.sqlite_version_info < _MIN_VERSION:
            raise RuntimeError(f"Korbase needs SQLite 3.35 or later, found {sqlite3.sqlite_version}")
        path = _parse_dsn(dsn)
        count = 1 if path == _MEMORY else max(1, int(size))
        conns = [_SqliteConn(path, busy_timeout_ms) for _ in range(count)]
        try:
            for c in conns:
                await c.open()
        except BaseException:
            for c in conns:
                await c.close()
            raise
        return _SqlitePool(conns)  # type: ignore[return-value]
