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
relation is loaded only when you ask for it with `.load()`.

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
