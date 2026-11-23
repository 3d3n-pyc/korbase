"""Tests for aggregate expressions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from korbase._dialect import SQLITE
from korbase._expr import ParamCollector, cols, compile_expr, count, param
from korbase._meta import PK, table


@table("items")
@dataclass
class Item:
    id: Annotated[int, PK()]
    price: int


I: Any = cols(Item)  # noqa: E741


def sql(expr: Any, sqlite: bool = False) -> tuple[str, list[Any]]:
    ctx = ParamCollector(SQLITE) if sqlite else ParamCollector()
    text = compile_expr(expr, ctx)
    return text, [getattr(i, "name", None) or getattr(i, "value", None) for i in ctx.items]


@pytest.mark.parametrize(
    ("expr", "text"),
    [
        (count(), "count(*)"),
        (I.price.count(), 'count("items"."price")'),
        (I.price.sum(), 'sum("items"."price")'),
        (I.price.min(), 'min("items"."price")'),
        (I.price.max(), 'max("items"."price")'),
        (I.price.avg(), 'CAST(avg("items"."price") AS DOUBLE PRECISION)'),
        (count() > 2, "count(*) > $1"),
        (count() == param("n"), "count(*) = $1"),
        (I.price.sum() <= 9, 'sum("items"."price") <= $1'),
        (I.price.sum() != None, 'sum("items"."price") IS NOT NULL'),  # noqa: E711
        (count().desc(), "count(*) DESC"),
        (I.price.max().asc(), 'max("items"."price") ASC'),
    ],
)
def test_postgres(expr: Any, text: str) -> None:
    assert sql(expr)[0] == text


def test_sqlite_avg_and_placeholder() -> None:
    assert sql(I.price.avg(), True)[0] == 'avg("items"."price")'
    assert sql(count() >= 3, True) == ("count(*) >= ?1", [3])


def test_metadata() -> None:
    agg = I.price.sum()
    assert agg.kind == "sum"
    assert agg.column is I.price
    assert count().column is None
    assert count().kind == "count"
