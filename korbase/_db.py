"""The user-facing `Database` and `Transaction`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Optional

from korbase._backend import Backend, Conn, Pool
from korbase._build import named_builder, positional_builder, validating_builder
from korbase._expr import Param
from korbase._ident import KorbaseError
from korbase._query import Compiled, _Query, _Select
from korbase._relations import load_relations

__all__ = ["Database", "Transaction"]


def _args(compiled: Compiled, params: dict[str, Any]) -> list[Any]:
    """Map `params` to the positional arguments of `compiled`."""
    names = {s.name for s in compiled.slots if isinstance(s, Param)}
    missing = sorted(names - params.keys())
    extra = sorted(params.keys() - names)
    if missing:
        raise KorbaseError(f"missing params: {', '.join(missing)}")
    if extra:
        raise KorbaseError(f"unexpected params: {', '.join(extra)}")
    return [params[s.name] if isinstance(s, Param) else s.value for s in compiled.slots]


def _row_builder(query: _Query, validate: bool) -> Any:
    """Return the row builder for the columns of the query's model."""
    if validate:
        return validating_builder(query._meta)
    return positional_builder(query._meta)


def _by_name(model: type) -> Any:
    """Return a builder that maps a driver record to `model` by column name."""

    def build(row: Any) -> Any:
        """Build one model from a record that knows its column names."""
        return named_builder(model, tuple(row.keys()))(row)

    return build


class _Runner:
    """Shared query methods; subclasses say how to get a connection."""

    _validate = False

    def _conn(self) -> AbstractAsyncContextManager[Conn]:
        """Return a context manager that yields a connection."""
        raise NotImplementedError

    async def run(self, query: _Query, **params: Any) -> Any:
        """Run a query builder with `params`.

        A select returns a list of models. Insert or upsert with
        `.returning()` returns a list of models; other writes return the
        command tag.
        """
        compiled = query.compile()
        args = _args(compiled, params)
        async with self._conn() as conn:
            if isinstance(query, _Select):
                rows = await conn.fetch(compiled.sql, args, _row_builder(query, self._validate))
                await load_relations(conn, rows, query._loads, self._validate)
                return rows
            if getattr(query, "_ret", False):
                return await conn.fetch(compiled.sql, args, _row_builder(query, self._validate))
            return await conn.execute(compiled.sql, args)

    async def first(self, query: _Query, **params: Any) -> Any:
        """Run a select (or a write with `.returning()`) and return the first model or `None`."""
        compiled = query.compile()
        args = _args(compiled, params)
        if not isinstance(query, _Select) and not getattr(query, "_ret", False):
            raise KorbaseError("first() needs a select or a query with .returning()")
        async with self._conn() as conn:
            row = await conn.fetchrow(compiled.sql, args, _row_builder(query, self._validate))
            if row is not None and isinstance(query, _Select):
                await load_relations(conn, [row], query._loads, self._validate)
            return row

    async def fetch(self, sql: str, *args: Any, model: Optional[type] = None) -> list[Any]:
        """Run raw SQL; rows become `model` (by column name) or tuples."""
        builder = tuple if model is None else _by_name(model)
        async with self._conn() as conn:
            return await conn.fetch(sql, args, builder)

    async def fetchrow(self, sql: str, *args: Any, model: Optional[type] = None) -> Any:
        """Run raw SQL and return the first row, or `None`."""
        builder = tuple if model is None else _by_name(model)
        async with self._conn() as conn:
            return await conn.fetchrow(sql, args, builder)

    async def execute(self, sql: str, *args: Any) -> str:
        """Run raw SQL and return the command tag."""
        async with self._conn() as conn:
            return await conn.execute(sql, args)


class Transaction(_Runner):
    """Queries bound to one connection inside a transaction."""

    def __init__(self, conn: Conn, validate: bool) -> None:
        """Bind to `conn`; use `Database.transaction()` instead."""
        self._bound = conn
        self._validate = validate

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[Conn]:
        """Yield the bound connection."""
        yield self._bound

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """Open a nested transaction, which is a savepoint."""
        async with self._bound.transaction():
            yield Transaction(self._bound, self._validate)


class Database(_Runner):
    """A pool of connections plus the methods to run queries."""

    def __init__(self, pool: Pool, validate: bool = False) -> None:
        """Wrap `pool`; use `Database.connect()` instead."""
        self._pool = pool
        self._validate = validate

    @classmethod
    async def connect(
        cls, dsn: str, *, backend: Optional[Backend] = None, validate: bool = False, **opts: Any
    ) -> Database:
        """Open a pool on `dsn`. `validate=True` checks every row against its model."""
        if backend is None:
            from korbase._asyncpg import AsyncpgBackend

            backend = AsyncpgBackend()
        return cls(await backend.connect(dsn, **opts), validate)

    def _conn(self) -> AbstractAsyncContextManager[Conn]:
        """Borrow a pooled connection."""
        return self._pool.acquire()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """Open a transaction on one connection: commit on success, roll back on error."""
        async with self._pool.acquire() as conn, conn.transaction():
            yield Transaction(conn, self._validate)

    async def close(self) -> None:
        """Close the pool."""
        await self._pool.close()

    async def __aenter__(self) -> Database:
        """Return the database."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Close the pool."""
        await self.close()
