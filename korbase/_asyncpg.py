"""The asyncpg backend."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from types import CoroutineType
from typing import Any, ParamSpec, TypeVar

from korbase._backend import M, Pool, RowBuilder
from korbase._errors import (
    CheckViolation,
    ConnectionFailed,
    DatabaseBusy,
    DatabaseError,
    ForeignKeyViolation,
    IntegrityError,
    NotNullViolation,
    UniqueViolation,
)

__all__ = ["AsyncpgBackend"]

_P = ParamSpec("_P")
_T = TypeVar("_T")

_INTEGRITY = {
    "23505": UniqueViolation,
    "23503": ForeignKeyViolation,
    "23502": NotNullViolation,
    "23514": CheckViolation,
}


def _translate(exc: Exception) -> DatabaseError | None:
    """Return the Korbase error for an asyncpg or network error, or `None` to leave it alone.

    The message is the server's main message only: `exc.detail` can hold row
    values, so it stays on `__cause__`.
    """
    import asyncpg

    message = str(exc.args[0]) if exc.args else str(exc)
    if isinstance(exc, asyncpg.PostgresError):
        state = getattr(exc, "sqlstate", None) or ""
        constraint = getattr(exc, "constraint_name", None)
        if state in _INTEGRITY:
            return _INTEGRITY[state](message, constraint=constraint)
        if state.startswith("23"):
            return IntegrityError(message, constraint=constraint)
        if state in ("55P03", "40P01"):
            return DatabaseBusy(message)
        if state.startswith("08"):
            return ConnectionFailed(message)
        return DatabaseError(message, constraint=constraint)
    if isinstance(exc, asyncpg.InterfaceError):
        return ConnectionFailed(message) if "closed" in message else None
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return ConnectionFailed(message)
    return None


def _translated(fn: Callable[_P, CoroutineType[Any, Any, _T]]) -> Callable[_P, CoroutineType[Any, Any, _T]]:
    """Re-raise asyncpg errors from the async function `fn` as Korbase errors."""

    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        """Call `fn`; translate errors only on the failure path."""
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            err = _translate(exc)
            if err is None:
                raise
            raise err from exc

    return wrapper


class _Transaction:
    """An asyncpg transaction whose errors, commit included, are Korbase errors."""

    def __init__(self, tx: Any) -> None:
        """Wrap an asyncpg transaction."""
        self._tx = tx

    @_translated
    async def __aenter__(self) -> None:
        """Start the transaction."""
        await self._tx.__aenter__()

    @_translated
    async def __aexit__(self, *exc: Any) -> None:
        """Commit, or roll back when the body raised."""
        await self._tx.__aexit__(*exc)


class _AsyncpgConn:
    """A `Conn` over one asyncpg connection."""

    def __init__(self, conn: Any) -> None:
        """Wrap an asyncpg connection."""
        self._conn = conn

    @_translated
    async def fetch(self, sql: str, args: Sequence[object], builder: RowBuilder[M]) -> list[M]:
        """Run `sql` and build one model per record."""
        records = await self._conn.fetch(sql, *args)
        return [builder(r) for r in records]

    @_translated
    async def fetchrow(self, sql: str, args: Sequence[object], builder: RowBuilder[M]) -> M | None:
        """Run `sql` and build the first record, or return `None`."""
        record = await self._conn.fetchrow(sql, *args)
        return None if record is None else builder(record)

    @_translated
    async def fetch_columns(self, sql: str, args: Sequence[object]) -> tuple[Sequence[str], list[Sequence[object]]]:
        """Run `sql` through a prepared statement; return column names and records."""
        stmt = await self._conn.prepare(sql)
        names = [a.name for a in stmt.get_attributes()]
        return names, list(await stmt.fetch(*args))

    @_translated
    async def execute(self, sql: str, args: Sequence[object]) -> str:
        """Run `sql` and return the status tag."""
        return await self._conn.execute(sql, *args)

    @_translated
    async def execute_script(self, sql: str) -> None:
        """Run a multi-statement SQL text with the simple protocol."""
        await self._conn.execute(sql)

    def transaction(self) -> AbstractAsyncContextManager[None]:
        """Open an asyncpg transaction."""
        return _Transaction(self._conn.transaction())


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

    @_translated
    async def __aenter__(self) -> _AsyncpgConn:
        """Acquire a connection."""
        self._ctx = self._pool.acquire()
        return _AsyncpgConn(await self._ctx.__aenter__())

    async def __aexit__(self, *exc: Any) -> None:
        """Release the connection."""
        await self._ctx.__aexit__(*exc)


class AsyncpgBackend:
    """Backend on `asyncpg`. Install asyncpg to use it."""

    @_translated
    async def connect(self, dsn: str, **opts: Any) -> Pool:
        """Create an asyncpg pool; `opts` go to `asyncpg.create_pool`."""
        import asyncpg

        return _AsyncpgPool(await asyncpg.create_pool(dsn, **opts))  # type: ignore[return-value]
