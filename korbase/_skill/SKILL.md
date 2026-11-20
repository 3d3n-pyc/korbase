---
name: korbase
description: Write Postgres data access with korbase, the async ORM built the Korvex way. Use when code imports korbase, or when asked to add models, queries, relations or migrations to a Korvex app.
---

# korbase

korbase is an async Postgres layer: models are plain classes, queries are
immutable builders you define once and run with parameters. Python 3.10 or
later. The API can change before 1.0: check `korbase.__version__`.

Install with `pip install korbase[asyncpg] msgspec`. Add the `korvex`
extra to use it inside a Korvex app.

## Models

A model is a msgspec `Struct`, a dataclass or a `TypedDict`, decorated with
`@table`. Markers go in `Annotated`: `PK()`, `FK("users.id")`, `Unique()`,
`Default(0)` or `Default(sql="now()")`, `SqlType("varchar(80)")`. Table
options: `@table("name", indexes=(Index("a", "b", unique=True),))`. A
single integer `PK()` is an identity column. `Optional[X]` means nullable.

```python
from typing import Annotated, Optional

import msgspec

from korbase import FK, PK, Rel, Unique, table


@table("authors")
class Author(msgspec.Struct):
    id: Annotated[int, PK()]
    name: Annotated[str, Unique()]
    books: Annotated[list["Book"], Rel("author_id")] = msgspec.field(default_factory=list)


@table("books")
class Book(msgspec.Struct):
    id: Annotated[int, PK()]
    author_id: Annotated[int, FK("authors.id")]
    title: str
```

A `Rel("fk_field")` field is not a column: it holds related rows filled by
`.load(...)`. Put `Rel` on the side that names the foreign key column.

## Queries

Build queries at module level, once. `cols(Model)` gives columns,
`param("x")` a named parameter filled at run time. Combine conditions with
`&`, `|`, `~`. `.where(*conditions, **equals)` ANDs several conditions and
`column=value` pairs; a plain value is fine when it is fixed, and a keyword
set to `None` means `IS NULL`.

```python
from korbase import cols, delete, insert, param, select, update, upsert

b = cols(Book)
BY_AUTHOR = select(Book).where(b.title != "", author_id=param("a")).order_by(b.id.asc()).limit(10)
NEW_BOOK = insert(Book).values(title=param("t"), author_id=param("a"))
RETITLE = update(Book).set(title=param("t")).where(id=param("id"))
UNTITLED = delete(Book).where(b.title.is_null() | (b.title == ""))
```

Also `.offset()`, `like`, `ilike`, `in_`, `is_not_null`, `.desc()`.
`upsert(M).on_conflict("col").values(...)` updates the other columns on a
clash. Guards: `update` and `delete` raise without `.where(...)` unless you
call `.all_rows()`; `insert` and `upsert` need `.values(...)`.

## Running

```python
db = await Database.connect(dsn)            # a pool; `async with` closes it
books = await db.run(BY_AUTHOR, a=1)        # select: list[Book]
[book] = await db.run(NEW_BOOK, t="Dune", a=1)  # insert/upsert: the new rows
one = await db.first(BY_AUTHOR, a=1)        # first row or None
n = await db.run(RETITLE, t="Dune II", id=1)    # update/delete: an int row count
gone = await db.run(delete(Book).where(id=1).returning())  # rows instead of a count
```

`run` and `first` take parameters as keywords. Results are typed by the
model. `insert` and `upsert` always return rows. `update` and `delete`
return the number of rows changed; add `.returning()` to get the rows
(`first` needs it too). A `param` compared with `==`, `!=` or `in_()` cannot
be `None`: `run` raises. Use `.where(col=None)` or `.is_null()`.

## Relations

`select(Author).load(cols(Author).books)` runs one extra query per
relation (not one per row) and fills the field. Loads work both ways
(one-to-many and many-to-one), on `first` too, and inside transactions.
Composite primary keys are not supported by `.load`.

## Transactions and raw SQL

```python
async with db.transaction() as tx:   # commits, or rolls back on an exception
    await tx.run(NEW_BOOK, t="A", a=1)
```

`tx` has the same `run`, `first`, `fetch`, `fetchrow`, `execute` as `db`.
Raw SQL: `await db.fetch("SELECT ... WHERE x = $1", 5, model=Book)` maps
columns to the model by name; without `model` rows are tuples. Always pass
values as `$1` arguments, never format them into the string.

## Migrations

`create_all(db, models)` (from `korbase.migrate`) creates tables: use it in
tests. For real databases use files:

1. `KORBASE_DSN=... python -m korbase.migrate make --models app.models --dir migrations -m add_books`
   compares the models' tables with the database and writes
   `0001_add_books.sql`. Other tables are ignored; add `--all-tables` to
   compare the whole schema, which is the only way to drop the table of a
   removed model.
2. Review the file. Lines `-- korbase:review ...` flag dangerous changes
   (dropped data, new NOT NULL). Edit the SQL if needed, then delete each
   review line once you have read it: `apply` refuses files that keep one.
3. `python -m korbase.migrate apply --dir migrations` runs pending files;
   `status` lists them.

For local work, `python -m korbase.migrate dev --models app.models --dir
migrations` runs make then apply in one step (stops on a review line).
Never use it on production.

Never edit or delete an applied file: `apply` refuses a changed one. Fix
mistakes with a new migration. Prefer `KORBASE_DSN` to `--dsn` (visible in
`ps`).

## Korvex

```python
from korvex import App, Depends

from korbase.korvex import install

app = App()
kdb = install(app, "postgresql://user:pass@localhost/db")  # opens/closes the pool


@app.get("/books/{id}")
async def get_book(id: int, tx=Depends(kdb.transaction)):
    return await tx.first(select(Book).where(cols(Book).id == param("i")), i=id)
```

`Depends(kdb.database)` gives the `Database`; `Depends(kdb.transaction)` a
`Transaction` per request, committed unless the handler raises.

## Common mistakes

- `and` / `or` / `not` on conditions: they raise. Use `&`, `|`, `~`, and
  parenthesize each side.
- `col == None`: use `col.is_null()`. A `None` given to a `param` compared
  with `==` raises: use `.where(col=None)`.
- `update` and `delete` return a count (an `int`), not rows: add
  `.returning()` when you need the rows. `insert` and `upsert` have no
  `.returning()`; they always return rows.
- Expecting `make` to drop a removed model's table: it only diffs the
  models' tables; use `--all-tables`.
- Building queries inside a hot loop or handler: build once at module level.
- Editing an applied migration, or applying one with a review line left.
- Formatting values into raw SQL: pass `$1` arguments.

## A complete script

```python
import asyncio
import os

from korbase import Database, cols, insert, param, select
from korbase.migrate import create_all

b = cols(Book)
ADD_AUTHOR = insert(Author).values(name=param("n"))
ADD_BOOK = insert(Book).values(title=param("t"), author_id=param("a"))
AUTHORS = select(Author).order_by(cols(Author).id.asc()).load(cols(Author).books)


async def main(dsn: str) -> list[Author]:
    async with await Database.connect(dsn) as db:
        await create_all(db, [Author, Book])
        async with db.transaction() as tx:
            ada = await tx.first(ADD_AUTHOR, n="Ada")
            await tx.run(ADD_BOOK, t="Notes", a=ada.id)
        return await db.run(AUTHORS)


if __name__ == "__main__":
    print(asyncio.run(main(os.environ["KORBASE_DSN"])))
```

Its check:

```python
def test_script(dsn):
    [ada] = asyncio.run(main(dsn))
    assert ada.name == "Ada" and [x.title for x in ada.books] == ["Notes"]
```
