# korbase

An async ORM for Postgres, built the Korvex way. It is pure Python.

korbase is pre-alpha. The API may still change, and there are no
migrations yet.

- **Models** are msgspec Structs, dataclasses or TypedDicts.
- **Driver**: asyncpg, installed with the `asyncpg` extra. A korpg driver is planned.
- **Korvex**: optional integration, installed with the `korvex` extra.
- **Python**: 3.10 and later.

## Install

```sh
pip install korbase[asyncpg]
```

## Example

This example drops and recreates two tables, so run it on a scratch
database. Replace the DSN with your own.

```python
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
        # Korbase does not create tables yet, so the schema is plain SQL.
        await db.execute(
            """
            DROP TABLE IF EXISTS posts; DROP TABLE IF EXISTS users;
            CREATE TABLE users (id int PRIMARY KEY, name text NOT NULL);
            CREATE TABLE posts (
                id int PRIMARY KEY,
                author_id int NOT NULL REFERENCES users(id),
                title text NOT NULL
            );
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

## Models

- `msgspec.Struct`, `dataclass` and `TypedDict` all work the same way.
  A `TypedDict` row comes back as a `dict`.
- Put `PK()`, `FK("table.column")`, `Unique()` or `Default(...)` inside
  `Annotated[...]`. Indexes go in `@table("name", indexes=(Index("col"),))`.
  Every table needs a primary key.
- A relation is a field marked `Rel("fk_field")`. A `list[Model]` relation is
  one-to-many. A single model relation is many-to-one.
- Declare relation fields after the columns. A dataclass with a relation
  between two columns cannot be read back without validation.
- `Database.connect(dsn, validate=True)` checks every row against its model
  with msgspec. It is off by default, and it adds work per row.

## Queries

- `select(Model)`, `insert(Model)`, `update(Model)`, `delete(Model)` and
  `upsert(Model)` build a query. Run it with `db.run(query, **params)`.
- `select` returns a list of models. `insert` and `upsert` return a list when
  you add `.returning()`. Other writes return the command tag, such as `"UPDATE 1"`.
- `update` and `delete` need a `.where(...)`, or `.all_rows()` to confirm.
- `db.first(query, **params)` returns one model or `None`.
- `db.fetch`, `db.fetchrow` and `db.execute` run raw SQL with `$1, $2` arguments.
- A transaction inside a transaction is a savepoint.

## How SQL stays safe

- Table and column names must match `[A-Za-z_][A-Za-z0-9_]{0,62}`. They are
  double-quoted. Anything else raises `KorbaseError`.
- Every value, including `limit` and `offset`, is sent as a `$n` placeholder.
  No value is written into the SQL text.
- Column names in `insert`, `update` and `upsert` must be columns of the model.
- Raw SQL is not checked. Only its values are sent as arguments.

## Korvex

`korbase.korvex.install` opens the pool when the app starts and closes it when
the app stops. Handlers get the database or a per-request transaction through
`Depends`. A transaction commits unless the handler raises.

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

Install with `pip install korbase[korvex]`.

## Not there yet

- Migrations and `create_all`. `Unique`, `Default` and `Index` are recorded
  on the model, but nothing uses them yet.
- SQLite.
- Joins in the query builder.
- The korpg driver. The `korpg` extra is empty.
- Relations on composite primary keys. `.load(...)` raises `KorbaseError`.

## Tests

Tests need a Postgres server. `tests/pg/start.sh` starts one in Docker on
port 55433 and prints the DSN:

```sh
eval "$(bash tests/pg/start.sh | grep '^export')"
uv run pytest
```

## Contributing

See [AGENTS.md](AGENTS.md) for the rules and the checks CI runs. Every
change is listed in [CHANGELOG.md](CHANGELOG.md).
