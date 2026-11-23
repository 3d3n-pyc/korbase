"""The user-facing `Database` and `Transaction`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Optional, TypeVar, Union, overload

from korbase._backend import Backend, Conn, Pool
from korbase._build import decoding_rows, named_builder, positional_builder, tuple_builder, validating_builder
from korbase._dialect import PG, Dialect
from korbase._expr import Param
from korbase._ident import KorbaseError
from korbase._query import (
    Compiled,
    Delete,
    DeleteReturning,
    Insert,
    Select,
    SelectColumns,
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


def _row_builder(query: _Query[Any], validate: bool, dialect: Dialect) -> Any:
    """Return the row builder for the columns of the query's model."""
    if isinstance(query, SelectColumns):
        return tuple_builder(query._columns, query.compile(dialect).outputs, dialect)
    if validate:
        return validating_builder(query._meta, dialect)
    return positional_builder(query._meta, dialect)


def _count(tag: str) -> int:
    """Return the row count at the end of a command tag: `"UPDATE 3"` gives 3."""
    return int(tag.rsplit(" ", 1)[-1])


def _by_name(model: type, names: Sequence[str], validate: bool, dialect: Dialect) -> Any:
    """Return a builder that maps a row with column `names` to `model`."""
    if not validate:
        return named_builder(model, tuple(names), dialect)
    import msgspec

    decode = None if dialect is PG else decoding_rows(model, names, dialect)

    def build(row: Sequence[Any]) -> Any:
        """Validate one row and build the model."""
        data = dict(zip(names, row if decode is None else decode(row), strict=True))
        return msgspec.convert(data, model, from_attributes=True)

    return build


def _backend_for(dsn: str) -> Backend:
    """Return the backend for the scheme of `dsn`; the DSN is never shown in errors."""
    scheme = dsn.split(":", 1)[0].lower() if ":" in dsn else ""
    if scheme in ("postgresql", "postgres"):
        from korbase._asyncpg import AsyncpgBackend

        return AsyncpgBackend()
    if scheme == "sqlite":
        from korbase._sqlite import SqliteBackend

        return SqliteBackend()
    raise KorbaseError("unknown database scheme: use postgresql://, postgres:// or sqlite:")


class _Runner:
    """Shared query methods; subclasses say how to get a connection."""

    _validate = False
    _dialect: Dialect = PG

    def _conn(self) -> AbstractAsyncContextManager[Conn]:
        """Return a context manager that yields a connection."""
        raise NotImplementedError

    @overload
    async def run(self, query: SelectColumns[M], /, **params: Any) -> list[M]: ...

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
        compiled = query.compile(self._dialect)
        args = _args(compiled, params)
        async with self._conn() as conn:
            if not query.returns_rows:
                return _count(await conn.execute(compiled.sql, args))
            rows = await conn.fetch(compiled.sql, args, _row_builder(query, self._validate, self._dialect))
            if isinstance(query, Select):
                await self._load(conn, rows, query)
            return rows

    async def _load(self, conn: Conn, rows: list[Any], query: Any) -> None:
        """Load the relations of `query` into `rows`, decoding children by dialect."""
        await load_relations(conn, rows, query._loads, self._validate, self._dialect)

    @overload
    async def first(self, query: SelectColumns[M], /, **params: Any) -> Optional[M]: ...

    @overload
    async def first(self, query: Rows[M], /, **params: Any) -> Optional[M]: ...

    async def first(self, query: Any, /, **params: Any) -> Any:
        """Run a query that returns rows and return the first model (or tuple), or `None`.

        Other updates and deletes raise `KorbaseError`: add `.returning()`.
        """
        compiled = query.compile(self._dialect)
        args = _args(compiled, params)
        if not query.returns_rows:
            raise KorbaseError("first() needs a query that returns rows: add .returning()")
        async with self._conn() as conn:
            row = await conn.fetchrow(compiled.sql, args, _row_builder(query, self._validate, self._dialect))
            if row is not None and isinstance(query, Select):
                await self._load(conn, [row], query)
            return row

    async def value(self, query: SelectColumns[tuple[M]], /, **params: Any) -> Optional[M]:
        """Return the first column of the first row, or `None` when there is no row.

        The query must select exactly one column or aggregate::

            n = await db.value(select(Task).columns(count()).where(done=False)) or 0
        """
        if len(query._columns) != 1:
            raise KorbaseError("value() needs a query with exactly one column or aggregate")
        row = await self.first(query, **params)
        return None if row is None else row[0]

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
        build = _by_name(model, names, self._validate, self._dialect)
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
        build = _by_name(model, names, self._validate, self._dialect)
        return build(rows[0]) if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        """Run raw SQL and return the command tag, such as `"UPDATE 3"`."""
        async with self._conn() as conn:
            return await conn.execute(sql, args)


class Transaction(_Runner):
    """Queries bound to one connection inside a transaction."""

    def __init__(self, conn: Conn, validate: bool, dialect: Dialect = PG) -> None:
        """Bind to `conn`; use `Database.transaction()` instead."""
        self._bound = conn
        self._validate = validate
        self._dialect = dialect

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[Conn]:
        """Yield the bound connection."""
        yield self._bound

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """Open a nested transaction, which is a savepoint."""
        async with self._bound.transaction():
            yield Transaction(self._bound, self._validate, self._dialect)


class Database(_Runner):
    """A pool of connections plus the methods to run queries."""

    def __init__(self, pool: Pool, validate: bool = False, dialect: Dialect = PG) -> None:
        """Wrap `pool`; use `Database.connect()` instead."""
        self._pool = pool
        self._validate = validate
        self._dialect = dialect

    @classmethod
    async def connect(
        cls, dsn: str, *, backend: Optional[Backend] = None, validate: bool = False, **opts: Any
    ) -> Database:
        """Open a pool on `dsn`. `validate=True` checks every row against its model.

        Without `backend`, the DSN scheme picks it: `postgresql://` or
        `postgres://` for Postgres, `sqlite:` for SQLite.
        """
        if backend is None:
            backend = _backend_for(dsn)
        dialect = getattr(backend, "dialect", PG)
        return cls(await backend.connect(dsn, **opts), validate, dialect)

    def _conn(self) -> AbstractAsyncContextManager[Conn]:
        """Borrow a pooled connection."""
        return self._pool.acquire()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """Open a transaction on one connection: commit on success, roll back on error."""
        async with self._pool.acquire() as conn, conn.transaction():
            yield Transaction(conn, self._validate, self._dialect)

    async def close(self) -> None:
        """Close the pool."""
        await self._pool.close()

    async def __aenter__(self) -> Database:
        """Return the database."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Close the pool."""
        await self.close()
