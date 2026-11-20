"""Immutable query builders, compiled once to SQL and parameter slots."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Generic, Optional, TypeVar, Union

from korbase._dialect import PG, Dialect
from korbase._expr import (
    Column,
    Expr,
    Order,
    Param,
    ParamCollector,
    RelRef,
    Value,
    compile_expr,
)
from korbase._ident import KorbaseError, quote_ident
from korbase._meta import TableMeta, table_meta

__all__ = [
    "Slot",
    "Compiled",
    "Select",
    "Insert",
    "Upsert",
    "Update",
    "UpdateReturning",
    "Delete",
    "DeleteReturning",
    "select",
    "insert",
    "update",
    "delete",
    "upsert",
]

M = TypeVar("M")
_Q = TypeVar("_Q", bound="_Query[Any]")

Slot = Union[Param, Value]


@dataclass(frozen=True)
class Compiled:
    """SQL text plus the slots that fill its numbered placeholders (`$n` or `?n`), in order."""

    sql: str
    slots: tuple[Slot, ...]


class _Query(Generic[M]):
    """Base of the builders: immutable, with a memoised `compile()`.

    `model` is the `@table` class; `returns_rows` tells whether running the
    query gives back rows (True) or only a count of written rows (False).
    """

    returns_rows: bool = False

    def __init__(self, model: type[M]) -> None:
        """Bind the builder to `model`, which must be a `@table` model."""
        self.model = model
        self._meta: TableMeta = table_meta(model)
        self._compiled: Optional[Compiled] = None
        self._compiled_alt: Optional[tuple[Dialect, Compiled]] = None

    def _replace(self: _Q, **changes: Any) -> _Q:
        """Return a copy with `changes` applied and no memoised result."""
        new = copy.copy(self)
        for key, value in changes.items():
            setattr(new, key, value)
        new._compiled = None
        new._compiled_alt = None
        return new

    def _become(self, cls: Any) -> Any:
        """Return a copy of this builder as an instance of `cls`."""
        new = cls.__new__(cls)
        new.__dict__.update(self.__dict__)
        new._compiled = None
        new._compiled_alt = None
        return new

    def _build(self, ctx: ParamCollector) -> str:
        """Return the SQL text, adding parameters to `ctx`."""
        raise NotImplementedError

    def compile(self, dialect: Dialect = PG) -> Compiled:
        """Return the SQL and slots for `dialect`; computed once per dialect, then reused."""
        if dialect is PG:
            if self._compiled is None:
                self._compiled = self._compile_for(PG)
            return self._compiled
        alt = self._compiled_alt
        if alt is None or alt[0] is not dialect:
            alt = self._compiled_alt = (dialect, self._compile_for(dialect))
        return alt[1]

    def _compile_for(self, dialect: Dialect) -> Compiled:
        """Build the `Compiled` of this query for `dialect`."""
        ctx = ParamCollector(dialect)
        sql = self._build(ctx)
        return Compiled(sql, tuple(ctx.items))

    def _table(self) -> str:
        """Return the quoted table name."""
        return quote_ident(self._meta.name)

    def _check_columns(self, names: Any) -> None:
        """Raise `KorbaseError` if a name is not a column of the table."""
        known = {c.name for c in self._meta.columns}
        for name in names:
            if name not in known:
                raise KorbaseError(f"table {self._meta.name!r} has no column {name!r}")

    def _returning(self) -> str:
        """Return a `RETURNING` clause for every column."""
        return " RETURNING " + ", ".join(quote_ident(c.name) for c in self._meta.columns)

    def _tail(self) -> str:
        """Return the `RETURNING` clause for builders that return rows."""
        return self._returning() if self.returns_rows else ""


class _Filtered(_Query[M]):
    """A builder with a `WHERE` filter."""

    def __init__(self, model: type[M]) -> None:
        """Start with no filter."""
        super().__init__(model)
        self._where: Optional[Expr] = None

    def where(self: _Q, *conditions: Expr, **equals: Any) -> _Q:
        """Filter rows; conditions and `column=value` pairs are ANDed.

        A keyword value may be a `param(...)` or a plain value; `None` means
        `IS NULL`. Several calls are combined with AND.
        """
        return self._replace(_where=_filter(self, conditions, equals))  # type: ignore[attr-defined]


def _filter(query: Any, conditions: tuple[Any, ...], equals: dict[str, Any]) -> Any:
    """Return `query`'s filter ANDed with the conditions and keyword equalities."""
    if not conditions and not equals:
        raise TypeError("where() needs at least one condition")
    query._check_columns(equals)
    exprs = list(conditions) + [Column(query._meta, k) == v for k, v in equals.items()]
    where = query._where
    for expr in exprs:
        where = _cond(where, expr)
    return where


