"""Tests for column selection, GROUP BY and HAVING compilation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from korbase._dialect import PG, SQLITE
from korbase._expr import Param, Value, cols, count, param
from korbase._ident import KorbaseError
from korbase._meta import PK, Default, table
from korbase._query import SelectColumns, select


@table("tasks")
@dataclass
class Task:
    id: Annotated[int, PK(), Default()]
    list_id: int
    done: bool
    points: int


T = cols(Task)


def test_count_star_with_where() -> None:
    q = select(Task).columns(count()).where(done=False)
    assert isinstance(q, SelectColumns)
    c = q.compile()
    assert c.sql == 'SELECT count(*) FROM "tasks" WHERE "tasks"."done" = $1'
    assert c.outputs == (None,)


def test_group_by_aggregates_both_dialects() -> None:
    q = (
        select(Task)
        .columns(T.list_id, T.points.sum(), T.points.avg(), T.points.min(), T.points.max())
        .group_by(T.list_id)
    )
    pg = q.compile(PG)
    assert pg.sql == (
        'SELECT "tasks"."list_id", sum("tasks"."points"), CAST(avg("tasks"."points") AS DOUBLE PRECISION), '
        'min("tasks"."points"), max("tasks"."points") FROM "tasks" GROUP BY "tasks"."list_id"'
    )
    lite = q.compile(SQLITE).sql
    assert "CAST" not in lite and "avg(" in lite
    assert pg.outputs == (T.list_id, T.points, None, T.points, T.points)


def test_having_param_order_limit() -> None:
    q = (
        select(Task)
        .columns(T.list_id, count())
        .where(done=False)
        .group_by(T.list_id)
        .having(count() > param("n"))
        .order_by(count().desc())
        .limit(5)
    )
    pg = q.compile(PG)
    assert pg.sql == (
        'SELECT "tasks"."list_id", count(*) FROM "tasks" WHERE "tasks"."done" = $1 '
        'GROUP BY "tasks"."list_id" HAVING count(*) > $2 ORDER BY count(*) DESC LIMIT $3'
    )
    assert isinstance(pg.slots[0], Value) and isinstance(pg.slots[1], Param) and isinstance(pg.slots[2], Value)
    assert "HAVING count(*) > ?2 ORDER BY count(*) DESC LIMIT ?3" in q.compile(SQLITE).sql


def test_columns_replaces_and_is_immutable() -> None:
    base = select(Task).columns(count())
    other = base.columns(T.id)
    assert "count" in base.compile().sql and "count" not in other.compile().sql


def test_columns_rejects_strings() -> None:
    with pytest.raises(KorbaseError):
        select(Task).columns("id")  # type: ignore[call-overload]


def test_no_load_on_columns() -> None:
    assert not hasattr(select(Task).columns(count()), "load")


@pytest.mark.parametrize(
    "build",
    [
        lambda: select(Task).columns(),  # type: ignore[call-overload]
        lambda: select(Task).columns(T.list_id, count()),
        lambda: select(Task).columns(T.list_id, T.done).group_by(T.list_id),
        lambda: select(Task).columns(T.list_id).having(T.list_id > 1),
    ],
)
def test_refusals(build) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(KorbaseError):
        build().compile()
