"""Tests for arithmetic expressions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Optional

import pytest

from korbase._dialect import SQLITE
from korbase._expr import Arith, ParamCollector, cols, compile_expr, param
from korbase._ident import KorbaseError
from korbase._meta import PK, table


@table("accounts")
@dataclass
class Account:
    id: Annotated[int, PK()]
    balance: int
    rate: float
    price: Decimal
    maybe: Optional[int]
    flag: bool
    name: str
    day: date
    ref: uuid.UUID


A: Any = cols(Account)


def sql(expr: Any, sqlite: bool = False) -> tuple[str, list[Any]]:
    ctx = ParamCollector(SQLITE) if sqlite else ParamCollector()
    text = compile_expr(expr, ctx)
    return text, [getattr(i, "name", None) or getattr(i, "value", None) for i in ctx.items]


@pytest.mark.parametrize("sqlite", [False, True])
def test_basic_ops(sqlite: bool) -> None:
    ph = "?1" if sqlite else "$1"
    assert sql(A.balance + A.id, sqlite) == ('("accounts"."balance" + "accounts"."id")', [])
    assert sql(A.balance - 5, sqlite) == (f'("accounts"."balance" - {ph})', [5])
    assert sql(10 - A.balance, sqlite) == (f'({ph} - "accounts"."balance")', [10])
    assert sql(A.balance * 2, sqlite)[0] == f'("accounts"."balance" * {ph})'
    assert sql(A.balance / 2, sqlite)[0] == f'("accounts"."balance" / {ph})'
    assert sql(A.balance % 2, sqlite)[0] == f'("accounts"."balance" % {ph})'
    assert sql(7 % A.balance, sqlite)[0] == f'({ph} % "accounts"."balance")'


def test_nested() -> None:
    expr = (A.balance + A.id) * 2
    assert sql(expr) == ('(("accounts"."balance" + "accounts"."id") * $1)', [2])
    assert isinstance(expr, Arith)
    assert [c.name for c in expr.columns] == ["balance", "id"]


def test_float_cast_on_postgres_only() -> None:
    assert sql(A.rate * 1.5) == ('("accounts"."rate" * $1::float8)', [1.5])
    assert sql(A.rate * 1.5, sqlite=True)[0] == '("accounts"."rate" * ?1)'
    assert sql(A.price + Decimal("1.5"))[0] == '("accounts"."price" + $1::numeric)'


def test_param_operand_cast() -> None:
    """A param next to a float column gets the float cast; next to an int column, none."""
    assert sql(A.rate - param("x")) == ('("accounts"."rate" - $1::float8)', ["x"])
    assert sql(A.balance - param("x")) == ('("accounts"."balance" - $1)', ["x"])


def test_comparison_and_order() -> None:
    assert sql(A.balance - 1 > 5) == ('("accounts"."balance" - $1) > $2', [1, 5])
    assert sql((A.balance + 1) == None)[0] == '("accounts"."balance" + $1) IS NULL'  # noqa: E711
    assert sql(A.id > A.balance + 1) == ('"accounts"."id" > ("accounts"."balance" + $1)', [1])
    assert sql((A.balance + 1).desc())[0] == '("accounts"."balance" + $1) DESC'
    assert sql((A.balance + 1).asc())[0] == '("accounts"."balance" + $1) ASC'


def test_decode_type() -> None:
    assert (A.balance + 1).py_type is int
    assert (A.maybe + 1).py_type is int
    assert (A.balance + 1.5).py_type is float
    assert (A.balance * A.rate).py_type is float
    assert (A.price + 1).py_type is Decimal
    assert (A.balance + param("x")).py_type is int  # a param takes the column's type


@pytest.mark.parametrize("name", ["flag", "name", "day", "ref"])
def test_refuses_non_numeric_column(name: str) -> None:
    with pytest.raises(KorbaseError, match="not a number"):
        getattr(A, name) + 1
    with pytest.raises(KorbaseError, match="not a number"):
        1 + getattr(A, name)


def test_refuses_modulo_on_floats_and_decimals() -> None:
    for build in (
        lambda: A.rate % 2,
        lambda: A.balance % 1.5,
        lambda: A.price % 2,
        lambda: A.balance % Decimal(2),
        lambda: 5 % A.rate,
    ):
        with pytest.raises(KorbaseError, match="only works on integers"):
            build()


def test_refuses_decimal_column_on_sqlite() -> None:
    with pytest.raises(KorbaseError, match="Decimal columns is not supported on SQLite"):
        sql(A.price + 1, sqlite=True)
    with pytest.raises(KorbaseError, match="Decimal columns is not supported on SQLite"):
        sql((A.price + 1) * 2, sqlite=True)
    assert sql(A.price + 1)[0] == '("accounts"."price" + $1)'


def test_refuses_bad_values_at_compile() -> None:
    for bad in (True, "x", None, b"x"):
        with pytest.raises(KorbaseError, match="arithmetic needs a number"):
            sql(A.balance + bad)
    with pytest.raises(KorbaseError, match="Decimal"):
        sql(A.balance + Decimal(1), sqlite=True)


def test_refuses_other_expressions() -> None:
    with pytest.raises(KorbaseError, match="cannot be used in arithmetic"):
        A.balance + A.balance.sum()
    with pytest.raises(KorbaseError, match="cannot be used in arithmetic"):
        A.balance + (A.balance > 1)


NOT_COND = "not a condition"


def test_arith_is_not_a_condition() -> None:
    """An arithmetic expression cannot be an operand of `&`, `|` or `~`."""
    for build in (
        lambda: (A.balance + 7) & (A.id > 0),
        lambda: (A.id > 0) | (A.balance + 1),
        lambda: ~(A.balance + 1),
    ):
        with pytest.raises(KorbaseError, match=NOT_COND):
            build()


def test_arith_not_a_condition_in_having() -> None:
    """`having` refuses the same combinations."""
    from korbase import count, select

    with pytest.raises(KorbaseError, match=NOT_COND):
        select(Account).columns(A.balance, count()).group_by(A.balance).having((A.balance + 1) & (count() > 0))


@table("wrapped")
@dataclass
class Wrapped:
    id: Annotated[int, PK()]
    uniq: Optional[Annotated[int, "meta"]]
    typed: Optional[Annotated[Decimal, "meta"]]
    flag: Optional[Annotated[bool, "meta"]]


def test_numeric_type_unwraps_annotated_and_newtype() -> None:
    """Optional[Annotated[...]] and NewType columns are numbers; bool stays refused."""
    w: Any = cols(Wrapped)
    assert (w.uniq + 1).py_type is int
    assert (w.typed + 1).py_type is Decimal
    with pytest.raises(KorbaseError, match="not a number"):
        w.flag + 1


def test_newtype_over_number() -> None:
    """A NewType over int is read through `__supertype__`."""
    from typing import NewType

    from korbase._expr import _unwrap_number

    assert _unwrap_number(NewType("Cents", int)) is int
    assert _unwrap_number(Optional[NewType("Money", Decimal)]) is Decimal


def test_decimal_with_float_refused() -> None:
    """A Decimal combined with a float raises, like Python does."""
    for build in (
        lambda: A.price + 1.5,
        lambda: 1.5 * A.price,
        lambda: A.price + A.rate,
        lambda: A.rate - Decimal(1),
        lambda: (A.rate + 1) * A.price,
    ):
        with pytest.raises(KorbaseError, match="Decimal"):
            build()
