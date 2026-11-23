"""Tests for join compilation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from korbase._dialect import PG, SQLITE
from korbase._expr import cols, count
from korbase._ident import KorbaseError
from korbase._meta import FK, PK, Default, table
from korbase._query import SelectColumns, SelectJoin, select


@table("boards")
@dataclass
class Board:
    id: Annotated[int, PK(), Default()]
    name: str


@table("lists")
@dataclass
class ListRow:
    id: Annotated[int, PK(), Default()]
    board_id: Annotated[int, FK("boards.id")]
    title: str


@table("tasks")
@dataclass
class Task:
    id: Annotated[int, PK(), Default()]
    list_id: Annotated[int, FK("lists.id")]
    title: str


@table("loose")
@dataclass
class Loose:
    id: Annotated[int, PK()]


@table("twins")
@dataclass
class Twin:
    id: Annotated[int, PK()]
    a: Annotated[int, FK("boards.id")]
    b: Annotated[int, FK("boards.id")]


T, L, B = cols(Task), cols(ListRow), cols(Board)
TASK = '"tasks"."id", "tasks"."list_id", "tasks"."title"'
LIST = '"lists"."id", "lists"."board_id", "lists"."title"'
BOARD = '"boards"."id", "boards"."name"'


def test_fk_inferred_forward() -> None:
    q = select(Task).join(ListRow)
    assert isinstance(q, SelectJoin)
    c = q.compile()
    assert c.sql == f'SELECT {TASK}, {LIST} FROM "tasks" JOIN "lists" ON "tasks"."list_id" = "lists"."id"'
    assert [(p.meta.name, p.start, p.stop, p.left) for p in c.parts] == [("tasks", 0, 3, False), ("lists", 3, 6, False)]


def test_fk_inferred_backward_and_left() -> None:
    c = select(ListRow).left_join(Task).compile(SQLITE)
    assert c.sql == f'SELECT {LIST}, {TASK} FROM "lists" LEFT JOIN "tasks" ON "tasks"."list_id" = "lists"."id"'
    assert [p.left for p in c.parts] == [False, True]


def test_explicit_on() -> None:
    q = select(ListRow).left_join(Task, on=T.list_id == L.id)
    assert q.compile().sql.endswith('LEFT JOIN "tasks" ON "tasks"."list_id" = "lists"."id"')


def test_two_joins_both_dialects() -> None:
    q = select(Task).join(ListRow).left_join(Board)
    sql = f'SELECT {TASK}, {LIST}, {BOARD} FROM "tasks" JOIN "lists" ON "tasks"."list_id" = "lists"."id" '
    sql += 'LEFT JOIN "boards" ON "lists"."board_id" = "boards"."id"'
    assert q.compile(PG).sql == sql
    assert q.compile(SQLITE).sql == sql
    assert [p.stop for p in q.compile().parts] == [3, 6, 8]


def test_where_order_paging_and_kwargs() -> None:
    q = select(Task).join(ListRow).where(L.title == "x", title="t").order_by(L.title.desc()).limit(5).offset(2)
    sql = q.compile().sql
    assert sql.endswith(
        'WHERE ("lists"."title" = $1 AND "tasks"."title" = $2) ORDER BY "lists"."title" DESC LIMIT $3 OFFSET $4'
    )
    assert "?1" in q.compile(SQLITE).sql


def test_columns_group_by_over_join() -> None:
    q = select(Task).join(ListRow).columns(L.title, count()).group_by(L.title)
    assert isinstance(q, SelectColumns)
    c = q.compile()
    assert c.sql == (
        'SELECT "lists"."title", count(*) FROM "tasks" JOIN "lists" ON "tasks"."list_id" = "lists"."id" '
        'GROUP BY "lists"."title"'
    )
    assert c.outputs == (L.title, None)


def test_no_fk_or_several_fks() -> None:
    with pytest.raises(KorbaseError, match="on="):
        select(Task).join(Loose).compile()
    with pytest.raises(KorbaseError, match="several"):
        select(Board).join(Twin)


def test_on_must_be_expression() -> None:
    with pytest.raises(KorbaseError, match="expression"):
        select(Task).join(ListRow, on='"a" = "b"')  # type: ignore[arg-type]


def test_refusals() -> None:
    with pytest.raises(KorbaseError, match="already"):
        select(Task).join(ListRow).join(Task, on=T.id == L.id)
    with pytest.raises(KorbaseError, match="at most"):
        select(Task).join(ListRow).join(Board).join(Loose, on=cols(Loose).id == B.id)  # type: ignore[call-overload]
    with pytest.raises(KorbaseError, match="not in the query"):
        select(Task).join(ListRow).where(B.name == "x").compile()
    with pytest.raises(KorbaseError, match="not in the query"):
        select(Task).join(ListRow).order_by(B.name.asc()).compile()
    with pytest.raises(KorbaseError):
        select(Task).join(ListRow).columns(B.name).compile()
    with pytest.raises(KorbaseError, match="not in the query"):
        select(Task).join(ListRow).columns(B.id.sum()).compile()
