"""Immutable query builders, compiled once to SQL and parameter slots."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Generic, Optional, TypeVar, Union, overload

from korbase._dialect import PG, Dialect
from korbase._expr import (
    Aggregate,
    Arith,
    Column,
    Expr,
    Order,
    Param,
    ParamCollector,
    RelRef,
    Value,
    _numeric_type,
    compile_expr,
)
from korbase._ident import KorbaseError, quote_ident
from korbase._join import Join, JoinPart, check_tables, from_sql, make_join, parts_of, select_names, table_names
from korbase._meta import TableMeta, table_meta

__all__ = [
    "Slot",
    "Compiled",
    "Select",
    "SelectColumns",
    "SelectJoin",
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
R = TypeVar("R")
X1 = TypeVar("X1")
X2 = TypeVar("X2")
X3 = TypeVar("X3")
X4 = TypeVar("X4")
X5 = TypeVar("X5")
X6 = TypeVar("X6")
JA = TypeVar("JA")
JB = TypeVar("JB")
JC = TypeVar("JC")

Slot = Union[Param, Value]
_MAX_PARAMS = 32766


@dataclass(frozen=True)
class Compiled:
    """SQL text plus the slots that fill its numbered placeholders (`$n` or `?n`), in order.

    `outputs` is set for column queries: one entry per selected item, the
    source column to decode the value with, or `None` for `count` and `avg`.
    `parts` is set for whole-row joins: one `JoinPart` per model in the row.
    """

    sql: str
    slots: tuple[Slot, ...]
    outputs: tuple[Optional[Column[Any]], ...] = ()
    parts: tuple[JoinPart, ...] = ()
    arith: tuple[tuple[str, tuple[type, ...]], ...] = ()


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
        return Compiled(sql, tuple(ctx.items), arith=tuple(ctx.arith.items()))

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
    if isinstance(expr, Arith):
        raise KorbaseError("an arithmetic expression is not a condition; compare it, e.g. `c.a + c.b > 3`")
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


def _plain_slots(values: dict[str, Any]) -> dict[str, Slot]:
    """Return `_slots(values)`, refusing columns and arithmetic (not allowed in inserts)."""
    if any(isinstance(v, Expr) for v in values.values()):
        raise KorbaseError("insert values must be plain values or param()")
    return _slots(values)


def _row_slots(rows: Any) -> list[dict[str, Slot]]:
    """Wrap each row's values as `Value`, all in the first row's key order."""
    if isinstance(rows, (Mapping, str, bytes)) or not isinstance(rows, Sequence):
        raise KorbaseError("rows() takes a list of dicts")
    if not all(isinstance(r, Mapping) for r in rows):
        raise KorbaseError("rows() takes a list of dicts")
    # One pass over every value: bulk inserts are the hot path here.
    if any(isinstance(v, (Param, Value, Expr)) for r in rows for v in r.values()):
        raise KorbaseError("rows() takes plain values, not param(), columns or arithmetic")
    if not rows:
        return []
    order = list(rows[0])
    return [_slots({k: r[k] for k in order if k in r} | {k: r[k] for k in r if k not in order}) for r in rows]


class _Ordered(_Filtered[M]):
    """A filtered `SELECT` with order and paging; can switch to chosen columns."""

    returns_rows = True

    def __init__(self, model: type[M]) -> None:
        """Start with no filter, order or paging."""
        super().__init__(model)
        self._orders: tuple[Order, ...] = ()
        self._limit: Optional[Slot] = None
        self._offset: Optional[Slot] = None
        self._joins: tuple[Join, ...] = ()

    def limit(self: _Q, value: Union[Param, int]) -> _Q:
        """Return at most `value` rows."""
        return self._replace(_limit=_paging(value))

    def offset(self: _Q, value: Union[Param, int]) -> _Q:
        """Skip the first `value` rows."""
        return self._replace(_offset=_paging(value))

    @overload
    def columns(self, a: _Item[X1], /) -> SelectColumns[tuple[X1]]: ...
    @overload
    def columns(self, a: _Item[X1], b: _Item[X2], /) -> SelectColumns[tuple[X1, X2]]: ...
    @overload
    def columns(self, a: _Item[X1], b: _Item[X2], c: _Item[X3], /) -> SelectColumns[tuple[X1, X2, X3]]: ...
    @overload
    def columns(
        self, a: _Item[X1], b: _Item[X2], c: _Item[X3], d: _Item[X4], /
    ) -> SelectColumns[tuple[X1, X2, X3, X4]]: ...
    @overload
    def columns(
        self, a: _Item[X1], b: _Item[X2], c: _Item[X3], d: _Item[X4], e: _Item[X5], /
    ) -> SelectColumns[tuple[X1, X2, X3, X4, X5]]: ...
    @overload
    def columns(
        self, a: _Item[X1], b: _Item[X2], c: _Item[X3], d: _Item[X4], e: _Item[X5], f: _Item[X6], /
    ) -> SelectColumns[tuple[X1, X2, X3, X4, X5, X6]]: ...
    def columns(self, *items: Any) -> Any:
        """Select chosen columns and aggregates instead of whole rows.

        Rows come back as tuples, in the order given. Calling it again
        replaces the list. Use `group_by` and `having` on the result.
        """
        for item in items:
            if not isinstance(item, (Column, Aggregate, Arith)):
                raise KorbaseError(f"columns() takes columns, aggregates and arithmetic, got {item!r}")
        new = self._become(SelectColumns)
        return new._replace(_columns=items, _group=(), _having=None)


_Item = Union[Column[X1], Aggregate[X1], Arith[X1]]


def _check_orders(orders: tuple[Any, ...], allowed: tuple[type, ...]) -> None:
    """Raise `TypeError` if an order is not one of the `allowed` types."""
    for o in orders:
        if not isinstance(o, allowed):
            raise TypeError(f"order_by() takes columns or .asc()/.desc(), got {type(o).__name__}")


class Select(_Ordered[M]):
    """A `SELECT` over all columns of one model; runs as rows."""

    def __init__(self, model: type[M]) -> None:
        """Start with no filter, order, paging or loads."""
        super().__init__(model)
        self._loads: tuple[RelRef, ...] = ()

    def order_by(self, *orders: Union[Column[Any], Arith[Any], Order]) -> Select[M]:
        """Sort rows, using `col.asc()` or `col.desc()`."""
        _check_orders(orders, (Column, Arith, Order))
        return self._replace(_orders=self._orders + tuple(orders))

    def load(self, *relations: RelRef) -> Select[M]:
        """Remember relations to load along with the rows."""
        return self._replace(_loads=self._loads + relations)

    def join(self, model: type[X1], on: Optional[Expr] = None) -> SelectJoin[tuple[M, X1]]:
        """Join another table (`JOIN`); rows become tuples of models.

        Without `on`, the one foreign key between the tables is used.
        """
        return _start_join(self, model, False, on)

    def left_join(self, model: type[X1], on: Optional[Expr] = None) -> SelectJoin[tuple[M, Optional[X1]]]:
        """Join another table (`LEFT JOIN`); its model is `None` when nothing matches."""
        return _start_join(self, model, True, on)

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `SELECT` statement."""
        t = self._table()
        names = ", ".join(f"{t}.{quote_ident(c.name)}" for c in self._meta.columns)
        return _finish(self, f"SELECT {names} FROM {t}", ctx)


