"""The user-facing `Database` and `Transaction`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Optional, TypeVar, Union, overload

from korbase._backend import Backend, Conn, Pool
from korbase._build import named_builder, positional_builder, validating_builder
from korbase._expr import Param
from korbase._ident import KorbaseError
from korbase._query import (
    Compiled,
    Delete,
    DeleteReturning,
    Insert,
    Select,
    Update,
    UpdateReturning,
    Upsert,
    _Query,
)
from korbase._relations import load_relations

__all__ = ["Database", "Transaction"]

M = TypeVar("M")
Rows = Union[Select[M], Insert[M], Upsert[M], UpdateReturning[M], DeleteReturning[M]]
Count = Union[Update[M], Delete[M]]


def _args(compiled: Compiled, params: dict[str, Any]) -> list[Any]:
    """Map `params` to the positional arguments of `compiled`."""
    names = {s.name for s in compiled.slots if isinstance(s, Param)}
    missing = sorted(names - params.keys())
    extra = sorted(params.keys() - names)
    if missing:
        raise KorbaseError(f"missing params: {', '.join(missing)}")
    if extra:
        raise KorbaseError(f"unexpected params: {', '.join(extra)}")
    if any(v is None for v in params.values()):
        _reject_null_eq(compiled, params)
    return [params[s.name] if isinstance(s, Param) else s.value for s in compiled.slots]


def _reject_null_eq(compiled: Compiled, params: dict[str, Any]) -> None:
    """Raise if a `None` param is compared with `==`, `!=` or `.in_()`, which never match NULL."""
    for s in compiled.slots:
        if isinstance(s, Param) and s.eq and params[s.name] is None:
            raise KorbaseError(
                f"param {s.name!r} is None but is compared with ==, != or in_(), which never match NULL: "
                "use .is_null() or .where(column=None)"
            )


def _row_builder(query: _Query[Any], validate: bool) -> Any:
    """Return the row builder for the columns of the query's model."""
    if validate:
        return validating_builder(query._meta)
    return positional_builder(query._meta)


def _count(tag: str) -> int:
    """Return the row count at the end of a command tag: `"UPDATE 3"` gives 3."""
    return int(tag.rsplit(" ", 1)[-1])


def _by_name(model: type, names: Sequence[str], validate: bool) -> Any:
    """Return a builder that maps a row with column `names` to `model`."""
    if not validate:
        return named_builder(model, tuple(names))
    import msgspec

    def build(row: Sequence[Any]) -> Any:
        """Validate one row and build the model."""
        data = dict(zip(names, row, strict=True))
        return msgspec.convert(data, model, from_attributes=True)

    return build


class _Runner:
    """Shared query methods; subclasses say how to get a connection."""

    _validate = False

    def _conn(self) -> AbstractAsyncContextManager[Conn]:
        """Return a context manager that yields a connection."""
        raise NotImplementedError

    @overload
    async def run(self, query: Rows[M], /, **params: Any) -> list[M]: ...

    @overload
    async def run(self, query: Count[M], /, **params: Any) -> int: ...

    async def run(self, query: Any, /, **params: Any) -> Any:
        """Run a query builder with `params`.

        A select, insert, upsert, or an update or delete with `.returning()`,
        returns a list of models. Any other update or delete returns the
        number of rows it changed.
        """
        compiled = query.compile()
        args = _args(compiled, params)
        async with self._conn() as conn:
            if not query.returns_rows:
                return _count(await conn.execute(compiled.sql, args))
            rows = await conn.fetch(compiled.sql, args, _row_builder(query, self._validate))
            if isinstance(query, Select):
                await load_relations(conn, rows, query._loads, self._validate)
            return rows

    async def first(self, query: Rows[M], /, **params: Any) -> Optional[M]:
        """Run a query that returns rows and return the first model, or `None`.

        Other updates and deletes raise `KorbaseError`: add `.returning()`.
        """
        compiled = query.compile()
        args = _args(compiled, params)
        if not query.returns_rows:
            raise KorbaseError("first() needs a query that returns rows: add .returning()")
        async with self._conn() as conn:
            row = await conn.fetchrow(compiled.sql, args, _row_builder(query, self._validate))
            if row is not None and isinstance(query, Select):
                await load_relations(conn, [row], query._loads, self._validate)
            return row

    @overload
    async def fetch(self, sql: str, *args: Any, model: type[M]) -> list[M]: ...

    @overload
    async def fetch(self, sql: str, *args: Any, model: None = None) -> list[tuple[Any, ...]]: ...

    async def fetch(self, sql: str, *args: Any, model: Optional[type] = None) -> list[Any]:
        """Run raw SQL; rows become `model` (by column name) or tuples."""
        async with self._conn() as conn:
            if model is None:
                return await conn.fetch(sql, args, tuple)
            names, rows = await conn.fetch_columns(sql, args)
        build = _by_name(model, names, self._validate)
        return [build(r) for r in rows]

    @overload
    async def fetchrow(self, sql: str, *args: Any, model: type[M]) -> Optional[M]: ...

    @overload
    async def fetchrow(self, sql: str, *args: Any, model: None = None) -> Optional[tuple[Any, ...]]: ...

    async def fetchrow(self, sql: str, *args: Any, model: Optional[type] = None) -> Any:
        """Run raw SQL and return the first row, or `None`."""
        async with self._conn() as conn:
            if model is None:
                return await conn.fetchrow(sql, args, tuple)
            names, rows = await conn.fetch_columns(sql, args)
        build = _by_name(model, names, self._validate)
        return build(rows[0]) if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        """Run raw SQL and return the command tag, such as `"UPDATE 3"`."""
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
