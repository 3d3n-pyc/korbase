# Korvex

**Use this when** you build a web app with Korvex and want a database in your handlers, with one transaction per request. Install it with `pip install korbase[korvex]`.

## The idea

A web app answers many requests at once. Opening a new database connection for each one is slow, so the app opens a few connections when it starts and shares them. This is a [connection pool](glossary.md#connection-pool). A handler borrows a connection, uses it, and gives it back.

A per-request [transaction](glossary.md#transaction) goes one step further. The app borrows a connection when the request starts and opens a transaction on it. If the handler finishes, the transaction commits. If the handler fails, it [rolls back](glossary.md#rollback), so a half-done request leaves nothing behind. That is the all-or-nothing rule, applied to one HTTP request.

## Setup

`korbase.korvex.install` opens the pool when the app starts and closes it when the app stops. Handlers get the database through `Depends`.

```python
from __future__ import annotations

import os
from typing import Annotated

import msgspec
from korvex import App, Depends, HTTPException, Response

from korbase import PK, Database, Transaction, cols, select, table
from korbase.korvex import install

app = App()
kdb = install(app, os.environ["KORBASE_DSN"])  # module level


@table("users")
class User(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str


@app.get("/users")
async def list_users(db: Annotated[Database, Depends(kdb.database)]) -> Response:
    users = await db.run(select(User).order_by(cols(User).id.asc()))
    return Response.json([u.name for u in users])


@app.post("/users", status=201)
async def add_user(tx: Annotated[Transaction, Depends(kdb.transaction)]) -> None:
    await tx.execute("INSERT INTO users (id, name) VALUES ($1, $2)", 3, "Cy")


@app.post("/bad")
async def bad(tx: Annotated[Transaction, Depends(kdb.transaction)]) -> None:
    await tx.execute("INSERT INTO users (id, name) VALUES ($1, $2)", 4, "Dee")
    raise HTTPException(400)  # the insert is rolled back
```

- `install(app, dsn, **db_opts)` registers the startup and shutdown hooks and returns the object we call `kdb` here.
- `db_opts` go to the driver. With the default asyncpg [backend](glossary.md#backend) they go to `asyncpg.create_pool`, for example `min_size=1, max_size=10`.
- Assign `kdb` at module level, as above. See [Going further](#going-further) for why.

## The two dependencies

- `Depends(kdb.database)` gives the handler the `Database`. Use it for reads, or for work that needs no transaction.
- `Depends(kdb.transaction)` gives the handler a `Transaction` for this request. It holds one connection for the whole request. It commits when the handler returns normally. It rolls back when the handler raises, including `HTTPException`, as `/bad` shows.

A `Transaction` has the same query methods as `Database`, and `tx.transaction()` opens a [savepoint](glossary.md#savepoint). See [Transactions](transactions.md).

## Pool size

Each request that uses `kdb.transaction` holds a connection until it ends. If `max_size` is small, requests wait for a free connection. Set `max_size` for the number of requests you expect at the same time. Keep handlers short: do slow work, such as calls to other services, before or after the database part.

## Tests

`tests/test_korvex.py` runs an app with `korvex.testing.TestClient`. It checks the commit, the rollback, and that the pool closes when the app stops. The test is skipped when Korvex is not installed.

## Going further

- **Pool lifecycle.** `install` adds an `on_startup` hook that calls `Database.connect(dsn, **db_opts)`, and an `on_shutdown` hook that closes the pool. The pool is kept on `app.local.korbase_db`. `app.local` belongs to the running event loop, so read it only inside handlers and hooks, not at import time.
- **Rollback on any error.** `kdb.transaction` is a generator dependency that wraps `db.transaction()`. Korvex throws the handler's exception into it, so the block rolls back and the error goes on as usual. This is not limited to `HTTPException`: the tests cover that case, and a handler that raises `RuntimeError` also had its insert rolled back when we tried it.
- **`from __future__ import annotations` and a module-level `kdb`.** With postponed annotations, `Annotated[Database, Depends(kdb.database)]` is a string until Korvex reads it. Korvex then looks the names up in the module's globals, so `kdb`, `Database` and `Transaction` must be module-level names. If `kdb` is created inside a function, the lookup cannot find it.

**Next:** [Testing](testing.md)
