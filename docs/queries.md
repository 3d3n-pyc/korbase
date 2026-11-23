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
You write them as plain Python values: `.where(name="Ada")`. Korbase sends
the SQL text and the values to the database separately, and never pastes a
value into the text. This is what stops
[SQL injection](glossary.md#sql-injection): a name like
`x'; DROP TABLE users; --` is only ever data, so the database never reads it
as a command.

The results are typed. `db.run(select(User))` is a `list[User]` for your
editor and for pyright or mypy, so `user.name` autocompletes and a typo is
flagged before you run anything.

## Run a query

The examples use this model and run inside an `async` function, with a `db`
opened like this. The `users` table must exist: see [Migrations](migrations.md).

```python
import asyncio
import dataclasses
import os
from typing import Annotated, Optional

from korbase import PK, Database, cols, delete, insert, select, table, update, upsert


@table("users")
@dataclasses.dataclass
class User:
    id: Annotated[int, PK()]
    name: str
    age: Optional[int] = None


async def main() -> None:
    async with await Database.connect(os.environ["KORBASE_DSN"]) as db:
        U = cols(User)
        users = await db.run(select(User).where(name="Ada"))
        print(users)  # a list[User]


asyncio.run(main())
```

`cols(User)` gives you the [columns](glossary.md#column) of a model as
attributes: `cols(User).name`. An unknown name raises `KorbaseError`.

## What a query returns

Your editor knows the type of the result from the builder you pass:

```python
users = await db.run(select(User))                      # list[User]
user = await db.first(select(User).where(id=1))         # User | None
changed = await db.run(update(User).set(age=37).where(id=1))   # int
```

| Builder | `db.run()` gives | `db.first()` gives |
|---|---|---|
| `select`, `insert`, `upsert` | `list[User]` | `User \| None` |
| `select(...).columns(...)` | `list[tuple]` | `tuple \| None` |
| `update`, `delete` | `int`, the number of rows changed | not allowed |
| `update` or `delete` with `.returning()` | `list[User]` | `User \| None` |

`db.first()` returns `None` when no row matches, and the type says so: your
editor reminds you to check. On an `update` or `delete` without
`.returning()`, `db.first()` raises `KorbaseError`.

## Filter with where()

`where()` takes conditions, `column=value` pairs, or both. Everything you
give it is joined with AND.

```python
await db.run(select(User).where(id=1))                           # id = 1
await db.run(select(User).where(U.age > 18, U.name.like("A%")))  # both conditions
await db.run(select(User).where(U.age > 18, name="Ada"))         # a condition and a keyword
await db.run(select(User).where(age=None))                       # age IS NULL
```

- A keyword such as `name="Ada"` means "equals". With `None` it means
  `IS NULL`. An unknown column name raises `KorbaseError`.
- A condition is an expression built from a column: `U.age > 18`. A boolean
  column is a condition by itself: `.where(P.published)`, with `P = cols(Post)`
  for a `Post` model that has a `published` field.
- `where()` can be called several times. The conditions are joined with AND.
  `where()` with nothing in it raises `TypeError`.

## Operators

| Python | SQL |
|---|---|
| `col == x`, `col != x` | `=`, `<>` |
| `col == None`, `col != None` | `IS NULL`, `IS NOT NULL` |
| `<`, `<=`, `>`, `>=` | the same operator |
| `col.like(x)`, `col.ilike(x)` | `LIKE`, `ILIKE` (SQLite: `LIKE` for both) |
| `col.is_null()`, `col.is_not_null()` | `IS NULL`, `IS NOT NULL` |
| `col.in_(values)` | `= ANY($n)`, with a list or a `param(...)` (SQLite: `IN (SELECT value FROM json_each(?n))`) |
| `a & b`, `a \| b`, `~a` | `AND`, `OR`, `NOT`, in parentheses |

To say AND, put the conditions side by side in `where()`. You need `&`, `|`
and `~` only for OR, NOT, or a mix: each side goes in parentheses.

```python
adults_or_named = select(User).where((U.age >= 18) | (U.name == "Ada"))
```

Never `and`, `or` or `not`: see [Going further](#why-and-and-or-raise).

## Select

```python
q = (
    select(User)
    .where(U.age > 18, name="bob")
    .order_by(U.name.asc(), U.id.desc())
    .limit(10)
    .offset(20)
)
rows = await db.run(q)
```

- `order_by()` takes `col.asc()`, `col.desc()` or a plain column (ascending).
- `limit()` and `offset()` take an `int` or a `param(...)`.

## Aggregates and grouping

An aggregate turns many rows into one value: how many, the total, the
average. Say you keep a to-do list, and you want the number of open tasks and
their points for each list, biggest first:

```python
from korbase import cols, count, select

T = cols(Task)

per_list = (
    select(Task)
    .columns(T.list_id, count(), T.points.sum())
    .where(done=False)
    .group_by(T.list_id)
    .having(count() > 1)
    .order_by(count().desc())
    .limit(3)
)

rows = await db.run(per_list)   # [(1, 3, 9)]: list 1 has 3 open tasks worth 9 points
```

This example uses a `Task` model with the fields `id`, `list_id`, `done` and
`points`. On Postgres it sends this SQL:

```sql
SELECT "tasks"."list_id", count(*), sum("tasks"."points") FROM "tasks" WHERE "tasks"."done" = $1 GROUP BY "tasks"."list_id" HAVING count(*) > $2 ORDER BY count(*) DESC LIMIT $3
```

On SQLite the SQL is the same, with `?1`, `?2` and `?3` in place of `$1`,
`$2` and `$3`.

`columns()` replaces the whole row with the items you list, so each result
is a tuple, in the order you gave. `group_by()` makes one row per value of the
column. `having()` keeps only the groups that match, as `where()` keeps rows.
`where()` runs first, on rows, and `having()` runs after, on groups.

### The aggregates

| Python | SQL | Result |
|---|---|---|
| `count()` | `count(*)` | `int`, the rows in the group |
| `col.count()` | `count(col)` | `int`, the rows where `col` is not null |
| `col.sum()` | `sum(col)` | the column type, or `None` |
| `col.avg()` | `avg(col)` | always a `float`, or `None` |
| `col.min()`, `col.max()` | `min(col)`, `max(col)` | the column type, or `None` |

`sum`, `avg`, `min` and `max` give `None` when there is nothing to add up,
and `count` gives `0`. Values come back as the column type, not as raw
database values. `min()` of a `date` column is a `date`, also on SQLite,
which stores dates as text. `avg` is a `float` on both databases: Postgres
would otherwise return a `Decimal`, so Korbase casts it
(`CAST(avg(col) AS DOUBLE PRECISION)`). SQLite already returns a float.

An aggregate can go in `having()`, with `>`, `<`, `==` and the other
operators, and in `order_by()`, with `.asc()` or `.desc()`.

### One value

`db.value()` runs a query that selects one column or aggregate, and returns
that single value. It returns `None` when there is no row. A query with
no `group_by()` always returns one row, so `count()` gives `0`, not `None`:

```python
open_tasks = await db.value(select(Task).columns(count()).where(done=False))
best = await db.value(select(Task).columns(T.points.max()))   # int | None
```

For a `sum()` that you want as a number, write `... or 0`. For a `count()`
you do not need it.

With no `group_by()` and no row, `db.first()` still returns a tuple, for
example `(0, None)`. With a `group_by()` and no row, there are no groups:
`db.run()` returns `[]` and `db.first()` returns `None`.

`db.value()` raises `KorbaseError` if the query does not select exactly one
item.

### What Korbase refuses

These raise `KorbaseError` when the query is compiled, which happens the
first time you run it:

- `columns()` with nothing in it.
- A plain column next to an aggregate, when the column is not in
  `group_by()`. The database could not tell which value to show.
- `having()` on a query with no aggregate and no `group_by()`.

Two more limits:

- `.load()` is not available on a `columns()` query. It has no rows of the
  model to fill.
- Postgres has no `min` or `max` for `uuid` columns, so `.min()` and `.max()`
  on one fail in the database.

Aggregates also work over a join: see [Joins](relations.md#joins).

The editor knows the type of each tuple for up to six items in `columns()`.
Calling `columns()` again replaces the list, and clears `group_by()` and
`having()`.

## Insert

```python
[user] = await db.run(insert(User).values(name="Ada", age=36))
```

- `values()` lists the columns to write. A column left out gets its default.
- An insert always returns the written row, with all its columns, so you
  get the `id` the database chose. There is no `.returning()` on `insert`.
  `db.run()` gives a list with one row, and `[user] =` takes it out of the
  list. `db.first()` gives the row directly.

## Update and delete

```python
changed = await db.run(update(User).set(name="Ada", age=37).where(id=1))
removed = await db.run(delete(User).where(id=1))
```

- `update` and `delete` need a `where()`, or the query raises `KorbaseError`.
  To change or remove every row on purpose, call `.all_rows()`.
- `db.run()` returns the number of rows changed, as an `int`: `1` if the
  row existed, `0` if no row matched.
- To get the rows back instead, add `.returning()`. `db.run()` then returns a
  list of models, `db.first()` the first:

```python
[old] = await db.run(delete(User).where(id=1).returning())
```

## Upsert

An upsert inserts a row, or updates the existing one on a conflict. Like an
insert, it always returns the written row.

```python
[user] = await db.run(upsert(User).values(id=1, name="Ada", age=1).on_conflict("id"))
```

- `on_conflict()` names the columns that must clash. It is required.
- The columns in the `SET` list are the inserted columns, minus the conflict
  columns and the primary key. The primary key is never rewritten.
- If nothing is left to set, the upsert does nothing on conflict (`DO NOTHING`).

## Typed columns

`cols(Post).titel` is a typo, and by default nothing tells you until the
query runs. Korbase can generate a small module that tells your editor which
columns each model has and what type each one holds. Then `cols(Post).title`
is a `Column[str]`, and `cols(Post).titel` is an error in pyright or mypy
before you run anything. Comparing a column with the wrong type is an error
too: `cols(Post).id == "3"` when `id` is an `int`, or `like()` on a column
that is not a `str`.

Run the command once, with the module that holds your models and the file to
write:

```sh
python -m korbase.stubs app.models -o app/columns.py
```

Then import `cols` from the generated module instead of from `korbase`:

```python
from app.columns import cols
from app.models import Post
from korbase import select

P = cols(Post)
recent = select(Post).where(P.title.like("A%")).order_by(P.id.desc())
```

At runtime it is the same object as `korbase.cols(Post)`: the generated
module only adds the types. Do not edit it by hand. Rerun the command after
you add, rename or retype a field, and commit the file with your models. To
make sure nobody forgets, add `--check` to your CI:

```sh
python -m korbase.stubs app.models -o app/columns.py --check
```

`--check` writes nothing. It exits with 1 and a message if the file is out of
date.

The generated module has one class per model, found by the names the models
module gives them. It stops with an error if the module has no model, or if
two models share a name. A field type it cannot show, such as a class of your
own, becomes `Any`, so it is never wrongly rejected.

Two limits. Keyword filters, as in `.where(title="x")`, are still checked
only when the query is built: a typo there raises at runtime, not in the
editor. Use `.where(P.title == "x")` when you want the editor to check it.
And `@table` keeps the class type, so `db.run(select(Post))` is a
`list[Post]` even without the generated module.

## Going further

### Parameters: queries built once

A plain value is part of the builder: `.where(name="Ada")` is a query about
Ada, and nothing else. Builders are immutable: `where()` or `limit()`
returns a new builder and leaves the old one as it was. The first time a
builder is needed, it [compiles](glossary.md#compiled-query) to SQL text and
a tuple of slots, and it keeps the result. A builder made inside a function is
compiled again on every call.

For a query that runs many times with different values, build it once at
module level, with [parameters](glossary.md#parameter). A parameter is a
named blank, such as `param("min")`, that you fill in when the query runs.
Pass the values to `db.run()` by name:

```python
from korbase import param

ADULTS = select(User).where(U.age > param("min")).order_by(U.name.asc()).limit(param("n"))

rows = await db.run(ADULTS, min=18, n=10)
rows = await db.run(ADULTS, min=21, n=50)
```

This compiles once and runs as often as you like, which is the fast way.
`param()` works in `where()` conditions, `values()`, `set()`, `limit()` and
`offset()`. A keyword takes one too: `.where(id=param("id"))`.

Postgres takes positional arguments: `$1`, `$2`. SQLite writes `?1`, `?2`
instead; the rest of this page shows Postgres. `compile()` returns the SQL
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


ADULTS = select(User).where(cols(User).age >= param("min"), name="bob").limit(10)

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

A plain value and a `param(...)` give the same SQL: each one is a `$n`.

```sql
SELECT "users"."id", "users"."name", "users"."age" FROM "users" WHERE ("users"."age" > $1 AND "users"."name" = $2) ORDER BY "users"."name" ASC, "users"."id" DESC LIMIT $3 OFFSET $4
INSERT INTO "users" ("name", "age") VALUES ($1, $2) RETURNING "id", "name", "age"
UPDATE "users" SET "name" = $1, "age" = $2 WHERE "users"."id" = $3
DELETE FROM "users" WHERE "users"."id" = $1
INSERT INTO "users" ("id", "name", "age") VALUES ($1, $2, $3) ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name", "age" = EXCLUDED."age" RETURNING "id", "name", "age"
```

### NULL: `None`, `== None` and `is_null()`

In SQL, `= NULL` never matches anything. So Korbase handles `None` for you
where it can, and refuses where it cannot:

- A literal `None` becomes `IS NULL`: `.where(age=None)`, `U.age == None` and
  `U.age.is_null()` mean the same. A variable that may be `None` works too:
  `.where(age=maybe_age)`, because the choice is made when you build the query.
- A `param(...)` always compiles to `=`, and its value is only known later. If
  you run the query with `None` for a parameter compared by `==`, `!=` or
  `in_()`, `db.run()` raises `KorbaseError` instead of silently matching no
  row. For a value that may be `None`, use a plain value or `.is_null()`.

### Why `and` and `or` raise

Python decides `a and b` by turning `a` into `True` or `False`. A Korbase
condition is a piece of SQL, not a boolean, so it cannot answer. It raises
`TypeError` instead of silently dropping half of your condition. Use `&`, `|`
and `~`, or several conditions in `where()`. This also stops `1 < U.age < 5`,
which Python reads as an `and`.

**Next:** [Relations](relations.md)
