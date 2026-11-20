# Testing

**Use this when** you want automated tests (pytest, for example) for code that uses korbase.

## The idea

A test should not need a fake database. korbase tests run against a real Postgres, so you test what really happens. Two tricks keep this fast and clean. The tables are created once for the whole run. Each test then works inside a [transaction](glossary.md#transaction) that is [rolled back](glossary.md#rollback) at the end, so the next test starts with no leftover data.

## A disposable Postgres

```sh
docker run -d --rm --name app-test -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app \
  -e POSTGRES_DB=app -p 55433:5432 postgres:18
export APP_TEST_DSN=postgresql://app:app@localhost:55433/app
```

`--rm` deletes the container when it stops (`docker stop app-test`).

## Create the schema once

`create_all(db, models)` creates the tables in one transaction. It drops nothing, so it fails if a table exists: start from an empty database. (Your real [schema](glossary.md#schema) comes from [migrations](migrations.md). Tests skip them and build the tables straight from the [models](models.md).)

```python
import asyncio
import os

import pytest

from korbase import Database
from korbase.migrate import create_all

from app.models import Post, User  # your code: your models


@pytest.fixture(scope="session", autouse=True)
def schema() -> None:
    async def build() -> None:
        async with await Database.connect(os.environ["APP_TEST_DSN"]) as db:
            await create_all(db, [User, Post])

    asyncio.run(build())
```

## Roll back each test

Raise a private error inside the transaction. It rolls back, so the next test starts from the same data. The code under test must use `tx`.

```python
import asyncio
import os

from korbase import Database


class _Rollback(Exception):
    """Ends a test's transaction on purpose."""


async def add_user() -> None:
    async with await Database.connect(os.environ["APP_TEST_DSN"]) as db:
        try:
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO users (id, name) VALUES (1, 'Ada')")
                assert len(await tx.fetch("SELECT id FROM users")) == 1
                raise _Rollback
        except _Rollback:
            pass


def test_add_user() -> None:
    asyncio.run(add_user())
```

Open the `Database` inside the loop that runs the test. A [connection pool](glossary.md#connection-pool) belongs to the loop that opened it. `asyncio.run` in a plain test works, and so do pytest-asyncio and anyio.

## Testing Korvex routes

`korvex.testing.TestClient` calls routes without a server.

```python
import os
from typing import Annotated

from korvex import App, Depends, Response
from korvex.testing import TestClient
from korbase import Database
from korbase.korvex import install

app = App()
kdb = install(app, os.environ["APP_TEST_DSN"], min_size=1, max_size=2)


@app.get("/users")
async def users(db: Annotated[Database, Depends(kdb.database)]):
    rows = await db.fetch("SELECT name FROM users ORDER BY name")
    return Response.json([r[0] for r in rows])


def test_users_route() -> None:
    with TestClient(app) as client:
        assert client.get("/users").status == 200
```

The `with` block starts the app, which opens the pool, and stops it at the end. A route with `Depends(kdb.transaction)` commits when it returns, so a rollback does not undo its writes. Clear those tables before such a test, with `TRUNCATE users, posts RESTART IDENTITY CASCADE`.

## Going further

### Isolation between tests
- A rollback undoes rows, not counters. Identity values (the numbers of an auto `id`) keep growing. Do not assert on generated ids, or use `TRUNCATE ... RESTART IDENTITY`.
- Only code that uses the same `tx` is inside the test's transaction. A query on `db` borrows another connection, does not see uncommitted rows, and commits on its own.
- To test a failure and carry on, use `tx.transaction()`. It is a [savepoint](glossary.md#savepoint), so only the inner part is undone.

### Parallel test runs
- `create_all` takes no lock. Two workers that run it on the same database race, and one fails. Give each worker its own database, for example `app_test_gw0`, made with `await db.execute("CREATE DATABASE app_test_gw0")` from another database.
- `migrate.apply` takes an [advisory lock](glossary.md#advisory-lock), so several processes can run it on one database and each file is applied once. This protects the schema only. Tests that write and commit still need separate databases, or rollbacks.

### Speed
- Build the schema once per session, as above, not once per test. For a test-only container, add `-c fsync=off` after the image name. It trades crash safety for speed. Never use it for real data. Keep pools small (`min_size=1, max_size=2`).

**Next:** [For AI assistants](ai.md)
