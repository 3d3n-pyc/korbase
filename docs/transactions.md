# Transactions and queries

**Use this when** you need to open a database, run queries, and make several writes succeed or fail together.

## The idea

A [transaction](glossary.md#transaction) groups several statements into one unit: all of them happen, or none do. Take a bank transfer. You take 10 from account A, then add 10 to account B. If the program crashes between the two steps, the money is gone. Put both steps in one transaction and a crash undoes the first step. This undo is called a [rollback](glossary.md#rollback). If no error occurs, the transaction commits and both changes become permanent.

A [savepoint](glossary.md#savepoint) is a bookmark inside a transaction. If a step fails, you go back to the bookmark and keep everything done before it. In korbase, a transaction opened inside a transaction is a savepoint.

Most of the time you build queries with `select`, `insert`, `update`, `delete` and `upsert`. Use raw SQL when the query is something they cannot say, such as `balance = balance - 10` or a `CASE` expression. You lose the checks, but you can run any SQL.

## A first run

`Database` holds a [pool of connections](glossary.md#connection-pool). Each query borrows one for the length of the call. A `Transaction` holds one connection for every query inside its block.

```python
import asyncio
import os
from typing import Annotated

import msgspec

from korbase import PK, Database, insert, select, table, update


@table("users")
class User(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str


async def main() -> None:
    db = await Database.connect(os.environ["KORBASE_DSN"])
    await db.execute("CREATE TABLE IF NOT EXISTS users (id integer PRIMARY KEY, name text NOT NULL)")

    async with db.transaction() as tx:
        await tx.run(insert(User).values(id=3, name="Cy"))
        changed = await tx.run(update(User).set(name="Carl").where(id=3))
        print(changed)  # 1, the number of rows updated

    carl = await db.first(select(User).where(name="Carl"))
    print(carl)  # User(id=3, name='Carl')

    await db.execute("DROP TABLE users")
    await db.close()


asyncio.run(main())
```

## Connect and close

- `Database.connect(dsn)` opens the pool. A `postgresql://` DSN uses asyncpg, installed with `pip install --pre "korbase[asyncpg]"`. A `sqlite:` DSN uses the standard library `sqlite3` module and needs nothing more (see [SQLite](sqlite.md)). Pass `backend=` to use another [backend](glossary.md#backend).
- Extra keyword arguments go to the backend. With asyncpg they go to `asyncpg.create_pool`, for example `min_size=1, max_size=10`. With SQLite they are `size=` and `busy_timeout_ms=`.
- `validate=True` checks every row against its model with msgspec. It is off by default.
- `await db.close()` closes the pool. `async with await Database.connect(dsn) as db:` closes it on exit.

## Queries

Build a query, then run it with one of these methods:

- `db.run(query, **params)` returns a list of models for a select, an insert and an upsert, which always return the rows they wrote. An update or a delete returns the number of rows it changed, as an `int`. Add `.returning()` to an update or a delete to get the rows instead.
- `db.first(query, **params)` returns the first model, or `None`. It works on any query that returns rows. An update or a delete without `.returning()` raises `KorbaseError`.

The result is typed: `run(select(User))` is a `list[User]` for your type checker, and `first(...)` is `User | None`.

Plain values go straight into the query. When the value is only known at run time, use a named parameter, `param("name")`, and give it as a keyword to `run`. Every parameter must be given, and no extra one may be. A missing or extra name raises `KorbaseError` before anything is sent.

```python
from korbase import cols, delete, param

u = cols(User)

new = await db.run(insert(User).values(id=4, name="Dee"))  # [User(id=4, name='Dee')]
n = await db.run(update(User).set(name="Bob").where(id=4))  # 1
gone = await db.run(delete(User).where(id=4).returning())  # [User(id=4, name='Bob')]

rows = await db.run(select(User).where(u.id >= param("min_id")), min_id=1)
count = await db.run(update(User).set(name="Bob").where(u.id == param("id")), id=2)  # 1
```

`where` takes conditions, keyword pairs, or both, all joined with AND: `.where(u.id > 1, name="Bob")`. A keyword set to `None` means `IS NULL`.

A `param` compared with `==`, `!=` or `in_()` cannot be given `None`: such a comparison never matches NULL, so `run` raises `KorbaseError` instead of returning nothing. Use `.where(column=None)` or `.is_null()` for a NULL test.

## Transactions

`db.transaction()` opens a transaction on one connection. The block commits when it ends without an error. It rolls back when an exception leaves it, and the exception is raised again. Here is the transfer from above:

```python
async with db.transaction() as tx:
    await tx.execute("UPDATE accounts SET balance = balance - $1 WHERE id = $2", 10, 1)
    await tx.execute("UPDATE accounts SET balance = balance + $1 WHERE id = $2", 10, 2)
```

If the second statement fails, the first is undone.

Use `tx`, not `db`, for the queries inside the block. A call on `db` borrows another connection, so it runs outside the transaction.

`Transaction` has the same query methods as `Database`: `run`, `first`, `fetch`, `fetchrow` and `execute`. It also has `transaction()`, which opens a savepoint. A failure inside it rolls back only that part. The outer block carries on:

```python
async with db.transaction() as tx:
    await tx.run(insert(User).values(id=1, name="a"))
    try:
        async with tx.transaction() as inner:  # a savepoint
            await inner.run(insert(User).values(id=2, name="b"))
            raise RuntimeError("undo the second insert only")
    except RuntimeError:
        pass
    # User 1 is kept. User 2 was rolled back to the savepoint.
```

## Raw SQL

Write `$1`, `$2` placeholders (on SQLite, `?` or `?1`, `?2`). Only the values are sent as arguments. The SQL text itself is not checked, so never build it from user input.

```python
await db.execute("INSERT INTO users VALUES ($1, $2)", 1, "a")  # "INSERT 0 1"

rows = await db.fetch("SELECT id, name FROM users WHERE id > $1", 0)  # list of tuples
row = await db.fetchrow("SELECT id, name FROM users WHERE id = $1", 5)  # tuple or None

users = await db.fetch("SELECT * FROM users", model=User)  # list of models
one = await db.fetchrow("SELECT * FROM users WHERE id = $1", 1, model=User)  # model or None
```

`fetch` with no rows returns `[]`, and `fetchrow` returns `None`. `execute` returns the command tag. With `model=User` the result is typed as `list[User]` or `User | None`. The same methods exist on `Transaction`.

## Going further

- **Nested savepoints.** `tx.transaction()` can be nested as deep as you like. Each level is its own savepoint, and an error rolls back to the nearest one. If you do not catch the error, it moves up and rolls back the next level too.
- **One connection per transaction.** `db.transaction()` takes a connection from the pool and keeps it until the block ends. Keep blocks short, and do not wait on slow things (an HTTP call, user input) inside one. If every connection is in use, the next caller waits. Size `max_size` for the transactions you expect at the same time.
- **`model=` on raw SQL.** Columns are matched to fields by name, not by position. Extra columns are ignored. A required field with no matching column raises `KorbaseError`. A TypedDict model gives dicts. Alias columns in the SQL (`SELECT full_name AS name`) when the names differ.
- **`validate=True` costs time.** Each row goes through `msgspec.convert`, and a wrong type raises `msgspec.ValidationError`. The same check applies to raw rows with `model=`. It needs msgspec installed. Keep the default when the database already guarantees the types, and measure before you turn it on for hot queries.
- **Tests.** The tests run against a real Postgres. Start one with `bash tests/pg/start.sh`, which prints the export line for `KORBASE_TEST_DSN`. The tests for this page are in `tests/test_db.py`.

**Next:** [Migrations](migrations.md)
