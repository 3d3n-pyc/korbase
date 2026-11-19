"""Tests for the expression layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from korbase._expr import Column, ParamCollector, cols, compile_expr, param
from korbase._ident import KorbaseError
from korbase._meta import PK, table


@table("users")
@dataclass
class User:
    id: Annotated[int, PK()]
    name: str
    age: int | None = None


U: Any = cols(User)


def sql(expr: Any) -> tuple[str, list[Any]]:
    ctx = ParamCollector()
    text = compile_expr(expr, ctx)
    return text, [getattr(i, "name", None) or getattr(i, "value", None) for i in ctx.items]


@pytest.mark.parametrize(
    ("expr", "text"),
    [
        (U.id == 1, '"users"."id" = $1'),
        (U.id != 1, '"users"."id" <> $1'),
        (U.id < 1, '"users"."id" < $1'),
        (U.id <= 1, '"users"."id" <= $1'),
        (U.id > 1, '"users"."id" > $1'),
        (U.id >= 1, '"users"."id" >= $1'),
        (U.name.like("a%"), '"users"."name" LIKE $1'),
        (U.name.ilike("a%"), '"users"."name" ILIKE $1'),
        (U.age.is_null(), '"users"."age" IS NULL'),
        (U.age.is_not_null(), '"users"."age" IS NOT NULL'),
        (U.id.in_([1, 2]), '"users"."id" = ANY($1)'),
        (U.id.in_(param("ids")), '"users"."id" = ANY($1)'),
    ],
)
def test_operators(expr: Any, text: str) -> None:
    assert sql(expr)[0] == text


def test_in_values_are_one_array() -> None:
    assert sql(U.id.in_([1, 2]))[1] == [[1, 2]]


def test_logic() -> None:
    expr = ((U.id == 1) & (U.name == "a")) | ~(U.age > 3)
    text, values = sql(expr)
    assert text == '(("users"."id" = $1 AND "users"."name" = $2) OR (NOT "users"."age" > $3))'
    assert values == [1, "a", 3]


def test_param_reuse_and_order() -> None:
    expr = (U.id == param("x")) & (U.age > 5) & (U.age < param("x"))
    text, values = sql(expr)
    assert text.count("$1") == 2 and "$2" in text and "$3" not in text
    assert values == ["x", 5]


def test_values_never_inlined() -> None:
    evil = "x'; drop table"
    text, values = sql(U.name == evil)
    assert "drop" not in text and "x'" not in text
    assert values == [evil]


def test_and_or_not_misuse() -> None:
    with pytest.raises(TypeError, match="&"):
        _ = (U.id == 1) and (U.id == 2)
    with pytest.raises(TypeError):
        _ = (U.id == 1) or (U.id == 2)
    with pytest.raises(TypeError):
        _ = not (U.id > 1)  # noqa: SIM201


def test_unknown_column() -> None:
    with pytest.raises(KorbaseError, match="nope"):
        _ = U.nope


def test_bad_param_name() -> None:
    with pytest.raises(KorbaseError):
        param("a; b")


def test_hashable_and_cached() -> None:
    col: Column[Any] = U.id
    assert {col: 1}[U.id] == 1
    assert cols(User) is cols(User)
    assert hash(U.id) == hash(Column(U._meta, "id"))


def test_order() -> None:
    assert sql(U.id.asc())[0] == '"users"."id" ASC'
    assert sql(U.name.desc())[0] == '"users"."name" DESC'