def _start_join(query: Any, model: type[Any], left: bool, on: Any) -> Any:
    """Turn a `Select` into a `SelectJoin` with its first join."""
    if query._loads:
        raise KorbaseError("load() cannot be combined with a join")
    new = query._become(SelectJoin)
    return new._add_join(model, left, on)


class SelectJoin(_Ordered[Any], Generic[R]):
    """A `SELECT` over joined tables; runs as tuples of models of type `R`."""

    def order_by(self, *orders: Union[Column[Any], Arith[Any], Order]) -> SelectJoin[R]:
        """Sort rows, using `col.asc()` or `col.desc()`."""
        _check_orders(orders, (Column, Arith, Order))
        return self._replace(_orders=self._orders + tuple(orders))

    def _add_join(self, model: type[Any], left: bool, on: Any) -> Any:
        """Return a copy with one more join."""
        join = make_join(self._meta, self._joins, table_meta(model), left, on)
        return self._replace(_joins=self._joins + (join,))

    def join(
        self: SelectJoin[tuple[JA, JB]], model: type[JC], on: Optional[Expr] = None
    ) -> SelectJoin[tuple[JA, JB, JC]]:
        """Join a third table (`JOIN`); at most two joins are allowed."""
        return self._add_join(model, False, on)

    def left_join(
        self: SelectJoin[tuple[JA, JB]], model: type[JC], on: Optional[Expr] = None
    ) -> SelectJoin[tuple[JA, JB, Optional[JC]]]:
        """Join a third table (`LEFT JOIN`); at most two joins are allowed."""
        return self._add_join(model, True, on)

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `SELECT` statement."""
        check_query(self)
        sql = f"SELECT {select_names(self._meta, self._joins)} FROM {from_sql(self._meta, self._joins, ctx)}"
        return _finish(self, sql, ctx)

    def _compile_for(self, dialect: Dialect) -> Compiled:
        """Build the `Compiled` of this query, with the slice of each model."""
        done = super()._compile_for(dialect)
        return replace(done, parts=parts_of(self._meta, self._joins))


def check_query(query: Any) -> None:
    """Raise `KorbaseError` if filters or orders use a table that is not in the query."""
    nodes = [n for n in (query._where, getattr(query, "_having", None)) if n is not None]
    nodes += list(getattr(query, "_columns", ())) + list(query._orders)
    check_tables(nodes, table_names(query._meta, query._joins))


def _finish(query: Any, sql: str, ctx: ParamCollector, middle: Any = None) -> str:
    """Add WHERE, the `middle` clauses, ORDER BY, LIMIT and OFFSET to `sql`."""
    sql += _where(query, ctx)
    if middle is not None:
        sql += middle()
    if query._orders:
        sql += " ORDER BY " + ", ".join(compile_expr(o, ctx) for o in query._orders)
    if query._limit is not None:
        sql += f" LIMIT {ctx.add(query._limit)}"
    if query._offset is not None:
        sql += f" OFFSET {ctx.add(query._offset)}"
    return sql


class SelectColumns(_Ordered[Any], Generic[R]):
    """A `SELECT` of chosen columns and aggregates; runs as tuples of type `R`."""

    def __init__(self, model: type[Any]) -> None:
        """Start with no columns, grouping or `having` filter."""
        super().__init__(model)
        self._columns: tuple[Any, ...] = ()
        self._group: tuple[Column[Any], ...] = ()
        self._having: Optional[Expr] = None

    def order_by(self, *orders: Union[Column[Any], Aggregate[Any], Arith[Any], Order]) -> SelectColumns[R]:
        """Sort rows, using columns, aggregates, `.asc()` or `.desc()`."""
        _check_orders(orders, (Column, Aggregate, Arith, Order))
        return self._replace(_orders=self._orders + tuple(orders))

    def group_by(self, *columns: Column[Any]) -> SelectColumns[R]:
        """Group rows by these columns, one result row per group."""
        if not columns or not all(isinstance(c, Column) for c in columns):
            raise KorbaseError("group_by() takes one or more columns")
        return self._replace(_group=self._group + tuple(columns))

    def having(self, *conditions: Expr) -> SelectColumns[R]:
        """Keep only groups matching the conditions (ANDed with earlier calls)."""
        if not conditions:
            raise TypeError("having() needs at least one condition")
        having = self._having
        for expr in conditions:
            having = _cond(having, expr)
        return self._replace(_having=having)

    def _validate(self) -> None:
        """Raise `KorbaseError` if the columns, grouping and `having` do not fit together."""
        if not self._columns:
            raise KorbaseError("columns() needs at least one column or aggregate")
        known = table_names(self._meta, self._joins)
        for item in self._columns + self._group:
            for col in _used_columns(item):
                if col.table.name not in known:
                    raise KorbaseError(f"column {col.name!r} is not a column of {self._meta.name!r}")
        plain = [c for item in self._columns for c in _used_columns(item)]
        has_agg = any(isinstance(c, Aggregate) for c in self._columns)
        grouped = {(c.table.name, c.name) for c in self._group}
        if (self._group or has_agg) and any((c.table.name, c.name) not in grouped for c in plain):
            raise KorbaseError("a plain column must be listed in group_by() when mixed with aggregates")
        if self._having is not None and not self._group and not has_agg:
            raise KorbaseError("having() needs an aggregate or group_by()")

    def _group_sql(self, ctx: ParamCollector) -> str:
        """Return the ` GROUP BY ...` clause, or an empty string."""
        if not self._group:
            return ""
        return " GROUP BY " + ", ".join(c.compile(ctx) for c in self._group)

    def _having_sql(self, ctx: ParamCollector) -> str:
        """Return the ` HAVING ...` clause, or an empty string."""
        if self._having is None:
            return ""
        return " HAVING " + compile_expr(self._having, ctx)

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `SELECT` statement."""
        self._validate()
        check_query(self)
        names = ", ".join(compile_expr(c, ctx) for c in self._columns)
        sql = f"SELECT {names} FROM {from_sql(self._meta, self._joins, ctx)}"
        return _finish(self, sql, ctx, lambda: self._group_sql(ctx) + self._having_sql(ctx))

    def _compile_for(self, dialect: Dialect) -> Compiled:
        """Build the `Compiled` of this query, with the per-item decode columns."""
        done = super()._compile_for(dialect)
        return replace(done, outputs=tuple(_source(c) for c in self._columns))


