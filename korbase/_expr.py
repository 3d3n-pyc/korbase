"""Expressions: columns, params, operators and their compilation to SQL."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, NoReturn, TypeVar, Union

from korbase._dialect import PG, Dialect
from korbase._ident import KorbaseError, quote_ident, validate_ident
from korbase._meta import RelMeta, TableMeta, table_meta

__all__ = [
    "Expr",
    "Column",
    "Param",
    "Value",
    "BinOp",
    "IsNull",
    "InAny",
    "And",
    "Or",
    "Not",
    "Order",
    "RelRef",
    "Cols",
    "ParamCollector",
    "param",
    "cols",
    "compile_expr",
]

T = TypeVar("T")
M = TypeVar("M")


class Param:
    """A named query parameter, filled in when the query runs.

    `eq` is set on the copy a compiled query keeps when the parameter is
    compared with `==`, `!=` or `.in_()`; such a parameter cannot be `None`.
    """

    __slots__ = ("name", "eq")

    def __init__(self, name: str, eq: bool = False) -> None:
        """Create a parameter; `name` must be a valid identifier."""
        self.name = validate_ident(name)
        self.eq = eq


class Value:
    """An anonymous value. It is always sent as `$n`, never inlined."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        """Wrap a Python value."""
        self.value = value


def param(name: str) -> Param:
    """Declare a named parameter to pass when the query runs."""
    return Param(name)


class ParamCollector:
    """Collects parameters in order and hands out numbered placeholders."""

    def __init__(self, dialect: Dialect = PG) -> None:
        """Start with no parameters; placeholders follow `dialect`."""
        self.dialect = dialect
        self.items: list[Union[Param, Value]] = []
        self._named: dict[str, int] = {}

    def add(self, item: Union[Param, Value], eq: bool = False) -> str:
        """Register `item` and return its placeholder.

        The same named parameter always gets the same placeholder. `eq` says
        the parameter is compared with `==`, `!=` or `.in_()`; it is recorded
        on the stored slot.
        """
        if isinstance(item, Param):
            known = self._named.get(item.name)
            if known is not None:
                if eq and not self.items[known - 1].eq:  # type: ignore[union-attr]
                    self.items[known - 1] = Param(item.name, True)
                return self.dialect.placeholder(known)
            self.items.append(Param(item.name, True) if eq else item)
            self._named[item.name] = len(self.items)
        else:
            self.items.append(item)
        return self.dialect.placeholder(len(self.items))


def _operand(value: Any) -> Any:
    """Return `value` if it is an expression or param, else wrap it."""
    if isinstance(value, (Expr, Param, Value)):
        return value
    return Value(value)


class Expr:
    """Base of every SQL expression."""

    __slots__ = ()

    def compile(self, ctx: ParamCollector) -> str:
        """Return the SQL text, adding parameters to `ctx`."""
        raise NotImplementedError

    def __and__(self, other: Any) -> And:
        """`a & b` is SQL AND."""
        return And(self, other)

    def __or__(self, other: Any) -> Or:
        """`a | b` is SQL OR."""
        return Or(self, other)

    def __invert__(self) -> Not:
        """`~a` is SQL NOT."""
        return Not(self)

    def __bool__(self) -> NoReturn:
        """Refuse `and`, `or`, `not` and `if`: they cannot build SQL."""
        raise TypeError("use &, | and ~ instead of and, or, not to combine expressions")


def _sql(node: Any, ctx: ParamCollector, eq: bool = False) -> str:
    """Compile an operand: an expression, a param or a plain value."""
    if isinstance(node, Expr):
        return node.compile(ctx)
    return ctx.add(node if isinstance(node, (Param, Value)) else Value(node), eq)


class Order:
    """A column with a sort direction."""

    __slots__ = ("column", "desc")

    def __init__(self, column: Column[Any], desc: bool) -> None:
        """Sort by `column`, descending if `desc`."""
        self.column = column
        self.desc = desc

    def compile(self, ctx: ParamCollector) -> str:
        """Return `"t"."c" ASC` or `... DESC`."""
        return f"{self.column.compile(ctx)} {'DESC' if self.desc else 'ASC'}"


class Column(Expr, Generic[T]):
    """A column of a table, usable in comparisons."""

    __slots__ = ("table", "name")

    def __init__(self, table: TableMeta, name: str) -> None:
        """Bind to `table` and the column `name`."""
        self.table = table
        self.name = name

    def compile(self, ctx: ParamCollector) -> str:
        """Return `"table"."column"`."""
        return f"{quote_ident(self.table.name)}.{quote_ident(self.name)}"

    def __hash__(self) -> int:
        """Hash by table and name, so columns work as dict keys."""
        return hash((self.table.name, self.name))

    def __eq__(self, other: T | Column[T] | Param | None) -> Expr:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """`col == x` is SQL `=`; `col == None` is `IS NULL`.

        Only a literal `None` is rewritten. A `param(...)` always uses `=`,
        which never matches NULL: use `.is_null()` to test for NULL.
        """
        if other is None:
            return IsNull(self, negate=False)
        return BinOp("=", self, other)

    def __ne__(self, other: T | Column[T] | Param | None) -> Expr:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """`col != x` is SQL `<>`; `col != None` is `IS NOT NULL`."""
        if other is None:
            return IsNull(self, negate=True)
        return BinOp("<>", self, other)

    def __lt__(self, other: T | Column[T] | Param) -> BinOp:
        """`col < x`."""
        return BinOp("<", self, other)

    def __le__(self, other: T | Column[T] | Param) -> BinOp:
        """`col <= x`."""
        return BinOp("<=", self, other)

    def __gt__(self, other: T | Column[T] | Param) -> BinOp:
        """`col > x`."""
        return BinOp(">", self, other)

    def __ge__(self, other: T | Column[T] | Param) -> BinOp:
        """`col >= x`."""
        return BinOp(">=", self, other)

    def like(self: Column[str], other: str | Param) -> BinOp:
        """SQL `LIKE`."""
        return BinOp("LIKE", self, other)

    def ilike(self: Column[str], other: str | Param) -> BinOp:
        """SQL `ILIKE` (case-insensitive)."""
        return BinOp("ILIKE", self, other)

    def is_null(self) -> IsNull:
        """SQL `IS NULL`."""
        return IsNull(self, negate=False)

    def is_not_null(self) -> IsNull:
        """SQL `IS NOT NULL`."""
        return IsNull(self, negate=True)

    def in_(self, values: Sequence[T] | Param) -> InAny:
        """SQL `= ANY($n)`; pass a `param(...)` or a list of values."""
        return InAny(self, values)

    def asc(self) -> Order:
        """Sort ascending."""
        return Order(self, False)

    def desc(self) -> Order:
        """Sort descending."""
        return Order(self, True)


class BinOp(Expr):
    """A binary operator such as `=` or `LIKE`."""

    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: Any, right: Any) -> None:
        """Build `left op right`."""
        self.op = op
        self.left = left
        self.right = right

    def compile(self, ctx: ParamCollector) -> str:
        """Return `left op right`, right side as a placeholder."""
        eq = self.op in ("=", "<>")
        op = self.op
        if op in ("LIKE", "ILIKE"):
            op = ctx.dialect.like_op(op == "ILIKE")
        return f"{_sql(self.left, ctx, eq)} {op} {_sql(self.right, ctx, eq)}"


class IsNull(Expr):
    """`IS NULL` or `IS NOT NULL`."""

    __slots__ = ("column", "negate")

    def __init__(self, column: Column[Any], negate: bool) -> None:
        """Test `column` for null."""
        self.column = column
        self.negate = negate

    def compile(self, ctx: ParamCollector) -> str:
        """Return `col IS [NOT] NULL`."""
        return f"{self.column.compile(ctx)} IS {'NOT ' if self.negate else ''}NULL"


class InAny(Expr):
    """`col = ANY($n)`, the array form of `IN`."""

    __slots__ = ("column", "values")

    def __init__(self, column: Column[Any], values: Any) -> None:
        """Match `column` against a param or a list of values."""
        self.column = column
        self.values = values if isinstance(values, Param) else Value(list(values))

    def compile(self, ctx: ParamCollector) -> str:
        """Return `col = ANY($n)` (Postgres) or the `json_each` form (SQLite)."""
        return ctx.dialect.any_of(self.column.compile(ctx), ctx.add(self.values, True))


class And(Expr):
    """Both sides must hold."""

    __slots__ = ("left", "right")

    def __init__(self, left: Any, right: Any) -> None:
        """Build `left AND right`."""
        self.left = left
        self.right = right

    def compile(self, ctx: ParamCollector) -> str:
        """Return `(left AND right)`."""
        return f"({_sql(self.left, ctx)} AND {_sql(self.right, ctx)})"


class Or(Expr):
    """At least one side must hold."""

    __slots__ = ("left", "right")

    def __init__(self, left: Any, right: Any) -> None:
        """Build `left OR right`."""
        self.left = left
        self.right = right

    def compile(self, ctx: ParamCollector) -> str:
        """Return `(left OR right)`."""
        return f"({_sql(self.left, ctx)} OR {_sql(self.right, ctx)})"


class Not(Expr):
    """Negates an expression."""

    __slots__ = ("inner",)

    def __init__(self, inner: Any) -> None:
        """Build `NOT inner`."""
        self.inner = inner

    def compile(self, ctx: ParamCollector) -> str:
        """Return `(NOT inner)`."""
        return f"(NOT {_sql(self.inner, ctx)})"


class RelRef(Generic[M]):
    """A reference to a relation field, used by `.load`."""

    __slots__ = ("table", "rel")

    def __init__(self, table: TableMeta, rel: RelMeta) -> None:
        """Bind to `table` and its relation `rel`."""
        self.table = table
        self.rel = rel


class Cols(Generic[T]):
    """Attribute access to a model's columns: `cols(User).name`."""

    def __init__(self, meta: TableMeta) -> None:
        """Index the columns and relations of `meta`."""
        self._meta = meta
        self._items: dict[str, Union[Column[Any], RelRef[Any]]] = {}
        for c in meta.columns:
            self._items[c.name] = Column(meta, c.name)
        for r in meta.relations:
            self._items[r.name] = RelRef(meta, r)

    def __getattr__(self, name: str) -> Any:
        """Return the column or relation `name`, or raise `KorbaseError`."""
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._items[name]
        except KeyError:
            raise KorbaseError(f"table {self._meta.name!r} has no column {name!r}") from None


_COLS: dict[type, Cols[Any]] = {}


def cols(model: type) -> Cols[Any]:
    """Return the `Cols` of a `@table` model (cached per model)."""
    found = _COLS.get(model)
    if found is None:
        found = _COLS[model] = Cols(table_meta(model))
    return found


def compile_expr(expr: Any, ctx: ParamCollector) -> str:
    """Compile an expression, order or operand to SQL, collecting params."""
    if isinstance(expr, Order):
        return expr.compile(ctx)
    return _sql(expr, ctx)
