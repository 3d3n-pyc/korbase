"""The asyncpg backend."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from korbase._backend import M, Pool, RowBuilder

__all__ = ["AsyncpgBackend"]


class _AsyncpgConn:
    """A `Conn` over one asyncpg connection."""

    def __init__(self, conn: Any) -> None:
        """Wrap an asyncpg connection."""
        self._conn = conn

    async def fetch(self, sql: str, args: Sequence[object], builder: RowBuilder[M]) -> list[M]:
        """Run `sql` and build one model per record."""
        records = await self._conn.fetch(sql, *args)
        return [builder(r) for r in records]

    async def fetchrow(self, sql: str, args: Sequence[object], builder: RowBuilder[M]) -> M | None:
        """Run `sql` and build the first record, or return `None`."""
        record = await self._conn.fetchrow(sql, *args)
        return None if record is None else builder(record)

    async def fetch_columns(self, sql: str, args: Sequence[object]) -> tuple[Sequence[str], list[Sequence[object]]]:
        """Run `sql` through a prepared statement; return column names and records."""
        stmt = await self._conn.prepare(sql)
        names = [a.name for a in stmt.get_attributes()]
        return names, list(await stmt.fetch(*args))

    async def execute(self, sql: str, args: Sequence[object]) -> str:
        """Run `sql` and return the status tag."""
        return await self._conn.execute(sql, *args)

    def transaction(self) -> AbstractAsyncContextManager[None]:
        """Open an asyncpg transaction."""
        return self._conn.transaction()  # type: ignore[no-any-return]


class _AsyncpgPool:
    """A `Pool` over an asyncpg pool."""

    def __init__(self, pool: Any) -> None:
        """Wrap an asyncpg pool."""
        self._pool = pool

    def acquire(self) -> AbstractAsyncContextManager[_AsyncpgConn]:
        """Borrow a connection."""
        return _Acquire(self._pool)

    async def close(self) -> None:
        """Close the pool."""
        await self._pool.close()


class _Acquire:
    """Async context manager that yields a wrapped connection."""

    def __init__(self, pool: Any) -> None:
        """Remember the pool to borrow from."""
        self._pool = pool
        self._ctx: Any = None

    async def __aenter__(self) -> _AsyncpgConn:
        """Acquire a connection."""
        self._ctx = self._pool.acquire()
        return _AsyncpgConn(await self._ctx.__aenter__())

    async def __aexit__(self, *exc: Any) -> None:
        """Release the connection."""
        await self._ctx.__aexit__(*exc)


class AsyncpgBackend:
    """Backend on `asyncpg`. Install asyncpg to use it."""

    async def connect(self, dsn: str, **opts: Any) -> Pool:
        """Create an asyncpg pool; `opts` go to `asyncpg.create_pool`."""
        import asyncpg

        return _AsyncpgPool(await asyncpg.create_pool(dsn, **opts))  # type: ignore[return-value]
