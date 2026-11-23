# Relations

**Use this when** you want a row to carry the rows it is linked to: a post
with its author, or a user with their posts.

## The idea

Two tables are linked by a [foreign key](glossary.md#foreign-key): a post
stores the `id` of its author. A [relation](glossary.md#relation) is a field on
your model that holds the linked rows themselves, so `post.author.name` works
and you do not look the author up by hand. A relation is not a column, so
nothing is stored for it.

Loading links badly causes the [N+1 queries](glossary.md#n1-queries) problem.
Say you fetch 100 posts (1 query), then ask for the author of each post (100
more queries). That is 101 round trips to the database, and it gets slower as
the list grows. Korbase avoids it: a relation is filled by one extra query for
all the rows at once, so 100 posts with their authors take 2 queries. A
relation is loaded only when you ask for it with `.load()`. To read columns
of two tables in one query instead, see [Joins](#joins).

## Declare a relation

`Rel("...")` names the foreign key field that links the two tables.

```python
from __future__ import annotations

import dataclasses
from typing import Annotated, Optional

from korbase import FK, PK, Rel, table


@table("users")
@dataclasses.dataclass
class User:
    id: Annotated[int, PK()]
    name: str
    posts: Annotated[list[Post], Rel("author_id")] = dataclasses.field(default_factory=list)


@table("posts")
@dataclasses.dataclass
class Post:
    id: Annotated[int, PK()]
    author_id: Annotated[Optional[int], FK("users.id")]
    title: str
    author: Annotated[Optional[User], Rel("author_id")] = None
```

- **Many-to-one**: `Post.author` is one `User`, or `None`. The foreign key
  `author_id` is a field of `Post`.
- **One-to-many**: `User.posts` is a `list[Post]`. The foreign key `author_id`
  is still a field of `Post`.

Both sides use the same `Rel("author_id")`, because it names the same foreign
key. `Post` is used in `User` before it is defined. The `from __future__`
import makes that work.

## Load a relation

Ask for a relation with `.load()` on a query. Each relation you name is filled
in on every returned row. Add this to the same file. It needs the `users` and
`posts` tables: see [Migrations](migrations.md).

```python
import asyncio
import os

from korbase import Database, cols, select


async def main() -> None:
    async with await Database.connect(os.environ["KORBASE_DSN"]) as db:
        U, P = cols(User), cols(Post)
        users = await db.run(select(User).load(U.posts))
        posts = await db.run(select(Post).order_by(P.id.asc()).load(P.author))
        for post in posts:
            print(post.title, "by", post.author.name if post.author else "nobody")
        print([len(u.posts) for u in users])


asyncio.run(main())
```

- A relation you did not load keeps its default. Give each relation field a
  default, as the models do.
- A many-to-one field is `None` when the foreign key is `NULL` or no row
  matches. A one-to-many field is an empty list when nothing matches.
- `db.first()` takes `.load()` too, and so does `tx.run()` inside a
  [transaction](transactions.md).
- Nothing is loaded when you read the attribute. There is no lazy loading.

## Joins

**Use this when** you want columns of two or three tables side by side, in one
query: each post with its author, or each user with their posts, flat.

`.join(Model)` adds a table to a `select`. Each row comes back as a tuple, one
model per table. This example uses the `User` and `Post` models above.

```python
from korbase import cols, select

U, P = cols(User), cols(Post)

rows = await db.run(select(Post).join(User).order_by(P.id.asc()))
for post, user in rows:
    print(post.title, "by", user.name)
```

`rows` is a `list[tuple[Post, User]]`, and the editor knows it. On Postgres,
Korbase sends one query:

```sql
SELECT "posts"."id", "posts"."author_id", "posts"."title", "users"."id", "users"."name" FROM "posts" JOIN "users" ON "posts"."author_id" = "users"."id" ORDER BY "posts"."id" ASC
```

On SQLite the SQL is the same, with `?1`, `?2` in place of `$1`, `$2`. A join
is an `INNER JOIN`: a post with no author (`author_id` is `NULL`) is not in
the result.

### Keep the rows with no match

`.left_join(Model)` keeps every row of the first table. When nothing matches,
the joined model is `None`, and the editor types it as `Model | None`.

```python
rows = await db.run(select(User).left_join(Post).order_by(U.id.asc(), P.id.asc()))
for user, post in rows:
    print(user.name, post.title if post else "no post")
```

A user with three posts is three rows, and a user with none is one row with
`post` set to `None`. Korbase decides a model is missing when all of its
primary key columns are `NULL`.

With `.columns()` after a `left_join`, columns of the joined table can be
`None` even though they are typed as the column type, because types cannot see
the join side: `select(User).left_join(Post).columns(U.name, P.title)` types
`title` as `str`, but it is `None` for a user with no post.

### Which columns are joined

Without `on=`, Korbase uses the one foreign key between the two tables, in
either direction. `select(Post).join(User)` and `select(User).left_join(Post)`
both use `posts.author_id = users.id`. When there is no such key, or
more than one, pass the condition yourself, with columns of both tables:

```python
select(Post).join(User, on=P.author_id == U.id)
```

`on=` takes a condition built with `cols()`, not a string.

### Filter, sort and count

`where()`, `order_by()`, `limit()` and `offset()` can use the column of any
table in the query. Keyword pairs in `where()` always mean the first model:

```python
q = select(Post).join(User).where(U.name == "Ada", title="Hello")
```

This is `"users"."name" = $1 AND "posts"."title" = $2`. A column of a table
that is not in the query raises `KorbaseError`.

`.columns()`, `.group_by()` and `.having()` work over a join, as in
[Aggregates](queries.md#aggregates-and-grouping). Here is the number of posts
for each user, with `0` for the users who have none:

```python
q = (
    select(User).left_join(Post)
    .columns(U.name, P.id.count())
    .group_by(U.name)
    .order_by(P.id.count().desc(), U.name.asc())
)
rows = await db.run(q)   # [("Ada", 2), ("Bob", 1), ("Cy", 0)]
```

`P.id.count()` counts the posts, not the rows, so a user with no post gives
`0`. `count()` would give `1`.

### Three tables

A join can be followed by one more, so a query has at most two joins. The
second one looks for its foreign key in all the tables already in the query.

```python
rows = await db.run(select(Post).join(User).left_join(Comment))
```

This assumes a `Comment` model with a foreign key to `posts`. Each row is a
`tuple[Post, User, Comment | None]`.

### What Korbase refuses

These raise `KorbaseError`:

- `.join(Model)` when no foreign key links the table to the query, or when
  several do. Pass `on=`.
- The same table twice, a self-join such as `select(User).join(User)`.
  Use [raw SQL](transactions.md) for that.
- A third join.
- `.load()` before a join. After a join there is no `.load()` at all.

Also, `db.run()` and `db.first()` take a join, but `db.value()` does not:
a row of models is not a single value. `db.value()` still works on a
`.columns()` query, with or without a join.

### Join or load

Both give you linked rows. They answer different questions.

| | `.load()` | `.join()` |
|---|---|---|
| Queries | one, plus one per relation | one |
| Result | models with nested objects: `post.author.name` | flat tuples: `(post, user)` |
| Needs a `Rel` field | yes | no, only a foreign key |
| Filter on the other table | no | yes |
| Group and count over both | no | yes |

Use `.load()` when you want a tree of objects to return from an API, such as a
user with their list of posts. Use a join when you filter or sort on the other
table (`where(U.name == "Ada")`), count across it, or want a flat report.
A join repeats the first row once for each match, so a user with 100 posts
is 100 rows. A join does not fill `Rel` fields: `post.author` stays at its
default.

## Going further

### One query per relation

Each relation costs one extra query, not one per row. For
`select(Post).order_by(P.id.asc()).load(P.author)`, Korbase runs:

```sql
SELECT "posts"."id", "posts"."author_id", "posts"."title" FROM "posts" ORDER BY "posts"."id" ASC
SELECT "users"."id", "users"."name" FROM "users" WHERE "users"."id" = ANY($1)
```

The second query gets every distinct foreign key value in one array
parameter, with `= ANY($1)`. Korbase then matches the rows to their owners in
Python. For a one-to-many relation, the array holds the ids of the loaded
rows, and the query reads `FROM "posts" WHERE "posts"."author_id" = ANY($1)`.
Loading `n` relations costs `n` extra queries. A relation query is skipped when
there are no rows, or when no foreign key value needs a lookup.

### Which column is used

The join uses the foreign key marker when there is one, else the primary key.

- **Many-to-one** (`Post.author`): if `author_id` is marked `FK("users.<column>")`,
  Korbase matches `users.<column>`. Otherwise it matches the primary key of `User`.
- **One-to-many** (`User.posts`): if `Post.author_id` is marked
  `FK("users.<column>")`, Korbase matches that column of `User`. Otherwise it
  matches the primary key of `User`.

So a foreign key can point at a unique column that is not the primary key.
A many-to-one `Rel` must name a field of the same model, checked when the
model is declared. For a one-to-many `Rel`, the named field must be a column
of the target model, or loading raises `KorbaseError`.

### Forward references

Put both models in the same module. Use `from __future__ import annotations`,
as above, or write the later model's name as a string. Korbase resolves the
name the first time the relation is used, in the module that defines the
model. If it cannot be found, loading raises `KorbaseError` with
`cannot resolve`.

### Limits

Composite primary keys are not supported yet. If the column to match is part
of a composite key and no `FK` marker names another column, loading raises
`KorbaseError`.

**Next:** [Transactions and raw SQL](transactions.md)
