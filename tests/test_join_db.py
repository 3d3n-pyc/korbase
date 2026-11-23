"""Join queries on both backends."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Optional

import msgspec

from korbase import FK, PK, Database, cols, insert, select, table
from korbase._expr import count


@table("jauthors")
class Author(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str
    ref: uuid.UUID
    active: bool


@table("jbooks")
class Book(msgspec.Struct):
    id: Annotated[int, PK()]
    author_id: Annotated[int, FK("jauthors.id")]
    title: str
    day: dt.date


@table("jnotes")
class Note(msgspec.Struct):
    id: Annotated[int, PK()]
    book_id: Annotated[int, FK("jbooks.id")]
    text: str


SETUP = """
DROP TABLE IF EXISTS jnotes;
DROP TABLE IF EXISTS jbooks;
DROP TABLE IF EXISTS jauthors;
CREATE TABLE jauthors (id int PRIMARY KEY, name text NOT NULL, ref uuid NOT NULL, active boolean NOT NULL);
CREATE TABLE jbooks (id int PRIMARY KEY, author_id int NOT NULL REFERENCES jauthors(id),
    title text NOT NULL, day date NOT NULL);
CREATE TABLE jnotes (id int PRIMARY KEY, book_id int NOT NULL REFERENCES jbooks(id), text text NOT NULL);
"""
U1, U2, U3 = (uuid.UUID(int=i) for i in (1, 2, 3))
AUTHORS = [Author(1, "ann", U1, True), Author(2, "bob", U2, False), Author(3, "cy", U3, True)]
BOOKS = [
    Book(1, 1, "a1", dt.date(2024, 1, 1)),
    Book(2, 1, "a2", dt.date(2024, 2, 1)),
    Book(3, 2, "b1", dt.date(2024, 3, 1)),
]
NOTES = [Note(1, 1, "n1")]
a, b = cols(Author), cols(Book)


def run(fn: Callable[[Database], Awaitable[None]], dsn: str) -> None:
    """Run `fn` against freshly filled tables."""

    async def main() -> None:
        async with await Database.connect(dsn) as db:
            async with db._pool.acquire() as conn:
                await conn.execute_script(SETUP)
            for model, rows in ((Author, AUTHORS), (Book, BOOKS), (Note, NOTES)):
                for r in rows:
                    await db.run(insert(model).values(**msgspec.structs.asdict(r)))
            await fn(db)

    asyncio.run(main())


def test_inner_join(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        rows = await db.run(select(Book).join(Author).order_by(b.id.asc()))
        assert rows == [(BOOKS[0], AUTHORS[0]), (BOOKS[1], AUTHORS[0]), (BOOKS[2], AUTHORS[1])]

    run(body, backend_dsn)


def test_left_join_none(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        rows = await db.run(select(Author).left_join(Book).order_by(a.id.asc(), b.id.asc()))
        assert [(x.id, y.id if y else None) for x, y in rows] == [(1, 1), (1, 2), (2, 3), (3, None)]
        assert rows[-1][1] is None

    run(body, backend_dsn)


def test_two_joins(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        q = select(Author).join(Book).left_join(Note).order_by(b.id.asc())
        rows: list[tuple[Author, Book, Optional[Note]]] = await db.run(q)
        assert [(x.id, y.id, n.id if n else None) for x, y, n in rows] == [(1, 1, 1), (1, 2, None), (2, 3, None)]

    run(body, backend_dsn)


def test_where_joined_column(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        rows = await db.run(select(Book).join(Author).where(a.name == "bob"))
        assert rows == [(BOOKS[2], AUTHORS[1])]

    run(body, backend_dsn)


def test_order_limit(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        rows = await db.run(select(Book).join(Author).order_by(b.id.desc()).limit(2))
        assert [y.id for y, _ in rows] == [3, 2]

    run(body, backend_dsn)


def test_group_count(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        q = select(Author).join(Book).columns(a.name, count()).group_by(a.name).order_by(a.name.asc())
        assert await db.run(q) == [("ann", 2), ("bob", 1)]

    run(body, backend_dsn)


def test_typed_values(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        book, author = (await db.run(select(Book).join(Author).where(b.id == 3)))[0]
        assert author.ref == U2 and author.active is False
        assert book.day == dt.date(2024, 3, 1)

    run(body, backend_dsn)


def test_first(backend_dsn: str) -> None:
    async def body(db: Database) -> None:
        assert await db.first(select(Book).join(Author).where(b.id == 3)) == (BOOKS[2], AUTHORS[1])
        assert await db.first(select(Book).join(Author).where(b.id == 99)) is None

    run(body, backend_dsn)
