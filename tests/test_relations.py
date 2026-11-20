"""Tests for relation loading with `.load(...)`."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional, TypedDict

import msgspec
import pytest

from korbase import FK, PK, Database, KorbaseError, Rel, cols, select, table
from korbase._meta import table_meta


@table("users")
class UserA(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str
    posts: Annotated[list[PostA], Rel("author_id")] = msgspec.field(default_factory=list)


@table("posts")
class PostA(msgspec.Struct):
    id: Annotated[int, PK()]
    author_id: Optional[int]
    title: str
    author: Annotated[Optional[UserA], Rel("author_id")] = None


@table("posts")
class PostB(msgspec.Struct):
    id: Annotated[int, PK()]
    author_id: Optional[int]
    title: str
    author: Annotated[Optional[UserB], Rel("author_id")] = None


@table("users")
class UserB(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str
    posts: Annotated[list[PostB], Rel("author_id")] = msgspec.field(default_factory=list)


@table("users")
@dataclasses.dataclass(frozen=True)
class UserDC:
    id: Annotated[int, PK()]
    name: str
    posts: Annotated[list[PostDC], Rel("author_id")] = dataclasses.field(default_factory=list)


@table("posts")
@dataclasses.dataclass(frozen=True)
class PostDC:
    id: Annotated[int, PK()]
    author_id: Optional[int]
    title: str
    author: Annotated[Optional[UserDC], Rel("author_id")] = None


@table("users")
class UserTD(TypedDict):
    id: Annotated[int, PK()]
    name: str
    posts: Annotated[list[PostTD], Rel("author_id")]


@table("posts")
class PostTD(TypedDict):
    id: Annotated[int, PK()]
    author_id: Optional[int]
    title: str
    author: Annotated[Optional[UserTD], Rel("author_id")]


@table("users")
class Bad(msgspec.Struct):
    id: Annotated[int, PK()]
    other: Annotated[Optional[Nowhere], Rel("id")] = None  # type: ignore[name-defined]  # noqa: F821


@table("pairs")
class Pair(msgspec.Struct):
    a: Annotated[int, PK()]
    b: Annotated[int, PK()]
    kids: Annotated[list[PostA], Rel("author_id")] = msgspec.field(default_factory=list)


SETUP = """
DROP TABLE IF EXISTS posts; DROP TABLE IF EXISTS users; DROP TABLE IF EXISTS pairs;
CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL);
CREATE TABLE posts (id int PRIMARY KEY, author_id int REFERENCES users(id), title text NOT NULL);
CREATE TABLE pairs (a int, b int, PRIMARY KEY (a, b));
INSERT INTO users VALUES (1, 'Ada'), (2, 'Bob'), (3, 'Cy');
INSERT INTO posts VALUES (1, 1, 'p1'), (2, 1, 'p2'), (3, 2, 'p3'), (4, NULL, 'p4');
INSERT INTO pairs VALUES (1, 1);
"""


class CountingConn:
    """Wraps a connection and counts `fetch` calls."""

    def __init__(self, conn: Any, sqls: list[str]) -> None:
        self._conn = conn
        self._sqls = sqls

    async def fetch(self, sql: str, args: Any, builder: Any) -> Any:
        self._sqls.append(sql)
        return await self._conn.fetch(sql, args, builder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class CountingPool:
    """Wraps a pool so every connection counts its fetches."""

    def __init__(self, pool: Any, sqls: list[str]) -> None:
        self._pool = pool
        self._sqls = sqls

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        async with self._pool.acquire() as conn:
            yield CountingConn(conn, self._sqls)

    async def close(self) -> None:
        await self._pool.close()


def with_db(fn: Callable[[Database, list[str]], Awaitable[None]], dsn: str) -> None:
    """Run `fn` against a fresh schema, with a list collecting every fetched SQL."""

    async def main() -> None:
        async with await Database.connect(dsn) as plain:
            await plain.execute(SETUP)
            sqls: list[str] = []
            await fn(Database(CountingPool(plain._pool, sqls)), sqls)

    asyncio.run(main())


@pytest.mark.parametrize(("user", "post"), [(UserA, PostA), (UserB, PostB), (UserDC, PostDC)])
def test_both_directions(dsn: str, user: Any, post: Any) -> None:
    async def body(db: Database, sqls: list[str]) -> None:
        posts = await db.run(select(post).order_by(cols(post).id.asc()).load(cols(post).author))
        assert len(sqls) == 2
        assert 'FROM "users" WHERE "users"."id" = ANY($1)' in sqls[1]
        assert [p.author.name if p.author else None for p in posts] == ["Ada", "Ada", "Bob", None]
        sqls.clear()
        users = await db.run(select(user).order_by(cols(user).id.asc()).load(cols(user).posts))
        assert len(sqls) == 2
        assert 'WHERE "posts"."author_id" = ANY($1)' in sqls[1]
        assert [[p.id for p in u.posts] for u in users] == [[1, 2], [3], []]

    with_db(body, dsn)


def test_typeddict(dsn: str) -> None:
    async def body(db: Database, sqls: list[str]) -> None:
        posts = await db.run(select(PostTD).order_by(cols(PostTD).id.asc()).load(cols(PostTD).author))
        assert [p["author"] and p["author"]["name"] for p in posts] == ["Ada", "Ada", "Bob", None]
        users = await db.run(select(UserTD).order_by(cols(UserTD).id.asc()).load(cols(UserTD).posts))
        assert [len(u["posts"]) for u in users] == [2, 1, 0]
        assert len(sqls) == 4

    with_db(body, dsn)


def test_two_loads_two_queries_and_first(dsn: str) -> None:
    async def body(db: Database, sqls: list[str]) -> None:
        u = cols(UserA)
        q = select(UserA).where(u.id == 1).load(u.posts, u.posts)
        row = await db.first(q)
        assert row is not None and [p.id for p in row.posts] == [1, 2]
        assert len(sqls) == 2
        sqls.clear()
        await db.run(select(UserA).where(u.id == 99).load(u.posts))
        assert len(sqls) == 1

    with_db(body, dsn)


def test_inside_transaction(dsn: str) -> None:
    async def body(db: Database, sqls: list[str]) -> None:
        async with db.transaction() as tx:
            posts = await tx.run(select(PostA).load(cols(PostA).author))
            assert all(p.author is not None or p.author_id is None for p in posts)
            users = await tx.run(select(UserA).load(cols(UserA).posts))
            assert sum(len(x.posts) for x in users) == 3
        assert len(sqls) == 4

    with_db(body, dsn)


def test_unresolvable_forward_ref(dsn: str) -> None:
    assert table_meta(Bad).relations[0].name == "other"
    with pytest.raises(KorbaseError, match="cannot resolve"):
        _ = table_meta(Bad).relations[0].target_model

    async def body(db: Database, sqls: list[str]) -> None:
        with pytest.raises(KorbaseError, match="cannot resolve"):
            await db.run(select(Bad).load(cols(Bad).other))

    with_db(body, dsn)


def test_composite_pk_not_supported(dsn: str) -> None:
    async def body(db: Database, sqls: list[str]) -> None:
        with pytest.raises(KorbaseError, match="not supported yet"):
            await db.run(select(Pair).load(cols(Pair).kids))

    with_db(body, dsn)


@table("lg_a")
class LegacyParent(msgspec.Struct):
    id: Annotated[int, PK()]
    legacy: int
    kids: Annotated[list[LegacyChild], Rel("parent_legacy")] = msgspec.field(default_factory=list)


@table("lg_c")
class LegacyChild(msgspec.Struct):
    id: Annotated[int, PK()]
    parent_legacy: Annotated[Optional[int], FK("lg_a.legacy")]
    parent: Annotated[Optional[LegacyParent], Rel("parent_legacy")] = None


LEGACY_SETUP = """
DROP TABLE IF EXISTS lg_c; DROP TABLE IF EXISTS lg_a;
CREATE TABLE lg_a (id int PRIMARY KEY, legacy int UNIQUE NOT NULL);
CREATE TABLE lg_c (id int PRIMARY KEY, parent_legacy int);
INSERT INTO lg_a VALUES (1, 100), (2, 200);
INSERT INTO lg_c VALUES (1, 200), (2, 100), (3, 100);
"""


def test_fk_marker_column_is_used_in_both_directions(dsn: str) -> None:
    async def body(db: Database, sqls: list[str]) -> None:
        await db.execute(LEGACY_SETUP)
        kids = await db.run(select(LegacyChild).load(cols(LegacyChild).parent).order_by(cols(LegacyChild).id))
        assert [k.parent.id for k in kids if k.parent] == [2, 1, 1]
        assert 'WHERE "lg_a"."legacy" = ANY($1)' in sqls[-1]
        parents = await db.run(select(LegacyParent).load(cols(LegacyParent).kids).order_by(cols(LegacyParent).id))
        assert [[k.id for k in p.kids] for p in parents] == [[2, 3], [1]]

    with_db(body, dsn)


def test_fetch_in_sqlite_sql_and_json_param() -> None:
    from korbase._dialect import SQLITE
    from korbase._relations import _fetch_in

    class FakeConn:
        async def fetch(self, sql: str, args: list[Any], builder: Any) -> list[Any]:
            self.seen = (sql, args)
            return []

    conn = FakeConn()
    asyncio.run(_fetch_in(conn, table_meta(UserA), "id", [1, 2], False, SQLITE))  # type: ignore[arg-type]
    sql, args = conn.seen
    assert f'WHERE "{table_meta(UserA).name}"."id" IN (SELECT value FROM json_each(?1))' in sql
    assert args == ["[1,2]"]