def _used_columns(item: Any) -> tuple[Column[Any], ...]:
    """Return the columns a selected item reads: itself, those of arithmetic, or none."""
    if isinstance(item, Column):
        return (item,)
    return item.columns if isinstance(item, Arith) else ()


def _source(item: Any) -> Optional[Column[Any]]:
    """Return the column that decodes `item`'s value, or None for `count` and `avg`."""
    if isinstance(item, Column):
        return item
    if isinstance(item, Arith):
        return None
    return item.column if item.kind in ("sum", "min", "max") else None


class Insert(_Query[M]):
    """An `INSERT` of one row; always returns the row with all its columns."""

    returns_rows = True

    def __init__(self, model: type[M]) -> None:
        """Start with the default values."""
        super().__init__(model)
        self._values: Optional[dict[str, Slot]] = None
        self._rows: Optional[list[dict[str, Slot]]] = None

    def values(self: _Q, **values: Any) -> _Q:
        """Set the columns to insert; plain values become anonymous slots."""
        self._check_columns(values)  # type: ignore[attr-defined]
        return self._replace(_values=_plain_slots(values), _rows=None)

    def rows(self: _Q, rows: Sequence[Mapping[str, Any]]) -> _Q:
        """Insert many rows in one statement; each row is a mapping like `.values(...)`.

        Every row must have the same keys, and the rows come back in input order.
        Columns missing from every row use their default. The list must not be
        empty and may hold at most 32766 bound values; split larger lists into
        batches. Replaces earlier `.rows(...)` and `.values(...)`.
        """
        slots = _row_slots(rows)
        self._check_rows(slots)  # type: ignore[attr-defined]
        return self._replace(_rows=slots, _values=None)

    def _check_rows(self, rows: list[dict[str, Slot]]) -> None:
        """Raise `KorbaseError` if `rows` is empty, uneven, unknown or too large."""
        if not rows or not rows[0]:
            raise KorbaseError("rows needs at least one row with at least one column")
        keys = set(rows[0])
        if any(set(r) != keys for r in rows):
            raise KorbaseError("every row must have the same columns")
        self._check_columns(keys)
        if len(rows) * len(keys) > _MAX_PARAMS:
            raise KorbaseError(f"too many values (over {_MAX_PARAMS}); split the list into smaller batches")

    def _row(self) -> dict[str, Slot]:
        """Return the values to insert, defaulting to same-named params."""
        if self._rows is not None:
            return self._rows[0]
        if self._values is not None:
            return self._values
        return {c.name: Param(c.name) for c in self._meta.columns if c.default is None}

    def _build(self, ctx: ParamCollector) -> str:
        """Return the `INSERT` statement."""
        row = self._row()
        if not row:
            return f"INSERT INTO {self._table()} DEFAULT VALUES" + self._tail()
        names = ", ".join(quote_ident(k) for k in row)
        groups = ", ".join("(" + ", ".join(ctx.add(v) for v in r.values()) + ")" for r in self._rows or [row])
        sql = f"INSERT INTO {self._table()} ({names}) VALUES {groups}" + self._conflict_sql()
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
        for name, value in values.items():
            self._check_expr(value)
            if isinstance(value, Arith):
                self._check_arith_type(name, value)
        plain = _slots({k: v for k, v in values.items() if not isinstance(v, Expr)})
        return self._replace(_set={**self._set, **{k: plain.get(k, v) for k, v in values.items()}})

    def _check_expr(self, value: Any) -> None:
        """Refuse a column or arithmetic value that reads another table."""
        if isinstance(value, (Expr, Order)) and not isinstance(value, (Column, Arith)):
            raise KorbaseError("set() takes plain values, param(), columns or arithmetic")
        for col in _used_columns(value):
            if col.table.name != self._meta.name:
                raise KorbaseError(f"column {col.name!r} is not a column of {self._meta.name!r}")

    def _check_arith_type(self, name: str, value: Arith[Any]) -> None:
        """Refuse arithmetic whose result type is wider than column `name`."""
        target = _numeric_type(Column(self._meta, name))
        got = value.py_type
        if got is None:
            raise KorbaseError(f"cannot check the type of the expression set on {name!r}: use a matching expression")
        allowed = {int: (int,), float: (int, float), Decimal: (int, Decimal)}[target]
        if got not in allowed:
            raise KorbaseError(f"column {name!r} is {target.__name__}: it cannot be set to a {got.__name__} expression")

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
        sets = ", ".join(f"{quote_ident(k)} = {_set_sql(v, ctx)}" for k, v in self._set.items())
        return f"UPDATE {self._table()} SET {sets}" + _where(self, ctx) + self._tail()


def _set_sql(value: Any, ctx: ParamCollector) -> str:
    """Return the right side of one `SET` assignment."""
    return compile_expr(value, ctx) if isinstance(value, Expr) else ctx.add(value)


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

    def _check_repeats(self) -> None:
        """Raise `KorbaseError` if two rows hold the same conflict-key values."""
        seen: set[Any] = set()
        for r in self._rows or ():
            slots = [r.get(c) for c in self._conflict]
            if not all(isinstance(v, Value) for v in slots):
                return
            try:
                key = tuple(v.value for v in slots)  # type: ignore[union-attr]
                if key in seen:
                    raise KorbaseError(f"two rows have the same {', '.join(self._conflict)} value")
                seen.add(key)
            except TypeError:
                return

    def _conflict_sql(self) -> str:
        """Return the `ON CONFLICT ... DO UPDATE` clause."""
        if not self._conflict:
            raise KorbaseError("upsert needs .on_conflict(...)")
        self._check_repeats()
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
