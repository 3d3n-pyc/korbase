# korbase

An async ORM for Postgres, built the Korvex way. It is pure Python. Pre-alpha: the API may change, and there are no migrations yet. Models are msgspec Structs, dataclasses or TypedDicts. Driver: asyncpg. Python 3.10 and later. Install with `pip install korbase[asyncpg]`.

## Example

It creates two tables in plain SQL, so run it on a scratch database and replace the DSN.

```python
from __future__ import annotations
import asyncio
from typing import Annotated, Optional

import msgspec
from korbase import FK, PK, Database, Rel, cols, delete, insert, param, select, table, update

DSN = "postgresql://user:password@localhost:5432/scratch"

@table("users")
class User(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str
    posts: Annotated[list[Post], Rel("author_id")] = msgspec.field(default_factory=list)

@table("posts")
class Post(msgspec.Struct):
    id: Annotated[int, PK()]
    author_id: Annotated[int, FK("users.id")]
    title: str
    author: Annotated[Optional[User], Rel("author_id")] = None

async def main() -> None:
    async with await Database.connect(DSN) as db:
        await db.execute(
            """
            DROP TABLE IF EXISTS posts; DROP TABLE IF EXISTS users;
            CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL);
            CREATE TABLE posts (id int PRIMARY KEY, author_id int NOT NULL REFERENCES users(id), title text NOT NULL);
            """
        )
        u, p = cols(User), cols(Post)

        # Insert, and get the new row back.
        ada = await db.run(insert(User).values(id=1, name="Ada").returning())
        print(ada)  # [User(id=1, name='Ada', posts=[])]
        await db.run(insert(User).values(id=2, name="Bob"))
        await db.run(insert(Post).values(id=1, author_id=1, title="First"))
        await db.run(insert(Post).values(id=2, author_id=2, title="Second"))

        # Filter, order and limit. Values are passed as params.
        found = await db.run(
            select(User).where(u.id >= param("min_id")).order_by(u.name.asc()).limit(param("n")),
            min_id=1,
            n=10,
        )
        print([x.name for x in found])  # ['Ada', 'Bob']

        # One row, or None.
        bob = await db.first(select(User).where(u.name == param("name")), name="Bob")
        print(bob)  # User(id=2, name='Bob', posts=[])

        # Load a relation. Each relation costs one extra query.
        posts = await db.run(select(Post).order_by(p.id.asc()).load(p.author))
        print([(x.title, x.author.name if x.author else None) for x in posts])

        # A transaction commits on success and rolls back if the block raises.
        async with db.transaction() as tx:
            await tx.run(insert(User).values(id=3, name="Cy"))
            await tx.run(update(User).set(name="Carl").where(u.id == 3))

        await db.run(delete(Post).where(p.id == param("id")), id=2)

        # Raw SQL. Rows come back as models, matched by column name.
        rows = await db.fetch("SELECT id, name FROM users WHERE id = $1", 1, model=User)
        print(rows)  # [User(id=1, name='Ada', posts=[])]

asyncio.run(main())
```

## Models and queries

- `PK()`, `FK("table.column")`, `Unique()` and `Default(...)` go inside `Annotated[...]`. Every table needs a primary key.
- `Rel("fk_field")` marks a relation. `list[Model]` is one-to-many, a single model is many-to-one. Declare relation fields after the columns.
- `select`, `insert`, `update`, `delete` and `upsert` build a query. `db.run(query, **params)` runs it. `insert` and `upsert` return rows with `.returning()`. Other writes return the command tag, such as `"UPDATE 1"`. `db.first(query, **params)` returns one model or `None`.
- `db.fetch`, `db.fetchrow` and `db.execute` take raw SQL with `$1, $2` arguments. A transaction inside a transaction is a savepoint.
- `col == None` compiles to `IS NULL`. Prefer `.is_null()`. `Database.connect(dsn, validate=True)` checks each row with msgspec; it is off by default.

## How SQL stays safe

Table and column names must match `[A-Za-z_][A-Za-z0-9_]{0,62}`. They are double-quoted, and anything else raises `KorbaseError`. Columns in `insert`, `update` and `upsert` must belong to the model. Every value, including `limit` and `offset`, is a `$n` placeholder, never written into the SQL text. `update` and `delete` need `.where(...)`, or `.all_rows()` to confirm. Raw SQL is not checked, only its values are sent as arguments.

## Korvex

`korbase.korvex.install` opens the pool when the app starts and closes it when the app stops. Handlers get the database or a per-request transaction through `Depends`, and a transaction commits unless the handler raises. Install with `pip install korbase[korvex]`.

```python
from __future__ import annotations
from typing import Annotated

from korvex import App, Depends, Response
from korbase import Database, cols, select
from korbase.korvex import install
# DSN and User come from the example above.
app = App()
kdb = install(app, DSN)  # module level: the handler annotations need it

@app.get("/users")
async def list_users(db: Annotated[Database, Depends(kdb.database)]) -> Response:
    users = await db.run(select(User).order_by(cols(User).id.asc()))
    return Response.json([x.name for x in users])
```

## Not there yet

- Migrations and `create_all`. `Unique`, `Default` and `Index` are recorded on the model, but nothing uses them yet. `Index` goes in `@table("name", indexes=(Index("col"),))`.
- SQLite, joins in the query builder, and the korpg driver (its extra is empty). Relations on composite primary keys: `.load(...)` raises `KorbaseError`.

## Tests

`eval "$(bash tests/pg/start.sh | grep '^export')"` starts Postgres in Docker on port 55433, then `uv run pytest`. Rules: [AGENTS.md](AGENTS.md). Changes: [CHANGELOG.md](CHANGELOG.md).
