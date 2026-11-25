"""Compile tests for arithmetic in the builders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from korbase._dialect import PG, SQLITE
from korbase._expr import cols, param
from korbase._ident import KorbaseError
from korbase._meta import PK, table
from korbase._query import insert, select, update, upsert


@table("accounts")
@dataclass
class Account:
    id: Annotated[int, PK()]
    balance: int
    qty: int


@table("others")
@dataclass
class Other:
    id: Annotated[int, PK()]
    amount: int


A: Any = cols(Account)
O: Any = cols(Other)  # noqa: E741
DIALECTS = [PG, SQLITE]


def ph(dialect: Any, n: int) -> str:
    return f"${n}" if dialect is PG else f"?{n}"


@pytest.mark.parametrize("d", DIALECTS)
def test_set_arith(d: Any) -> None:
    c = update(Account).set(balance=A.balance - 10).where(id=1).compile(d)
    assert c.sql.startswith(f'UPDATE "accounts" SET "balance" = ("accounts"."balance" - {ph(d, 1)}) WHERE')
    assert [s.value for s in c.slots] == [10, 1]


@pytest.mark.parametrize("d", DIALECTS)
def test_set_arith_param_returning(d: Any) -> None:
    q = update(Account).set(balance=A.balance + 5).where(id=param("i")).returning()
    c = q.compile(d)
    assert [getattr(s, "name", None) for s in c.slots] == [None, "i"]
    assert c.sql.endswith("RETURNING *") or "RETURNING" in c.sql


def test_set_column() -> None:
    assert '"qty" = "accounts"."balance"' in update(Account).set(qty=A.balance).all_rows().compile().sql


def test_set_other_table_refused() -> None:
    with pytest.raises(KorbaseError):
        update(Account).set(balance=O.amount + 1)
    with pytest.raises(KorbaseError):
        update(Account).set(balance=O.amount)


def test_insert_refuses_expr() -> None:
    for q in (insert(Account), upsert(Account)):
        with pytest.raises(KorbaseError, match="plain values or param"):
            q.values(balance=A.qty + 1)
        with pytest.raises(KorbaseError, match="plain values or param"):
            q.rows([{"balance": A.qty}])


@pytest.mark.parametrize("d", DIALECTS)
def test_where_comparison(d: Any) -> None:
    c = select(Account).where(A.balance + A.qty > 3).compile(d)
    assert f'WHERE ("accounts"."balance" + "accounts"."qty") > {ph(d, 1)}' in c.sql


def test_where_bare_arith_refused() -> None:
    with pytest.raises(KorbaseError):
        select(Account).where(A.balance + 1)


@pytest.mark.parametrize("d", DIALECTS)
def test_columns_arith(d: Any) -> None:
    c = select(Account).columns(A.id, A.qty * A.balance).compile(d)
    assert c.sql.startswith('SELECT "accounts"."id", ("accounts"."qty" * "accounts"."balance") FROM')
    assert c.outputs[1] is None


@pytest.mark.parametrize("d", DIALECTS)
def test_order_by_arith_desc(d: Any) -> None:
    sql = select(Account).order_by((A.balance - A.qty).desc()).compile(d).sql
    assert sql.endswith('ORDER BY ("accounts"."balance" - "accounts"."qty") DESC')
    sql = select(Account).columns(A.id).order_by(A.balance * 2).compile(d).sql
    assert "ORDER BY" in sql


def test_set_wider_result_refused() -> None:
    """A float result cannot be stored in an int column."""
    with pytest.raises(KorbaseError):
        update(Account).set(balance=A.balance * 1.5)


def test_set_param_arith_accepted() -> None:
    """A param takes the column's type, so the expression type is known."""
    update(Account).set(balance=A.balance * param("rate"))


def test_set_order_refused() -> None:
    """An ordering is not a value."""
    with pytest.raises(KorbaseError):
        update(Account).set(balance=A.balance.asc())


def test_columns_arith_needs_group_by() -> None:
    """A column read inside arithmetic must be grouped like a plain column."""
    from korbase._expr import count

    with pytest.raises(KorbaseError):
        select(Account).columns(A.balance * 2, count()).compile(PG)


@table("items")
@dataclass
class Item:
    id: Annotated[int, PK()]
    n: int
    f: float


I: Any = cols(Item)  # noqa: E741


def test_param_takes_column_type() -> None:
    assert (I.n + param("n")).py_type is int
    assert (param("n") * I.f).py_type is float
    update(Item).set(n=I.n + param("n"))


def test_param_cast_on_postgres_float_column() -> None:
    assert "$1::float8" in select(Item).columns(I.f * param("k")).compile(PG).sql
    assert "::" not in select(Item).columns(I.n * param("k")).compile(PG).sql


def test_param_allowed_types_recorded() -> None:
    assert select(Item).columns(I.n * param("k")).compile(PG).arith == (("k", (int,)),)
    assert select(Item).columns(I.f * param("k")).compile(SQLITE).arith == (("k", (int, float)),)
