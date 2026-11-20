# Queries

**Use this when** you want to read, add, change or delete rows from Python,
without writing SQL by hand.

## The idea

A query is a request to the database: "give me the users older than 18", or
"add this user". Databases read these requests in a language called SQL.
Korbase builds the SQL for you. You call `select`, `insert`, `update`,
`delete` or `upsert` on a [model](glossary.md#model), and nothing touches the
database until you pass the result to `db.run()` or `db.first()`.

Most requests contain values that change: the age, the name typed by a user.
A [parameter](glossary.md#parameter) is a named blank, such as
`param("min")`, that you fill in when the query runs. Korbase sends the SQL
text and the values to the database separately, and never pastes a value into
the text. This is what stops [SQL injection](glossary.md#sql-injection): a
name like `x'; DROP TABLE users; --` is only ever data, so the database never
reads it as a command.

## Run a query

The examples use this model and run inside an `async` function, with a `db`
opened like this. The `users` table must exist: see [Migrations](migrations.md).

```python
import asyncio
import dataclasses
import os
from typing import Annotated, Optional

from korbase import PK, Database, cols, delete, insert, param, select, table, update, upsert


@table("users")
@dataclasses.dataclass
class User:
    id: Annotated[int, PK()]
    name: str
    age: Optional[int] = None


async def main() -> None:
    async with await Database.connect(os.environ["KORBASE_DSN"]) as db:
        U = cols(User)
        print(await db.run(select(User).where(U.name == param("name")), name="Ada"))


asyncio.run(main())
```

`cols(User)` gives you the [columns](glossary.md#column) of a model as
attributes: `cols(User).name`. An unknown name raises `KorbaseError`.
`db.run()` returns a list of models, `db.first()` the first one or `None`.
A plain value in a condition, such as `"bob"` in `U.name == "bob"`, is also
sent as a parameter.

## Operators

| Python | SQL |
|---|---|
| `col == x`, `col != x` | `=`, `<>` |
| `col == None`, `col != None` | `IS NULL`, `IS NOT NULL` |
| `<`, `<=`, `>`, `>=` | the same operator |
| `col.like(x)`, `col.ilike(x)` | `LIKE`, `ILIKE` |
| `col.is_null()`, `col.is_not_null()` | `IS NULL`, `IS NOT NULL` |
| `col.in_(values)` | `= ANY($n)`, with a list or a `param(...)` |
| `a & b`, `a \| b`, `~a` | `AND`, `OR`, `NOT`, in parentheses |

Combine conditions with `&`, `|` and `~`, each side in parentheses:
`(U.age > 18) & (U.name == "bob")`. Never `and`, `or` or `not`.

## Select

```python
q = (
    select(User)
    .where(U.age > param("min"))
    .where(U.name == "bob")
    .order_by(U.name.asc(), U.id.desc())
    .limit(10)
    .offset(param("off"))
)
rows = await db.run(q, min=18, off=0)
```

- `where()` can be called several times. The conditions are joined with AND.
- `order_by()` takes `col.asc()`, `col.desc()` or a plain column (ascending).
- `limit()` and `offset()` take an `int` or a `param(...)`.

## Insert

```python
q = insert(User).values(name=param("name"), age=param("age")).returning()
user = await db.first(q, name="Ada", age=36)
```

- `values()` lists the columns to write. A column left out gets its default.
- `returning()` makes `db.run()` return a list of the written rows, and
  `db.first()` return the first. Without it, `db.run()` returns the command tag.

## Update and delete

```python
q = update(User).set(name="Ada", age=param("age")).where(U.id == param("id"))
await db.run(q, age=37, id=1)

await db.run(delete(User).where(U.id == param("id")), id=1)
```

- `update` and `delete` need a `where()`, or the query raises `KorbaseError`.
  To change or remove every row on purpose, call `.all_rows()`.
- Neither returns rows. `db.run()` returns the command tag.

## Upsert

An upsert inserts a row, or updates the existing one on a conflict.

```python
q = upsert(User).values(id=param("id"), name=param("name"), age=1).on_conflict("id").returning()
user = await db.first(q, id=1, name="Ada")
```

- `on_conflict()` names the columns that must clash. It is required.
- The columns in the `SET` list are the inserted columns, minus the conflict
  columns and the primary key. The primary key is never rewritten.
- If nothing is left to set, the upsert does nothing on conflict (`DO NOTHING`).

## Going further

### Compiled once, with slots

Builders are immutable: `where()` or `limit()` returns a new builder and
leaves the old one as it was. The first time a builder is needed, it
[compiles](glossary.md#compiled-query) to SQL text and a tuple of slots, and
it keeps the result. Build your queries once, at module level, and pass them
to `db.run()` as often as you like. A builder made inside a function is
compiled again on every call.

Postgres takes positional arguments: `$1`, `$2`. `compile()` returns the SQL
and one slot per `$n`, in order. A slot is a `param(...)` or a fixed value.

```python
import dataclasses
from typing import Annotated, Optional

from korbase import PK, cols, param, select, table


@table("users")
@dataclasses.dataclass
class User:
    id: Annotated[int, PK()]
    name: str
    age: Optional[int] = None


ADULTS = select(User).where(cols(User).age >= param("min")).where(cols(User).name == "bob").limit(10)

c = ADULTS.compile()
print(c.sql)
print([s.name if hasattr(s, "name") else s.value for s in c.slots])
```

It prints the SQL, then the slots `['min', 'bob', 10]`:

```sql
SELECT "users"."id", "users"."name", "users"."age" FROM "users" WHERE ("users"."age" >= $1 AND "users"."name" = $2) LIMIT $3
```

- The same named parameter used twice gets the same `$n`.
- `db.run(q, **params)` builds the argument list from the slots. A missing or
  an unexpected parameter name raises `KorbaseError`.

### The SQL behind each builder

```sql
SELECT "users"."id", "users"."name", "users"."age" FROM "users" WHERE ("users"."age" > $1 AND "users"."name" = $2) ORDER BY "users"."name" ASC, "users"."id" DESC LIMIT $3 OFFSET $4
INSERT INTO "users" ("name", "age") VALUES ($1, $2) RETURNING "id", "name", "age"
UPDATE "users" SET "name" = $1, "age" = $2 WHERE "users"."id" = $3
DELETE FROM "users" WHERE "users"."id" = $1
INSERT INTO "users" ("id", "name", "age") VALUES ($1, $2, $3) ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name", "age" = EXCLUDED."age" RETURNING "id", "name", "age"
```

### NULL: `== None` and `is_null()`

Only a literal `None` becomes `IS NULL`. `col == param("x")` always compiles
to `=`, and in SQL `= NULL` never matches anything, even when you pass
`None`. For a nullable column and a value that may be `None`, write
`col.is_null()`, which says what it means.

### Why `and` and `or` raise

Python decides `a and b` by turning `a` into `True` or `False`. A Korbase
condition is a piece of SQL, not a boolean, so it cannot answer. It raises
`TypeError` instead of silently dropping half of your condition. Use `&`, `|`
and `~`. This also stops `1 < U.age < 5`, which Python reads as an `and`.

**Next:** [Relations](relations.md)
