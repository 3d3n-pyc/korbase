"""Tests for Database and Transaction against a real Postgres."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Optional, TypedDict

import msgspec
import pytest

from korbase import (
    FK,
    PK,
    Database,
    KorbaseError,
    cols,
    delete,
    insert,
    param,
    select,
    table,
    update,
    upsert,
)


@table("users")
class User(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str


@table("users")
@dataclasses.dataclass
class UserDC:
    id: Annotated[int, PK()]
    name: str


@table("users")
class UserTD(TypedDict):
    id: Annotated[int, PK()]
    name: str


@table("posts")
class Post(msgspec.Struct):
    id: Annotated[int, PK()]
    user_id: Annotated[int, FK("users.id")]
    title: Optional[str]


@table("users")
class UserInt(msgspec.Struct):
    id: Annotated[int, PK()]
    name: int


SETUP = """
DROP TABLE IF EXISTS posts; DROP TABLE IF EXISTS users;
CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL);
CREATE TABLE posts (id int PRIMARY KEY, user_id int NOT NULL REFERENCES users(id), title text);
"""


def with_db(fn: Callable[[Database], Awaitable[None]], dsn: str, **kw: Any) -> None:
    """Run `fn` against a fresh schema."""

    async def main() -> None:
        async with await Database.connect(dsn, **kw) as db:
            await db.execute(SETUP)
            await fn(db)

    asyncio.run(main())


def test_insert_select_first(dsn: str) -> None:
    async def body(db: Database) -> None:
        q = insert(User).returning()
        assert await db.run(q, id=1, name="Ada") == [User(1, "Ada")]
        await db.run(insert(User).values(id=2, name="Bob"))
        await db.run(insert(User).values(id=3, name="Cy"))
        u = cols(User)
        sel = select(User).where(u.id >= param("m")).order_by(u.id.desc()).limit(param("n")).offset(0)
        assert [x.id for x in await db.run(sel, m=1, n=2)] == [3, 2]
        assert await db.first(select(User).where(u.name == param("n")), n="Bob") == User(2, "Bob")
        assert await db.first(select(User).where(u.name == param("n")), n="x") is None
        assert await db.first(insert(User).values(id=9, name="Z").returning()) == User(9, "Z")

    with_db(body, dsn)


def test_model_kinds(dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.run(insert(UserDC).values(id=1, name="a"))
        assert await db.run(select(UserDC)) == [UserDC(1, "a")]
        assert await db.run(select(UserTD)) == [{"id": 1, "name": "a"}]
        assert await db.fetch("SELECT * FROM users", model=UserTD) == [{"id": 1, "name": "a"}]
        assert await db.fetch("SELECT name, id FROM users", model=UserDC) == [UserDC(1, "a")]

    with_db(body, dsn)


def test_update_delete_upsert(dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.run(insert(User).values(id=1, name="a"))
        tag = await db.run(update(User).set(name="b").where(cols(User).id == param("i")), i=1)
        assert tag == "UPDATE 1"
        up = upsert(User).on_conflict("id").returning()
        assert await db.run(up, id=1, name="c") == [User(1, "c")]
        assert await db.run(up, id=2, name="d") == [User(2, "d")]
        assert await db.run(delete(User).where(cols(User).id == 2)) == "DELETE 1"
        assert await db.run(delete(User).all_rows()) == "DELETE 1"
        with pytest.raises(KorbaseError):
            await db.run(delete(User))
        with pytest.raises(KorbaseError):
            await db.run(update(User).set(name="x"))

    with_db(body, dsn)


def test_param_errors(dsn: str) -> None:
    async def body(db: Database) -> None:
        q = select(User).where(cols(User).id == param("i"))
        with pytest.raises(KorbaseError, match="missing params: i"):
            await db.run(q)
        with pytest.raises(KorbaseError, match="unexpected params: z"):
            await db.run(q, i=1, z=2)

    with_db(body, dsn)


def test_raw_sql(dsn: str) -> None:
    async def body(db: Database) -> None:
        assert await db.execute("INSERT INTO users VALUES ($1, $2)", 1, "a") == "INSERT 0 1"
        assert await db.fetch("SELECT id, name FROM users WHERE id = $1", 1) == [(1, "a")]
        assert await db.fetchrow("SELECT id FROM users WHERE id = $1", 5) is None
        assert await db.fetchrow("SELECT * FROM users", model=User) == User(1, "a")
        assert await db.fetch("SELECT * FROM users", model=User) == [User(1, "a")]

    with_db(body, dsn)


def test_transaction_commit_and_rollback(dsn: str) -> None:
    async def body(db: Database) -> None:
        async with db.transaction() as tx:
            await tx.run(insert(User).values(id=1, name="a"))
        with pytest.raises(RuntimeError):
            async with db.transaction() as tx:
                await tx.run(insert(User).values(id=2, name="b"))
                raise RuntimeError
        assert [u.id for u in await db.run(select(User))] == [1]

    with_db(body, dsn)


def test_nested_savepoint(dsn: str) -> None:
    async def body(db: Database) -> None:
        async with db.transaction() as tx:
            await tx.run(insert(User).values(id=1, name="a"))
            with pytest.raises(RuntimeError):
                async with tx.transaction() as inner:
                    await inner.run(insert(User).values(id=2, name="b"))
                    raise RuntimeError
            assert [u.id for u in await tx.run(select(User))] == [1]
        assert [u.id for u in await db.run(select(User))] == [1]

    with_db(body, dsn)


def test_validate_rejects_mismatch(dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("INSERT INTO users VALUES (1, 'abc')")
        with pytest.raises(msgspec.ValidationError):
            await db.run(select(UserInt))
        assert await db.run(select(User)) == [User(1, "abc")]

    with_db(body, dsn, validate=True)


def test_relations_not_ready(dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.run(select(Post))

    with_db(body, dsn)


def test_validate_raw_model(dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("INSERT INTO users VALUES (1, 'abc')")
        with pytest.raises(msgspec.ValidationError):
            await db.fetch("SELECT * FROM users", model=UserInt)
        with pytest.raises(msgspec.ValidationError):
            await db.fetchrow("SELECT * FROM users", model=UserInt)
        assert await db.fetchrow("SELECT * FROM users", model=User) == User(1, "abc")

    with_db(body, dsn, validate=True)


def test_raw_model_on_empty_result(dsn: str) -> None:
    async def body(db: Database) -> None:
        assert await db.fetch("SELECT * FROM users", model=UserDC) == []
        assert await db.fetchrow("SELECT * FROM users", model=UserDC) is None
        with pytest.raises(KorbaseError):
            await db.fetch("SELECT id FROM users", model=UserDC)

    with_db(body, dsn)


def test_param_named_query_or_self(dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("INSERT INTO users VALUES (1, 'abc')")
        q = select(User).where(cols(User).name == param("query"))
        assert await db.run(q, query="abc") == [User(1, "abc")]
        assert await db.first(q, query="abc") == User(1, "abc")
        q = select(User).where(cols(User).name == param("self"))
        assert await db.first(q, self="abc") == User(1, "abc")

    with_db(body, dsn)


def test_none_comparison(dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("INSERT INTO users VALUES (1, 'a')")
        await db.execute("INSERT INTO posts VALUES (1, 1, NULL), (2, 1, 'x')")
        c = cols(Post)
        assert [p.id for p in await db.run(select(Post).where(c.title == None))] == [1]  # noqa: E711
        assert [p.id for p in await db.run(select(Post).where(c.title != None))] == [2]  # noqa: E711

    with_db(body, dsn)
