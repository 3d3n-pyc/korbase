"""Immutable query builders, compiled once to SQL and parameter slots."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Optional, Union

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

__all__ = ["Slot", "Compiled", "select", "insert", "update", "delete", "upsert"]

Slot = Union[Param, Value]


@dataclass(frozen=True)
class Compiled:
    """SQL text plus the slots that fill its `$n` placeholders, in order."""

    sql: str
    slots: tuple[Slot, ...]


class _Query:
    """Base of the builders: immutable, with a memoised `compile()`."""

    def __init__(self, model: type) -> None:
        """Bind the builder to `model`, which must be a `@table` model."""
        self.model = model
        self._meta: TableMeta = table_meta(model)
        self._compiled: Optional[Compiled] = None

    def _replace(self, **changes: Any) -> Any:
        """Return a copy with `changes` applied and no memoised result."""
        new = copy.copy(self)
        for key, value in changes.items():
            setattr(new, key, value)
        new._compiled = None
        return new

    def _build(self, ctx: ParamCollector) -> str:
        """Return the SQL text, adding parameters to `ctx`."""
        raise NotImplementedError

    def compile(self) -> Compiled:
        """Return the SQL and slots; computed once, then reused."""
        if self._compiled is None:
            ctx = ParamCollector()
            sql = self._build(ctx)
            self._compiled = Compiled(sql, tuple(ctx.items))
        return self._compiled

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


def _and(left: Optional[Any], right: Any) -> Any:
    """Combine two conditions with AND, `left` may be empty."""
    return right if left is None else left & right


def _cond(query: Any, expr: Any) -> Any:
    """Return `query`'s filter combined with `expr`; `expr` must be an expression."""
    if not isinstance(expr, Expr):
        raise TypeError(f"where() takes a korbase expression, got {type(expr).__name__}")
    return _and(query._where, expr)


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


class _Select(_Query):
    """A `SELECT` over all columns of one model."""

    def __init__(self, model: type) -> None:
        """Start with no filter, order, paging or loads."""
        super().__init__(model)
        self._where: Optional[Expr] = None
        self._orders: tuple[Order, ...] = ()
        self._limit: Optional[Slot] = None
        self._offset: Optional[Slot] = None
        self._loads: tuple[RelRef, ...] = ()

    def where(self, expr: Expr) -> _Select:
        """Filter rows; several calls are combined with AND."""
        return self._replace(_where=_cond(self, expr))

    def order_by(self, *orders: Union[Column[Any], Order]) -> _Select:
        """Sort rows, using `col.asc()` or `col.desc()`."""
        for o in orders:
            if not isinstance(o, (Column, Order)):
                raise TypeError(f"order_by() takes columns or .asc()/.desc(), got {type(o).__name__}")
        return self._replace(_orders=self._orders + tuple(orders))

    def limit(self, value: Union[Param, int]) -> _Select:
        """Return at most `value` rows."""
        return self._replace(_limit=_paging(value))

    def offset(self, value: Union[Param, int]) -> _Select:
        """Skip the first `value` rows."""
        return self._replace(_offset=_paging(value))

    def load(self, *relations: RelRef) -> _Select:
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


class _Insert(_Query):
    """An `INSERT` of one row."""

    def __init__(self, model: type) -> None:
        """Start with the default values and no `RETURNING`."""
        super().__init__(model)
        self._values: Optional[dict[str, Slot]] = None
        self._ret = False

    def values(self, **values: Any) -> _Insert:
        """Set the columns to insert; plain values become anonymous slots."""
        self._check_columns(values)
        return self._replace(_values=_slots(values))

    def returning(self) -> _Insert:
        """Return the inserted row with all its columns."""
        return self._replace(_ret=True)

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

    def _tail(self) -> str:
        """Return the `RETURNING` clause when asked for, else nothing."""
        return self._returning() if self._ret else ""

    def _conflict_sql(self) -> str:
        """Return the `ON CONFLICT` clause; empty for a plain insert."""
        return ""


class _Update(_Query):
    """An `UPDATE` with a mandatory filter or `.all_rows()`."""

    def __init__(self, model: type) -> None:
        """Start with nothing to set and no filter."""
        super().__init__(model)
        self._set: dict[str, Slot] = {}
        self._where: Optional[Expr] = None
        self._all = False

    def set(self, **values: Any) -> _Update:
        """Set columns; plain values become anonymous slots."""
        self._check_columns(values)
        return self._replace(_set={**self._set, **_slots(values)})

    def where(self, expr: Expr) -> _Update:
        """Filter rows; several calls are combined with AND."""
        return self._replace(_where=_cond(self, expr))

    def all_rows(self) -> _Update:
        """Allow the update to touch every row."""
        return self._replace(_all=True)

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `UPDATE` statement."""
        if not self._set:
            raise KorbaseError("update needs .set(...)")
        if self._where is None and not self._all:
            raise KorbaseError("update without where; call .all_rows() to confirm")
        sets = ", ".join(f"{quote_ident(k)} = {ctx.add(v)}" for k, v in self._set.items())
        return f"UPDATE {self._table()} SET {sets}" + _where(self, ctx)


class _Delete(_Query):
    """A `DELETE` with a mandatory filter or `.all_rows()`."""

    def __init__(self, model: type) -> None:
        """Start with no filter."""
        super().__init__(model)
        self._where: Optional[Expr] = None
        self._all = False

    def where(self, expr: Expr) -> _Delete:
        """Filter rows; several calls are combined with AND."""
        return self._replace(_where=_cond(self, expr))

    def all_rows(self) -> _Delete:
        """Allow the delete to touch every row."""
        return self._replace(_all=True)

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `DELETE` statement."""
        if self._where is None and not self._all:
            raise KorbaseError("delete without where; call .all_rows() to confirm")
        return f"DELETE FROM {self._table()}" + _where(self, ctx)


class _Upsert(_Insert):
    """An `INSERT ... ON CONFLICT ... DO UPDATE`."""

    def __init__(self, model: type) -> None:
        """Start with no conflict columns."""
        super().__init__(model)
        self._conflict: tuple[str, ...] = ()

    def on_conflict(self, *columns: str) -> _Upsert:
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

    def values(self, **values: Any) -> _Upsert:
        """Set the columns to insert; plain values become anonymous slots."""
        self._check_columns(values)
        return self._replace(_values=_slots(values))

    def returning(self) -> _Upsert:
        """Return the written row with all its columns."""
        return self._replace(_ret=True)


def select(model: type) -> _Select:
    """Start a `SELECT` over all columns of `model`."""
    return _Select(model)


def insert(model: type) -> _Insert:
    """Start an `INSERT` into the table of `model`."""
    return _Insert(model)


def update(model: type) -> _Update:
    """Start an `UPDATE` of the table of `model`."""
    return _Update(model)


def delete(model: type) -> _Delete:
    """Start a `DELETE` from the table of `model`."""
    return _Delete(model)


def upsert(model: type) -> _Upsert:
    """Start an `INSERT ... ON CONFLICT DO UPDATE` into the table of `model`."""
    return _Upsert(model)