def _cond(current: Optional[Any], expr: Any) -> Any:
    """Return `current` ANDed with `expr`; `expr` must be an expression."""
    if not isinstance(expr, Expr):
        raise TypeError(f"where() takes a korbase expression, got {type(expr).__name__}")
    return expr if current is None else current & expr


def _where(query: Any, ctx: ParamCollector) -> str:
    """Return the ` WHERE ...` clause, or an empty string."""
    if query._where is None:
        return ""
    return " WHERE " + compile_expr(query._where, ctx)


def _paging(value: Any) -> Slot:
    """Turn a limit or offset into a slot; ints are never inlined."""
    if isinstance(value, Param):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Value(value)
    raise KorbaseError(f"limit and offset take a param or an int, got {value!r}")


def _slots(values: dict[str, Any]) -> dict[str, Slot]:
    """Wrap plain values of a `values` or `set` mapping as `Value`."""
    return {k: v if isinstance(v, (Param, Value)) else Value(v) for k, v in values.items()}


class Select(_Filtered[M]):
    """A `SELECT` over all columns of one model; runs as rows."""

    returns_rows = True

    def __init__(self, model: type[M]) -> None:
        """Start with no filter, order, paging or loads."""
        super().__init__(model)
        self._orders: tuple[Order, ...] = ()
        self._limit: Optional[Slot] = None
        self._offset: Optional[Slot] = None
        self._loads: tuple[RelRef, ...] = ()

    def order_by(self, *orders: Union[Column[Any], Order]) -> Select[M]:
        """Sort rows, using `col.asc()` or `col.desc()`."""
        for o in orders:
            if not isinstance(o, (Column, Order)):
                raise TypeError(f"order_by() takes columns or .asc()/.desc(), got {type(o).__name__}")
        return self._replace(_orders=self._orders + tuple(orders))

    def limit(self, value: Union[Param, int]) -> Select[M]:
        """Return at most `value` rows."""
        return self._replace(_limit=_paging(value))

    def offset(self, value: Union[Param, int]) -> Select[M]:
        """Skip the first `value` rows."""
        return self._replace(_offset=_paging(value))

    def load(self, *relations: RelRef) -> Select[M]:
        """Remember relations to load along with the rows."""
        return self._replace(_loads=self._loads + relations)

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `SELECT` statement."""
        t = self._table()
        names = ", ".join(f"{t}.{quote_ident(c.name)}" for c in self._meta.columns)
        sql = f"SELECT {names} FROM {t}" + _where(self, ctx)
        if self._orders:
            sql += " ORDER BY " + ", ".join(compile_expr(o, ctx) for o in self._orders)
        if self._limit is not None:
            sql += f" LIMIT {ctx.add(self._limit)}"
        if self._offset is not None:
            sql += f" OFFSET {ctx.add(self._offset)}"
        return sql


class Insert(_Query[M]):
    """An `INSERT` of one row; always returns the row with all its columns."""

    returns_rows = True

    def __init__(self, model: type[M]) -> None:
        """Start with the default values."""
        super().__init__(model)
        self._values: Optional[dict[str, Slot]] = None

    def values(self: _Q, **values: Any) -> _Q:
        """Set the columns to insert; plain values become anonymous slots."""
        self._check_columns(values)  # type: ignore[attr-defined]
        return self._replace(_values=_slots(values))

    def _row(self) -> dict[str, Slot]:
        """Return the values to insert, defaulting to same-named params."""
        if self._values is not None:
            return self._values
        return {c.name: Param(c.name) for c in self._meta.columns if c.default is None}

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `INSERT` statement."""
        row = self._row()
        if not row:
            return f"INSERT INTO {self._table()} DEFAULT VALUES" + self._tail()
        names = ", ".join(quote_ident(k) for k in row)
        marks = ", ".join(ctx.add(v) for v in row.values())
        sql = f"INSERT INTO {self._table()} ({names}) VALUES ({marks})" + self._conflict_sql()
        return sql + self._tail()

    def _conflict_sql(self) -> str:
        """Return the `ON CONFLICT` clause; empty for a plain insert."""
        return ""


