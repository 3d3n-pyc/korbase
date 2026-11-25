"""Expressions: columns, params, operators and their compilation to SQL."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Generic, NoReturn, TypeVar, Union, overload

from korbase._dialect import PG, Dialect, _strip_optional
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
    "Aggregate",
    "Arith",
    "RelRef",
    "Cols",
    "ParamCollector",
    "param",
    "count",
    "cols",
    "compile_expr",
]

T = TypeVar("T")
M = TypeVar("M")
T_co = TypeVar("T_co", covariant=True)
N1 = TypeVar("N1", bound=None)  # carries a `None` (Optional) from the left operand
N2 = TypeVar("N2", bound=None)  # carries a `None` (Optional) from the right operand


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
    """A column or aggregate with a sort direction."""

    __slots__ = ("column", "desc")

    def __init__(self, column: Expr, desc: bool) -> None:
        """Sort by `column`, descending if `desc`."""
        self.column = column
        self.desc = desc

    def compile(self, ctx: ParamCollector) -> str:
        """Return `"t"."c" ASC` or `... DESC`."""
        return f"{self.column.compile(ctx)} {'DESC' if self.desc else 'ASC'}"


_ARITH_OPS = ("+", "-", "*", "/", "%")
_NUMBERS = (int, float, Decimal)
_SQLITE_DECIMAL = "arithmetic on Decimal columns is not supported on SQLite, which stores them as text"


def _numeric_type(column: Column[Any]) -> Any:
    """Return the column's Python type (int, float or Decimal); refuse any other.

    Returns `None` when the table has no such column. `Optional` is unwrapped.
    """
    for meta in column.table.columns:
        if meta.name == column.name:
            tp = _strip_optional(meta.python_type)
            if tp not in _NUMBERS:
                name = getattr(tp, "__name__", str(tp))
                raise KorbaseError(f"column {column.name!r} is {name}, not a number: it cannot be used in arithmetic")
            return tp
    return None


def _operand_type(node: Any) -> Any:
    """Return the number type of an arithmetic operand, `None` when unknown."""
    if isinstance(node, Column):
        return _numeric_type(node)
    if isinstance(node, Arith):
        return node.py_type
    if isinstance(node, Expr):
        raise KorbaseError(f"{type(node).__name__} cannot be used in arithmetic")
    return type(node) if type(node) in _NUMBERS else None


def _result_type(op: str, left: Any, right: Any) -> Any:
    """Return the Python type of `left op right`, `None` when unknown.

    `%` accepts integers only. A float anywhere gives a float, then a Decimal.
    """
    if op == "%" and (float in (left, right) or Decimal in (left, right)):
        raise KorbaseError("% only works on integers, not floats or Decimals")
    if left is None or right is None:
        return None
    return float if float in (left, right) else Decimal if Decimal in (left, right) else int


def _arith_sql(node: Any, ctx: ParamCollector) -> str:
    """Compile an arithmetic operand: values go through the dialect, never inline."""
    if isinstance(node, Column) and ctx.dialect.name == "sqlite" and _numeric_type(node) is Decimal:
        raise KorbaseError(_SQLITE_DECIMAL)
    if isinstance(node, Expr):
        return node.compile(ctx)
    if isinstance(node, Param):
        return ctx.add(node)
    return ctx.dialect.arith_operand(ctx.add(Value(node)), node)


def _forward(op: str) -> Any:
    """Build the method for `self op other`."""

    def method(self: Any, other: Any) -> Arith[Any]:
        """`self op other`."""
        return Arith(op, self, other)

    return method


def _reverse(op: str) -> Any:
    """Build the method for `other op self`."""

    def method(self: Any, other: Any) -> Arith[Any]:
        """`other op self`."""
        return Arith(op, other, self)

    return method


if TYPE_CHECKING:
    # Typing scheme. The operators are declared once, on `+`, and reused for
    # `-`, `*` and `/`. Overloads, tried in order: int with int gives int; a
    # float on either side gives float; a Decimal with int or Decimal gives
    # Decimal. `N1`/`N2` carry `None` from an Optional column to the result.
    # `%` is int only; str and other combinations match nothing (type error).

    class _ArithOps(Generic[T_co]):
        @overload
        def __add__(self: _ArithOps[int | N1], other: int | Param | _ArithOps[int | N2]) -> Arith[int | N1 | N2]: ...  # pyright: ignore[reportOverlappingOverload]
        @overload
        def __add__(
            self: _ArithOps[float | N1], other: float | Param | _ArithOps[float | N2]
        ) -> Arith[float | N1 | N2]: ...
        @overload
        def __add__(
            self: _ArithOps[Decimal | N1], other: int | Decimal | Param | _ArithOps[int | Decimal | N2]
        ) -> Arith[Decimal | N1 | N2]: ...
        @overload
        def __add__(
            self: _ArithOps[int | N1], other: Decimal | _ArithOps[Decimal | N2]
        ) -> Arith[Decimal | N1 | N2]: ...
        def __add__(self, other: Any) -> Any: ...

        __sub__ = __mul__ = __truediv__ = __add__

        @overload
        def __radd__(self: _ArithOps[int | N1], other: int) -> Arith[int | N1]: ...  # pyright: ignore[reportOverlappingOverload]
        @overload
        def __radd__(self: _ArithOps[float | N1], other: float) -> Arith[float | N1]: ...
        @overload
        def __radd__(self: _ArithOps[Decimal | N1], other: int | Decimal) -> Arith[Decimal | N1]: ...
        @overload
        def __radd__(self: _ArithOps[int | N1], other: Decimal) -> Arith[Decimal | N1]: ...
        def __radd__(self, other: Any) -> Any: ...

        __rsub__ = __rmul__ = __rtruediv__ = __radd__

        def __mod__(self: _ArithOps[int | N1], other: int | Param | _ArithOps[int | N2]) -> Arith[int | N1 | N2]: ...
        def __rmod__(self: _ArithOps[int | N1], other: int) -> Arith[int | N1]: ...

else:

    class _ArithOps(Generic[T_co]):
        """The arithmetic operators `+ - * / %` and their reversed forms."""

        __slots__ = ()
        __add__ = _forward("+")
        __sub__ = _forward("-")
        __mul__ = _forward("*")
        __truediv__ = _forward("/")
        __mod__ = _forward("%")
        __radd__ = _reverse("+")
        __rsub__ = _reverse("-")
        __rmul__ = _reverse("*")
        __rtruediv__ = _reverse("/")
        __rmod__ = _reverse("%")


class Column(Expr, _ArithOps[T]):
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

    def __eq__(self, other: T | Column[T] | Arith[T] | Param | None) -> Expr:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """`col == x` is SQL `=`; `col == None` is `IS NULL`.

        Only a literal `None` is rewritten. A `param(...)` always uses `=`,
        which never matches NULL: use `.is_null()` to test for NULL.
        """
        if other is None:
            return IsNull(self, negate=False)
        return BinOp("=", self, other)

    def __ne__(self, other: T | Column[T] | Arith[T] | Param | None) -> Expr:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """`col != x` is SQL `<>`; `col != None` is `IS NOT NULL`."""
        if other is None:
            return IsNull(self, negate=True)
        return BinOp("<>", self, other)

    def __lt__(self, other: T | Column[T] | Arith[T] | Param) -> BinOp:
        """`col < x`."""
        return BinOp("<", self, other)

    def __le__(self, other: T | Column[T] | Arith[T] | Param) -> BinOp:
        """`col <= x`."""
        return BinOp("<=", self, other)

    def __gt__(self, other: T | Column[T] | Arith[T] | Param) -> BinOp:
        """`col > x`."""
        return BinOp(">", self, other)

    def __ge__(self, other: T | Column[T] | Arith[T] | Param) -> BinOp:
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

    def count(self) -> Aggregate[int]:
        """SQL `count(col)`: the number of rows where the column is not null."""
        return Aggregate("count", self)

    def sum(self) -> Aggregate[T | None]:
        """SQL `sum(col)`; `None` when there are no rows to add."""
        return Aggregate("sum", self)

    def avg(self) -> Aggregate[float | None]:
        """SQL `avg(col)` as a float; `None` when there are no rows."""
        return Aggregate("avg", self)

    def min(self) -> Aggregate[T | None]:
        """SQL `min(col)`; `None` when there are no rows."""
        return Aggregate("min", self)

    def max(self) -> Aggregate[T | None]:
        """SQL `max(col)`; `None` when there are no rows."""
        return Aggregate("max", self)


class Aggregate(Expr, Generic[T]):
    """An aggregate such as `count(*)` or `sum(col)`.

    `kind` is `"count"`, `"sum"`, `"avg"`, `"min"` or `"max"`. `column` is the
    source column, or `None` for `count(*)`. It can be compared, to filter
    groups, and sorted with `.asc()` and `.desc()`.
    """

    __slots__ = ("kind", "column")
    __hash__ = Expr.__hash__

    def __init__(self, kind: str, column: Column[Any] | None) -> None:
        """Build the aggregate `kind` over `column` (`None` means `*`)."""
        self.kind = kind
        self.column = column

    def compile(self, ctx: ParamCollector) -> str:
        """Return `count(*)`, `sum("t"."c")`, ...; Postgres casts `avg` to float."""
        arg = "*" if self.column is None else self.column.compile(ctx)
        text = f"{self.kind}({arg})"
        if self.kind == "avg" and ctx.dialect.name == "postgres":
            return f"CAST({text} AS DOUBLE PRECISION)"
        return text

    def __eq__(self, other: T | Aggregate[T] | Param | None) -> Expr:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """`agg == x` is SQL `=`; `agg == None` is `IS NULL`."""
        if other is None:
            return IsNull(self, negate=False)
        return BinOp("=", self, other)

    def __ne__(self, other: T | Aggregate[T] | Param | None) -> Expr:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """`agg != x` is SQL `<>`; `agg != None` is `IS NOT NULL`."""
        if other is None:
            return IsNull(self, negate=True)
        return BinOp("<>", self, other)

    def __lt__(self, other: T | Aggregate[T] | Param) -> BinOp:
        """`agg < x`."""
        return BinOp("<", self, other)

    def __le__(self, other: T | Aggregate[T] | Param) -> BinOp:
        """`agg <= x`."""
        return BinOp("<=", self, other)

    def __gt__(self, other: T | Aggregate[T] | Param) -> BinOp:
        """`agg > x`."""
        return BinOp(">", self, other)

    def __ge__(self, other: T | Aggregate[T] | Param) -> BinOp:
        """`agg >= x`."""
        return BinOp(">=", self, other)

    def asc(self) -> Order:
        """Sort ascending."""
        return Order(self, False)

    def desc(self) -> Order:
        """Sort descending."""
        return Order(self, True)


class Arith(Expr, _ArithOps[T]):
    """An arithmetic expression such as `cols(Account).balance - 10`.

    It can be compared, sorted with `.asc()` and `.desc()`, and combined with
    more arithmetic. `columns` lists the columns it uses and `py_type` is the
    Python type of its result (`int`, `float` or `Decimal`), `None` if unknown.
    """

    __slots__ = ("op", "left", "right", "py_type")
    __hash__ = Expr.__hash__

    def __init__(self, op: str, left: Any, right: Any) -> None:
        """Build `left op right`, refusing operands that are not numbers."""
        if op not in _ARITH_OPS:
            raise KorbaseError(f"unknown arithmetic operator {op!r}")
        self.op = op
        self.left = left
        self.right = right
        self.py_type = _result_type(op, _operand_type(left), _operand_type(right))

    @property
    def columns(self) -> tuple[Column[Any], ...]:
        """The columns this expression reads, in order."""
        found: list[Column[Any]] = []
        for side in (self.left, self.right):
            if isinstance(side, Column):
                found.append(side)
            elif isinstance(side, Arith):
                found.extend(side.columns)
        return tuple(found)

    def compile(self, ctx: ParamCollector) -> str:
        """Return `(left op right)`, always parenthesised."""
        return f"({_arith_sql(self.left, ctx)} {self.op} {_arith_sql(self.right, ctx)})"

    def __eq__(self, other: T | Arith[T] | Param | None) -> Expr:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """`expr == x` is SQL `=`; `expr == None` is `IS NULL`."""
        if other is None:
            return IsNull(self, negate=False)
        return BinOp("=", self, other)

    def __ne__(self, other: T | Arith[T] | Param | None) -> Expr:  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        """`expr != x` is SQL `<>`; `expr != None` is `IS NOT NULL`."""
        if other is None:
            return IsNull(self, negate=True)
        return BinOp("<>", self, other)

    def __lt__(self, other: T | Arith[T] | Param) -> BinOp:
        """`expr < x`."""
        return BinOp("<", self, other)

    def __le__(self, other: T | Arith[T] | Param) -> BinOp:
        """`expr <= x`."""
        return BinOp("<=", self, other)

    def __gt__(self, other: T | Arith[T] | Param) -> BinOp:
        """`expr > x`."""
        return BinOp(">", self, other)

    def __ge__(self, other: T | Arith[T] | Param) -> BinOp:
        """`expr >= x`."""
        return BinOp(">=", self, other)

    def asc(self) -> Order:
        """Sort ascending."""
        return Order(self, False)

    def desc(self) -> Order:
        """Sort descending."""
        return Order(self, True)


def count() -> Aggregate[int]:
    """SQL `count(*)`: the number of rows in each group."""
    return Aggregate("count", None)


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

    def __init__(self, column: Expr, negate: bool) -> None:
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
