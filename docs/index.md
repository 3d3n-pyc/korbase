# korbase

An async ORM for Postgres, built the Korvex way. You describe tables with
plain Python classes, build queries in Python, and read rows back as the same
classes. Its SQL migrations are files you read before they run.

It is for everyone who needs a Postgres database in Python: people who have
never used a database, and experienced developers who want a small, typed
and fast layer with no surprises.

## Status

Pre-alpha. The API may change before 1.0, with no deprecation step. Each
change is in the changelog, with how to update.

## Install

Python 3.10 and later, with the asyncpg driver:

```sh
pip install "korbase[asyncpg]" msgspec
```

## Example

This runs on a scratch database that already has an `authors` table. The
[tutorial](tutorial.md) creates tables with a migration.

```python
from __future__ import annotations
import asyncio
from typing import Annotated

import msgspec
from korbase import PK, Database, cols, insert, param, select, table


@table("authors")
class Author(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str


async def main() -> None:
    async with await Database.connect("postgresql://user:password@localhost/scratch") as db:
        await db.run(insert(Author).values(name="Ada"))
        found = await db.run(select(Author).where(cols(Author).name == param("n")), n="Ada")
        print(found)  # [Author(id=1, name='Ada')]


asyncio.run(main())
```

## Key ideas

- **Plain models.** msgspec Structs, dataclasses and TypedDicts work with
  `@table("name")`. Markers such as `PK()`, `FK(...)` and `Rel(...)` go
  inside `Annotated[...]`.
- **Queries compile once.** Every value is a `$n` parameter, passed by name
  with `param("name")`. A write with no `where()` is refused.
- **Relations load on request.** `.load(...)` adds one query per relation.
- **Reviewed SQL migrations.** `make` writes the SQL, you read it, `apply`
  runs it. A change that drops data is refused until you review it.

## Where to start

**New to databases or to korbase?** Follow the [tutorial](tutorial.md). You
build a small blog database from zero. The [glossary](glossary.md) explains
each word.

**Experienced developer?** Read the guide pages you need:

- [Models](models.md): the three model kinds, keys, defaults, indexes.
- [Queries](queries.md): select, insert, update, delete and upsert.
- [Relations](relations.md): `Rel` and `.load`.
- [Transactions](transactions.md): commit, rollback, savepoints, raw SQL.
- [Migrations](migrations.md): `make`, `apply`, `status` and review.
- [Korvex](korvex.md): use korbase in a web app.
- [Testing](testing.md): run your tests on a real Postgres.

**Expert?** Each guide page ends with a "Going further" section: limits,
internals and edge cases. The [reference](reference/korbase.md) lists every
public name, built from the docstrings. [AI](ai.md) covers `llms.txt` and
the agent skill. To work on korbase itself, read
[AGENTS.md](https://github.com/3d3n-pyc/korbase/blob/main/AGENTS.md).
