"""Tests for the query builders and their compilation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

import pytest

from korbase._dialect import PG, SQLITE
from korbase._expr import Param, Value, cols, param
from korbase._ident import KorbaseError
from korbase._meta import PK, Default, table
from korbase._query import (
    Delete,
    DeleteReturning,
    Insert,
    Select,
    Update,
    UpdateReturning,
    Upsert,
    delete,
    insert,
    select,
    update,
    upsert,
)

if TYPE_CHECKING:
    from typing import assert_type


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
    assert c.sql == 'INSERT INTO "users" ("name", "age") VALUES ($1, $2) RETURNING "id", "name", "age"'
    assert [s.name for s in c.slots] == ["name", "age"]  # type: ignore[union-attr]


def test_insert_values_returning() -> None:
    c = insert(User).values(name=param("n"), age=3).compile()
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
        'ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name", "age" = EXCLUDED."age" '
        'RETURNING "id", "name", "age"'
    )


def test_upsert_do_nothing_and_guard() -> None:
    c = upsert(User).values(id=param("id")).on_conflict("id").compile()
    assert c.sql.endswith('ON CONFLICT ("id") DO NOTHING RETURNING "id", "name", "age"')
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


def test_upsert_never_sets_pk() -> None:
    sql = upsert(User).on_conflict("name").compile().sql
    assert 'ON CONFLICT ("name") DO UPDATE SET "age" = EXCLUDED."age"' in sql
    assert 'SET "id"' not in sql
    sql = upsert(User).values(id=1, name="a").on_conflict("name").compile().sql
    assert 'ON CONFLICT ("name") DO NOTHING' in sql


@pytest.mark.parametrize("bad", [True, 1, "x", None])
def test_where_needs_expression(bad: Any) -> None:
    with pytest.raises(TypeError):
        update(User).set(name="a").where(bad)
    with pytest.raises(TypeError):
        delete(User).where(bad)
    with pytest.raises(TypeError):
        select(User).where(bad)


@pytest.mark.parametrize("bad", ["name", 1, None])
def test_order_by_needs_order(bad: Any) -> None:
    with pytest.raises(TypeError):
        select(User).order_by(bad)
    sql = select(User).order_by(U.id, U.name.desc()).compile().sql
    assert sql.endswith('ORDER BY "users"."id", "users"."name" DESC')


def test_insert_default_values() -> None:
    @table("tick")
    @dataclass
    class Tick:
        id: Annotated[int, PK(), Default()]

    assert insert(Tick).compile().sql == 'INSERT INTO "tick" DEFAULT VALUES RETURNING "id"'


def test_update_delete_returning() -> None:
    u = update(User).set(age=1).where(U.id == 1)
    assert u.compile().sql == 'UPDATE "users" SET "age" = $1 WHERE "users"."id" = $2'
    assert u.returning().compile().sql.endswith('"users"."id" = $2 RETURNING "id", "name", "age"')
    d = delete(User).where(U.id == 1)
    assert d.compile().sql == 'DELETE FROM "users" WHERE "users"."id" = $1'
    assert d.returning().compile().sql.endswith('RETURNING "id", "name", "age"')
    assert not u.returns_rows and u.returning().returns_rows
    assert not d.returns_rows and d.returning().returns_rows
    assert select(User).returns_rows and insert(User).returns_rows
    assert select(User).model is User


def test_where_several_and_keywords() -> None:
    c = select(User).where(U.age > 1, U.id < 9).compile()
    assert c.sql.endswith('WHERE ("users"."age" > $1 AND "users"."id" < $2)')
    c = select(User).where(U.age > 1, name="bob", id=param("i")).compile()
    assert c.sql.endswith('WHERE (("users"."age" > $1 AND "users"."name" = $2) AND "users"."id" = $3)')
    assert isinstance(c.slots[2], Param) and c.slots[2].name == "i"
    c = update(User).set(age=1).where(name="a").compile()
    assert c.sql.endswith('WHERE "users"."name" = $2')
    assert delete(User).where(id=1).compile().sql == 'DELETE FROM "users" WHERE "users"."id" = $1'


def test_where_none_keyword_is_null() -> None:
    sql = select(User).where(age=None).compile().sql
    assert sql.endswith('WHERE "users"."age" IS NULL')


def test_where_keyword_errors() -> None:
    with pytest.raises(KorbaseError, match="nope"):
        select(User).where(nope=1)
    with pytest.raises(TypeError):
        select(User).where()
    with pytest.raises(TypeError):
        delete(User).where()
    with pytest.raises(KorbaseError):
        delete(User).compile()


def test_builder_types() -> None:
    if TYPE_CHECKING:
        assert_type(select(User), Select[User])
        assert_type(select(User).where(id=1).limit(1), Select[User])
        assert_type(insert(User).values(name="a"), Insert[User])
        assert_type(upsert(User).on_conflict("id"), Upsert[User])
        assert_type(update(User).set(age=1).where(id=1), Update[User])
        assert_type(update(User).returning(), UpdateReturning[User])
        assert_type(delete(User).where(id=1), Delete[User])
        assert_type(delete(User).returning(), DeleteReturning[User])


def test_sqlite_select_where_in_ilike() -> None:
    q = select(User).where(U.name.ilike("a%") & U.id.in_([1, 2]) & (U.age > param("x")))
    sql = q.compile(SQLITE).sql
    assert '"users"."name" LIKE ?1' in sql
    assert '"users"."id" IN (SELECT value FROM json_each(?2))' in sql
    assert '"users"."age" > ?3' in sql
    assert "$" not in sql
    assert "ILIKE" in q.compile().sql and "$1" in q.compile().sql


def test_sqlite_param_reuse() -> None:
    c = select(User).where((U.age > param("x")) | (U.id < param("x"))).compile(SQLITE)
    assert c.sql.endswith('"users"."age" > ?1 OR "users"."id" < ?1)') or "?2" not in c.sql
    assert len(c.slots) == 1


def test_sqlite_writes() -> None:
    assert insert(User).values(name=param("n")).compile(SQLITE).sql == insert(User).values(
        name=param("n")
    ).compile().sql.replace("$", "?")
    assert update(User).set(age=1).all_rows().compile(SQLITE).sql == 'UPDATE "users" SET "age" = ?1'
    assert delete(User).where(U.id == param("i")).compile(SQLITE).sql == 'DELETE FROM "users" WHERE "users"."id" = ?1'
    up = upsert(User).on_conflict("id").compile(SQLITE).sql
    assert "ON CONFLICT" in up and "EXCLUDED" in up.upper() and "$" not in up


def test_compile_memoised_per_dialect() -> None:
    q = select(User)
    pg, lite = q.compile(), q.compile(SQLITE)
    assert q.compile() is pg and q.compile(PG) is pg
    assert q.compile(SQLITE) is lite and lite is not pg
    assert lite.sql != pg.sql or "$" not in pg.sql
    assert q.where(U.id == 1).compile(SQLITE) is not lite
