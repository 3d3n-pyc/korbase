# Errors

**Use this when** a query can fail and you want to react: a duplicate email, a missing parent row, a locked database.

korbase raises its own exceptions, the same ones on Postgres and SQLite. You do not catch `asyncpg` or `sqlite3` errors.

## Catch a duplicate

```python
from korbase import UniqueViolation, insert

try:
    await db.run(insert(User).values(id=1, name="Ann", email="ann@example.com"))
except UniqueViolation:
    print("That email is already taken.")
```

`UniqueViolation` is raised when a row repeats a value that must be unique: a `Unique()` column or a primary key. The same code works on both databases.

## The classes

Everything is importable from `korbase`.

```
KorbaseError                 every error korbase raises
└── DatabaseError            the database said no
    ├── IntegrityError       a row broke a constraint
    │   ├── UniqueViolation
    │   ├── ForeignKeyViolation
    │   ├── NotNullViolation
    │   └── CheckViolation
    ├── DatabaseBusy         locked, lock not available, or deadlock: retry
    └── ConnectionFailed     the connection was lost or could not be opened
```

Catch a leaf to handle one case, or a parent to handle all of them: `except IntegrityError` catches the four violations. `KorbaseError` is also raised for mistakes in your own code, such as a missing `param`, so it is wider than `DatabaseError`.

## What the database did, what korbase raises

On Postgres, the class comes from the SQLSTATE code of the error.

| The database did | korbase raises |
| --- | --- |
| Unique or primary key repeated (`23505`) | `UniqueViolation` |
| Foreign key without a parent row (`23503`) | `ForeignKeyViolation` |
| NULL in a `NOT NULL` column (`23502`) | `NotNullViolation` |
| `CHECK` failed (`23514`) | `CheckViolation` |
| Another integrity error, such as an exclusion constraint (`23xxx`) | `IntegrityError` |
| Lock not available (`55P03`) or deadlock (`40P01`) | `DatabaseBusy` |
| Connection error (`08xxx`), closed connection, network error or timeout | `ConnectionFailed` |
| Any other error from the server | `DatabaseError` |

On SQLite, the class comes from the message of the error.

| The database did | korbase raises |
| --- | --- |
| `UNIQUE constraint failed` | `UniqueViolation` |
| `FOREIGN KEY constraint failed` | `ForeignKeyViolation` |
| `NOT NULL constraint failed` | `NotNullViolation` |
| `CHECK constraint failed` | `CheckViolation` |
| Another integrity error | `IntegrityError` |
| `database is locked`, or `database table is locked` | `DatabaseBusy` |
| `unable to open database file` | `ConnectionFailed` |
| Any other error | `DatabaseError` |

Errors that are not from the database, such as `msgspec.ValidationError` from `validate=True`, or an exception of your own inside a transaction block, are not changed.

A constraint that is checked late (a deferred foreign key) fails when the transaction commits, so the error comes from the end of the `async with` block, not from the statement.

## The constraint

`error.constraint` says which constraint was broken, when the database tells it. It is `None` otherwise.

- **Postgres** gives the constraint name, for example `users_email_key`.
- **SQLite** gives text in the form `table.column`, for example `users.email`, for a unique or a not null violation. A unique constraint on several columns gives all of them in one text. For a foreign key or a check, it is `None`.

```python
try:
    await db.run(insert(User).values(id=2, name="Bob", email="ann@example.com"))
except UniqueViolation as err:
    if err.constraint in ("users_email_key", "users.email"):
        print("That email is already taken.")
    else:
        raise
```

The two spellings differ because the databases differ. If your code runs on one database, test for one.

## The original error

The error from the driver is kept on `__cause__`, so nothing is lost:

```python
try:
    await db.run(insert(User).values(id=1, name="Ann", email="ann@example.com"))
except UniqueViolation as err:
    print(type(err.__cause__))  # asyncpg.exceptions.UniqueViolationError, or sqlite3.IntegrityError
```

The message of a korbase error holds no row values. Postgres puts them in a separate `detail` line (`Key (email)=(ann@example.com) already exists`), and korbase leaves that line on `__cause__`. Use it in your logs if you need it. Do not show it to end users: it can contain other people's data. Show your own text, like "That email is already taken."

## Retry when the database is busy

`DatabaseBusy` means the database could not take a lock now, but it can work later. The cause is one of:

- SQLite: another process holds the write lock for longer than `busy_timeout_ms` (see [SQLite](sqlite.md#transactions-and-several-processes)).
- Postgres: a lock was not available, for example `SELECT ... FOR UPDATE NOWAIT` on a locked row.
- Postgres: two transactions waited for each other (a deadlock), and the server stopped one.

Retry the whole transaction, not one statement, because a failed transaction is rolled back. Wait a little longer each time, and give up after a few tries:

```python
import asyncio

from korbase import DatabaseBusy


async def transfer(db, amount: int) -> None:
    for attempt in range(5):
        try:
            async with db.transaction() as tx:
                await tx.execute("UPDATE accounts SET balance = balance - $1 WHERE id = 1", amount)
                await tx.execute("UPDATE accounts SET balance = balance + $1 WHERE id = 2", amount)
            return
        except DatabaseBusy:
            if attempt == 4:
                raise
            await asyncio.sleep(0.05 * 2**attempt)
```

Only retry code that is safe to run twice. Here the first try was rolled back, so nothing was applied. Do not retry work with effects outside the database, such as sending an email, inside the same block.

## Going further

- **A lost connection.** `ConnectionFailed` is raised when a connection cannot be opened, or when one drops. Check the DSN, the path and the server.
- **Other errors.** A syntax error or a missing table raises plain `DatabaseError`. This is a bug in the query: fix it, do not catch it.
- **Korvex.** In a handler that uses `kdb.transaction`, an error rolls the transaction back and then goes on as usual, so these classes reach your code the same way (see [Korvex](korvex.md)).
- **Tests.** The tests are in `tests/test_errors_pg.py` and `tests/test_errors_sqlite.py`.

**Next:** [Migrations](migrations.md)
