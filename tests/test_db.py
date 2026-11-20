"""Tests for Database and Transaction against a real Postgres."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, Optional, TypedDict

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


if TYPE_CHECKING:
    from typing import assert_type

SETUP = """
DROP TABLE IF EXISTS posts;
DROP TABLE IF EXISTS users;
CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL);
CREATE TABLE posts (id int PRIMARY KEY, user_id int NOT NULL REFERENCES users(id), title text);
"""


def ph(db: Database, n: int) -> str:
    """Return the raw-SQL placeholder number `n` of the database's backend."""
    return f"?{n}" if db._dialect.name == "sqlite" else f"${n}"


def with_db(fn: Callable[[Database], Awaitable[None]], dsn: str, **kw: Any) -> None:
    """Run `fn` against a fresh schema."""

    async def main() -> None:
        async with await Database.connect(dsn, **kw) as db:
            async with db._pool.acquire() as conn:
                await conn.execute_script(SETUP)
            await fn(db)

    asyncio.run(main())


def test_insert_select_first(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        q = insert(User)
        assert await db.run(q, id=1, name="Ada") == [User(1, "Ada")]
        await db.run(insert(User).values(id=2, name="Bob"))
        await db.run(insert(User).values(id=3, name="Cy"))
        u = cols(User)
        sel = select(User).where(u.id >= param("m")).order_by(u.id.desc()).limit(param("n")).offset(0)
        assert [x.id for x in await db.run(sel, m=1, n=2)] == [3, 2]
        assert await db.first(select(User).where(u.name == param("n")), n="Bob") == User(2, "Bob")
        assert await db.first(select(User).where(u.name == param("n")), n="x") is None
        assert await db.first(insert(User).values(id=9, name="Z")) == User(9, "Z")

    with_db(body, backend_dsn)


def test_model_kinds(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.run(insert(UserDC).values(id=1, name="a"))
        assert await db.run(select(UserDC)) == [UserDC(1, "a")]
        assert await db.run(select(UserTD)) == [{"id": 1, "name": "a"}]
        assert await db.fetch("SELECT * FROM users", model=UserTD) == [{"id": 1, "name": "a"}]
        assert await db.fetch("SELECT name, id FROM users", model=UserDC) == [UserDC(1, "a")]

    with_db(body, backend_dsn)


def test_update_delete_upsert(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.run(insert(User).values(id=1, name="a"))
        tag = await db.run(update(User).set(name="b").where(cols(User).id == param("i")), i=1)
        assert tag == 1
        up = upsert(User).on_conflict("id")
        assert await db.run(up, id=1, name="c") == [User(1, "c")]
        assert await db.run(up, id=2, name="d") == [User(2, "d")]
        assert await db.run(delete(User).where(cols(User).id == 2)) == 1
        assert await db.run(delete(User).all_rows()) == 1
        with pytest.raises(KorbaseError):
            await db.run(delete(User))
        with pytest.raises(KorbaseError):
            await db.run(update(User).set(name="x"))

    with_db(body, backend_dsn)


def test_param_errors(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        q = select(User).where(cols(User).id == param("i"))
        with pytest.raises(KorbaseError, match="missing params: i"):
            await db.run(q)
        with pytest.raises(KorbaseError, match="unexpected params: z"):
            await db.run(q, i=1, z=2)

    with_db(body, backend_dsn)


def test_raw_sql(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        assert await db.execute(f"INSERT INTO users VALUES ({ph(db, 1)}, {ph(db, 2)})", 1, "a") == "INSERT 0 1"
        assert await db.fetch(f"SELECT id, name FROM users WHERE id = {ph(db, 1)}", 1) == [(1, "a")]
        assert await db.fetchrow(f"SELECT id FROM users WHERE id = {ph(db, 1)}", 5) is None
        assert await db.fetchrow("SELECT * FROM users", model=User) == User(1, "a")
        assert await db.fetch("SELECT * FROM users", model=User) == [User(1, "a")]

    with_db(body, backend_dsn)


def test_transaction_commit_and_rollback(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        async with db.transaction() as tx:
            await tx.run(insert(User).values(id=1, name="a"))
        with pytest.raises(RuntimeError):
            async with db.transaction() as tx:
                await tx.run(insert(User).values(id=2, name="b"))
                raise RuntimeError
        assert [u.id for u in await db.run(select(User))] == [1]

    with_db(body, backend_dsn)


def test_nested_savepoint(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        async with db.transaction() as tx:
            await tx.run(insert(User).values(id=1, name="a"))
            with pytest.raises(RuntimeError):
                async with tx.transaction() as inner:
                    await inner.run(insert(User).values(id=2, name="b"))
                    raise RuntimeError
            assert [u.id for u in await tx.run(select(User))] == [1]
        assert [u.id for u in await db.run(select(User))] == [1]

    with_db(body, backend_dsn)


def test_validate_rejects_mismatch(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("INSERT INTO users VALUES (1, 'abc')")
        with pytest.raises(msgspec.ValidationError):
            await db.run(select(UserInt))
        assert await db.run(select(User)) == [User(1, "abc")]

    with_db(body, backend_dsn, validate=True)


def test_relations_not_ready(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.run(select(Post))

    with_db(body, backend_dsn)


def test_validate_raw_model(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("INSERT INTO users VALUES (1, 'abc')")
        with pytest.raises(msgspec.ValidationError):
            await db.fetch("SELECT * FROM users", model=UserInt)
        with pytest.raises(msgspec.ValidationError):
            await db.fetchrow("SELECT * FROM users", model=UserInt)
        assert await db.fetchrow("SELECT * FROM users", model=User) == User(1, "abc")

    with_db(body, backend_dsn, validate=True)


def test_raw_model_on_empty_result(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        assert await db.fetch("SELECT * FROM users", model=UserDC) == []
        assert await db.fetchrow("SELECT * FROM users", model=UserDC) is None
        with pytest.raises(KorbaseError):
            await db.fetch("SELECT id FROM users", model=UserDC)

    with_db(body, backend_dsn)


def test_param_named_query_or_self(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("INSERT INTO users VALUES (1, 'abc')")
        q = select(User).where(cols(User).name == param("query"))
        assert await db.run(q, query="abc") == [User(1, "abc")]
        assert await db.first(q, query="abc") == User(1, "abc")
        q = select(User).where(cols(User).name == param("self"))
        assert await db.first(q, self="abc") == User(1, "abc")

    with_db(body, backend_dsn)


def test_none_comparison(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("INSERT INTO users VALUES (1, 'a')")
        await db.execute("INSERT INTO posts VALUES (1, 1, NULL), (2, 1, 'x')")
        c = cols(Post)
        assert [p.id for p in await db.run(select(Post).where(c.title == None))] == [1]  # noqa: E711
        assert [p.id for p in await db.run(select(Post).where(c.title != None))] == [2]  # noqa: E711

    with_db(body, backend_dsn)


def test_param_none_equality(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        u = cols(User)
        for q in (select(User).where(u.name == param("n")), select(User).where(u.name != param("n"))):
            with pytest.raises(KorbaseError, match="is_null"):
                await db.run(q, n=None)
        with pytest.raises(KorbaseError, match="is_null"):
            await db.run(select(User).where(u.name.in_(param("n"))), n=None)
        with pytest.raises(KorbaseError, match="is_null"):
            await db.run(select(User).where((u.name > param("n")) & (u.name == param("n"))), n=None)
        assert await db.run(select(User).where(u.name > param("n")).limit(0), n=None) == []
        assert await db.run(select(User).where(name=None)) == []

    with_db(body, backend_dsn)


def test_run_types(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        u = cols(User)
        if TYPE_CHECKING:
            assert_type(await db.run(select(User)), list[User])
            assert_type(await db.run(insert(User).values(id=1, name="a")), list[User])
            assert_type(await db.run(upsert(User).on_conflict("id")), list[User])
            assert_type(await db.run(update(User).set(name="a").all_rows()), int)
            assert_type(await db.run(update(User).set(name="a").all_rows().returning()), list[User])
            assert_type(await db.run(delete(User).all_rows()), int)
            assert_type(await db.run(delete(User).all_rows().returning()), list[User])
            assert_type(await db.first(select(User)), Optional[User])
            assert_type(await db.fetch("SELECT 1", model=User), list[User])
            assert_type(await db.fetch("SELECT 1"), list[tuple[Any, ...]])
            assert_type(await db.fetchrow("SELECT 1", model=User), Optional[User])
            assert_type(await db.fetchrow("SELECT 1"), Optional[tuple[Any, ...]])
        await db.run(insert(User).values(id=1, name="a"))
        assert await db.run(update(User).set(name="b").where(u.id == 1)) == 1
        assert await db.run(update(User).set(name="c").where(u.id == 1).returning()) == [User(1, "c")]
        assert await db.run(delete(User).where(u.id == 1).returning()) == [User(1, "c")]
        with pytest.raises(KorbaseError, match="returns rows"):
            await db.first(delete(User).all_rows())  # type: ignore[call-overload]  # pyright: ignore

    with_db(body, backend_dsn)


@table("typed")
class Typed(msgspec.Struct):
    id: Annotated[int, PK()]
    flag: bool
    uid: uuid.UUID
    at: dt.datetime
    price: Decimal


@table("typed_json")
class TypedJson(msgspec.Struct):
    id: Annotated[int, PK()]
    meta: dict[str, Any]
    tags: list[str]


TYPED_DDL = {
    "sqlite": "CREATE TABLE typed (id integer PRIMARY KEY, flag integer, uid text, at text, price text)",
    "postgres": "CREATE TABLE typed (id int PRIMARY KEY, flag bool, uid uuid, at timestamptz, price numeric)",
}


def _typed(i: int, hour: int) -> Typed:
    """Build a `Typed` row whose datetime is `hour` o'clock UTC."""
    return Typed(
        i,
        i % 2 == 0,
        uuid.UUID(int=i),
        dt.datetime(2024, 1, 1, hour, tzinfo=dt.timezone.utc),
        Decimal("12.50") * i,
    )


def test_value_round_trip(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        await db.execute("DROP TABLE IF EXISTS typed")
        await db.execute(TYPED_DDL[db._dialect.name])
        rows = [_typed(1, 10), _typed(2, 9)]
        for r in rows:
            await db.run(insert(Typed).values(**{f: getattr(r, f) for f in r.__struct_fields__}))
        t = cols(Typed)
        got = await db.run(select(Typed).order_by(t.at.asc()))
        assert got == [rows[1], rows[0]]
        assert all(isinstance(r.flag, bool) and isinstance(r.price, Decimal) for r in got)
        assert got[0].at.utcoffset() == dt.timedelta(0)
        assert await db.run(select(Typed).where(t.flag == True)) == [rows[1]]  # noqa: E712

    with_db(body, backend_dsn)


def test_sqlite_storage_forms(backend_dsn: str) -> None:
    if not backend_dsn.startswith("sqlite:"):
        pytest.skip("storage forms are SQLite-specific")

    async def body(db: Database) -> None:
        await db.execute(TYPED_DDL["sqlite"])
        r = _typed(2, 9)
        await db.run(insert(Typed).values(**{f: getattr(r, f) for f in r.__struct_fields__}))
        row = await db.fetchrow("SELECT typeof(flag), flag, uid, at, price FROM typed")
        assert row is not None
        assert row[:2] == ("integer", 1)
        assert row[2] == str(uuid.UUID(int=2))
        assert row[3] == "2024-01-01T09:00:00.000000+00:00"
        assert row[4] == "25.00"

    with_db(body, backend_dsn)


def test_sqlite_json_round_trip(backend_dsn: str) -> None:
    if not backend_dsn.startswith("sqlite:"):
        pytest.skip("dict and list columns are stored as JSON text on SQLite only")

    async def body(db: Database) -> None:
        await db.execute("CREATE TABLE typed_json (id integer PRIMARY KEY, meta text, tags text)")
        row = TypedJson(1, {"k": [1, None], "n": {"x": "é"}}, ["a", "b"])
        await db.run(insert(TypedJson).values(id=row.id, meta=row.meta, tags=row.tags))
        assert await db.run(select(TypedJson)) == [row]
        raw = await db.fetchrow("SELECT meta, tags FROM typed_json")
        assert raw == ('{"k":[1,null],"n":{"x":"\\u00e9"}}', '["a","b"]')

    with_db(body, backend_dsn)
