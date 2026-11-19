"""Tests for the query builders and their compilation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from korbase._expr import Param, Value, cols, param
from korbase._ident import KorbaseError
from korbase._meta import PK, Default, table
from korbase._query import delete, insert, select, update, upsert


@table("users")
@dataclass
class User:
    id: Annotated[int, PK(), Default()]
    name: str
    age: int | None = None


U: Any = cols(User)


def test_select_all() -> None:
    c = select(User).compile()
    assert c.sql == 'SELECT "users"."id", "users"."name", "users"."age" FROM "users"'
    assert c.slots == ()


def test_select_full() -> None:
    q = (
        select(User)
        .where(U.age > param("min"))
        .where(U.name == "bob")
        .order_by(U.name.asc(), U.id.desc())
        .limit(10)
        .offset(param("off"))
    )
    c = q.compile()
    assert c.sql == (
        'SELECT "users"."id", "users"."name", "users"."age" FROM "users" '
        'WHERE ("users"."age" > $1 AND "users"."name" = $2) '
        'ORDER BY "users"."name" ASC, "users"."id" DESC LIMIT $3 OFFSET $4'
    )
    assert isinstance(c.slots[0], Param) and c.slots[0].name == "min"
    assert isinstance(c.slots[1], Value) and c.slots[1].value == "bob"
    assert isinstance(c.slots[2], Value) and c.slots[2].value == 10
    assert isinstance(c.slots[3], Param) and c.slots[3].name == "off"


def test_param_reuse() -> None:
    c = select(User).where((U.age > param("x")) | (U.id < param("x"))).compile()
    assert c.sql.endswith('WHERE ("users"."age" > $1 OR "users"."id" < $1)')
    assert len(c.slots) == 1


def test_load_is_stored_only() -> None:
    assert select(User).load().compile().sql == select(User).compile().sql


def test_limit_rejects_junk() -> None:
    with pytest.raises(KorbaseError):
        select(User).limit("1; DROP")  # type: ignore[arg-type]


def test_insert_default_skips_defaulted() -> None:
    c = insert(User).compile()
    assert c.sql == 'INSERT INTO "users" ("name", "age") VALUES ($1, $2)'
    assert [s.name for s in c.slots] == ["name", "age"]  # type: ignore[union-attr]


def test_insert_values_returning() -> None:
    c = insert(User).values(name=param("n"), age=3).returning().compile()
    assert c.sql == ('INSERT INTO "users" ("name", "age") VALUES ($1, $2) RETURNING "id", "name", "age"')
    assert isinstance(c.slots[1], Value) and c.slots[1].value == 3


def test_update() -> None:
    c = update(User).set(name="x", age=param("a")).where(U.id == param("i")).compile()
    assert c.sql == 'UPDATE "users" SET "name" = $1, "age" = $2 WHERE "users"."id" = $3'
    assert len(c.slots) == 3


def test_update_all_rows() -> None:
    assert update(User).set(age=1).all_rows().compile().sql == 'UPDATE "users" SET "age" = $1'


def test_update_guard() -> None:
    with pytest.raises(KorbaseError):
        update(User).set(age=1).compile()


def test_delete() -> None:
    c = delete(User).where(U.id == param("i")).compile()
    assert c.sql == 'DELETE FROM "users" WHERE "users"."id" = $1'
    assert delete(User).all_rows().compile().sql == 'DELETE FROM "users"'


def test_delete_guard() -> None:
    with pytest.raises(KorbaseError):
        delete(User).compile()


def test_upsert() -> None:
    q = upsert(User).values(id=param("id"), name=param("n"), age=1).on_conflict("id")
    assert q.compile().sql == (
        'INSERT INTO "users" ("id", "name", "age") VALUES ($1, $2, $3) '
        'ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name", "age" = EXCLUDED."age"'
    )
    assert q.returning().compile().sql.endswith('"age" = EXCLUDED."age" RETURNING "id", "name", "age"')


def test_upsert_do_nothing_and_guard() -> None:
    c = upsert(User).values(id=param("id")).on_conflict("id").compile()
    assert c.sql.endswith('ON CONFLICT ("id") DO NOTHING')
    with pytest.raises(KorbaseError):
        upsert(User).compile()


def test_memoised() -> None:
    q = select(User)
    assert q.compile() is q.compile()


def test_immutable() -> None:
    base = select(User)
    before = base.compile()
    derived = base.where(U.id == 1).limit(1)
    assert derived is not base
    assert base.compile() is before
    assert "WHERE" not in base.compile().sql


def test_unknown_column() -> None:
    with pytest.raises(KorbaseError):
        insert(User).values(nope=1)
    with pytest.raises(KorbaseError):
        update(User).set(nope=1)
    with pytest.raises(KorbaseError):
        upsert(User).on_conflict("nope")