class Update(_Filtered[M]):
    """An `UPDATE` with a mandatory filter or `.all_rows()`; runs as a count."""

    def __init__(self, model: type[M]) -> None:
        """Start with nothing to set and no filter."""
        super().__init__(model)
        self._set: dict[str, Slot] = {}
        self._all = False

    def set(self, **values: Any) -> Update[M]:
        """Set columns; plain values become anonymous slots."""
        self._check_columns(values)
        return self._replace(_set={**self._set, **_slots(values)})

    def all_rows(self) -> Update[M]:
        """Allow the update to touch every row."""
        return self._replace(_all=True)

    def returning(self) -> UpdateReturning[M]:
        """Return the updated rows with all their columns."""
        return self._become(UpdateReturning)

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `UPDATE` statement."""
        if not self._set:
            raise KorbaseError("update needs .set(...)")
        if self._where is None and not self._all:
            raise KorbaseError("update without where; call .all_rows() to confirm")
        sets = ", ".join(f"{quote_ident(k)} = {ctx.add(v)}" for k, v in self._set.items())
        return f"UPDATE {self._table()} SET {sets}" + _where(self, ctx) + self._tail()


class UpdateReturning(Update[M]):
    """An `UPDATE ... RETURNING`; runs as rows."""

    returns_rows = True


class Delete(_Filtered[M]):
    """A `DELETE` with a mandatory filter or `.all_rows()`; runs as a count."""

    def __init__(self, model: type[M]) -> None:
        """Start with no filter."""
        super().__init__(model)
        self._all = False

    def all_rows(self) -> Delete[M]:
        """Allow the delete to touch every row."""
        return self._replace(_all=True)

    def returning(self) -> DeleteReturning[M]:
        """Return the deleted rows with all their columns."""
        return self._become(DeleteReturning)

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `DELETE` statement."""
        if self._where is None and not self._all:
            raise KorbaseError("delete without where; call .all_rows() to confirm")
        return f"DELETE FROM {self._table()}" + _where(self, ctx) + self._tail()


class DeleteReturning(Delete[M]):
    """A `DELETE ... RETURNING`; runs as rows."""

    returns_rows = True


class Upsert(Insert[M]):
    """An `INSERT ... ON CONFLICT ... DO UPDATE`; returns the written row."""

    def __init__(self, model: type[M]) -> None:
        """Start with no conflict columns."""
        super().__init__(model)
        self._conflict: tuple[str, ...] = ()

    def on_conflict(self, *columns: str) -> Upsert[M]:
        """Name the columns whose clash turns the insert into an update."""
        self._check_columns(columns)
        return self._replace(_conflict=tuple(columns))

    def _conflict_sql(self) -> str:
        """Return the `ON CONFLICT ... DO UPDATE` clause."""
        if not self._conflict:
            raise KorbaseError("upsert needs .on_conflict(...)")
        skip = set(self._conflict) | set(self._meta.pk)
        keys = [k for k in self._row() if k not in skip]
        cl = ", ".join(quote_ident(c) for c in self._conflict)
        if not keys:
            return f" ON CONFLICT ({cl}) DO NOTHING"
        sets = ", ".join(f"{quote_ident(k)} = EXCLUDED.{quote_ident(k)}" for k in keys)
        return f" ON CONFLICT ({cl}) DO UPDATE SET {sets}"


def select(model: type[M]) -> Select[M]:
    """Start a `SELECT` over all columns of `model`."""
    return Select(model)


def insert(model: type[M]) -> Insert[M]:
    """Start an `INSERT` into the table of `model`; it returns the new row."""
    return Insert(model)


def update(model: type[M]) -> Update[M]:
    """Start an `UPDATE` of the table of `model`."""
    return Update(model)


def delete(model: type[M]) -> Delete[M]:
    """Start a `DELETE` from the table of `model`."""
    return Delete(model)


def upsert(model: type[M]) -> Upsert[M]:
    """Start an `INSERT ... ON CONFLICT DO UPDATE` into the table of `model`; it returns the row."""
    return Upsert(model)
