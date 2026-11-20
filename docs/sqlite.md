# SQLite

**Use this when** you want korbase without a database server: tests, a small app, a script, or one machine with one data file.

## The idea

SQLite is a database that lives in one file. There is nothing to install or start: Python already ships it as the `sqlite3` module. korbase uses it through the same `Database`, models and queries as for Postgres. The DSN is the only thing you change.

Pick SQLite when one machine is enough: unit tests, a local tool, a small site. Pick Postgres when many servers share the data, when many writers work at once, or when you need its richer types and `ALTER TABLE`. SQLite lets many processes read at once, but only one write at a time.

## Connect

```python
from korbase import Database


async def open_db() -> Database:
    return await Database.connect("sqlite:///app.db")
```

| DSN | Database |
| --- | --- |
| `sqlite:///app.db` | the file `app.db`, relative to the current folder |
| `sqlite:////var/data/app.db` | an absolute path (four slashes) |
| `sqlite::memory:` | a database in memory, gone when the `Database` closes |

`sqlite::memory:` always uses one connection, whatever `size` you ask for. Two connections would each see their own empty database. Two calls to `Database.connect("sqlite::memory:")` give two separate databases.

No extra is needed: `pip install korbase` is enough. The scheme `sqlite:` picks the backend. korbase needs SQLite 3.35 or later and raises `RuntimeError` on an older one.

Options go to `Database.connect` (and to `korvex.install(app, dsn, ...)`):

- `size=1`: connections in the [pool](glossary.md#connection-pool). Each one runs on its own thread.
- `busy_timeout_ms=5000`: how long a connection waits for a lock before it fails.

Other options raise `TypeError`. The Postgres options `min_size` and `max_size` do not exist here.

## What korbase sets

On every new connection, korbase runs:

- `PRAGMA journal_mode=WAL` (not for `:memory:`). In WAL mode readers do not block the writer and the writer does not block readers. SQLite adds `app.db-wal` and `app.db-shm` next to your file: keep them together when you copy a live database.
- `PRAGMA foreign_keys=ON`. SQLite ignores [foreign keys](glossary.md#foreign-key) unless you ask, so korbase asks.
- `PRAGMA busy_timeout=5000`, or your `busy_timeout_ms`.

## Values and raw SQL

SQLite has only integers, floats, text and bytes. korbase converts on the way in and out, so models give you back the Python types you declared. This is what is stored:

| Python | Stored as |
| --- | --- |
| `bool` | integer `1` or `0` |
| `uuid.UUID`, `Decimal`, `Enum` | text (an enum stores its value) |
| `datetime`, `date`, `time` | ISO text |
| `dict`, `list` | compact JSON text |

An aware `datetime` is converted to UTC first and written as `2026-01-01T10:00:00.000000+00:00`, so text order is time order. You read it back in UTC. A naive `datetime` is stored as it is. Do not mix naive and aware values in one column: they do not sort together.

Raw SQL does not convert. A query on the `ok` column returns `1`, not `True`, and a `Decimal` column returns a string unless you pass `model=`. Write raw SQL with `?` placeholders, not `$1`:

```python
rows = await db.fetch("SELECT id, ok FROM tasks WHERE ok = ? AND id > ?", True, 10)
```

`?1` and `?2` also work when you need to reuse a value. Values you pass are converted the same way, so `True` and a `datetime` are fine as arguments.

## Case-insensitive search

`ilike` works, and it is the same as `like`: SQLite's `LIKE` ignores case, but only for ASCII letters. `'a' LIKE 'A'` is true, `'é' LIKE 'É'` is false. If you need accented text to match, store a lower-cased copy in a second column.

## Transactions and several processes

`db.transaction()` runs `BEGIN IMMEDIATE`, so it takes the write lock at once, not at the first write. A transaction inside one is a [savepoint](glossary.md#savepoint), as on Postgres. An error [rolls back](glossary.md#rollback) the block.

Only one process writes at a time. A second writer waits for `busy_timeout_ms`, then fails with the error SQLite gives, which korbase does not change:

```
sqlite3.OperationalError: database is locked
```

Keep write transactions short, and do slow work (network calls, for example) outside them. Raise `busy_timeout_ms` if short waits are normal for your load. Several processes can share one file, each with its own `Database`: that is safe, and the tests do it with two writers at once.

## Migrations on SQLite

`python -m korbase.migrate make|apply|status` and `create_all` work with a `sqlite:` DSN. `make` leaves the database untouched: it builds the models in a scratch memory database and compares. Each file runs in its own `BEGIN IMMEDIATE` transaction, and two `apply` runs at once apply each file once. There are no schemas on SQLite.

SQLite can only do a few `ALTER TABLE` changes. korbase writes real SQL for:

- `CREATE TABLE`, with the foreign keys inside it.
- `ADD COLUMN`, when the column is nullable or has a constant default.
- `CREATE INDEX` and `DROP INDEX`, including partial indexes (`Index(..., where=...)`).
- `DROP COLUMN`, `DROP TABLE` and `DROP INDEX`, each with a `-- korbase:review` line.

For everything else the file holds a [review marker](glossary.md#review-marker) and a comment, and no SQL. This covers a new `NOT NULL` column without a default, a column with a key or an expression default, and a change of type, nullability, default, primary key, unique constraint or foreign key. For example:

```sql
-- korbase:review SQLite cannot alter the type of pages on "books"; rebuild the table (see docs/sqlite.md)
-- intended: pages changes from TEXT to INTEGER
```

`apply` refuses the file until you replace those lines with the SQL that does the job. That SQL is a table rebuild.

### Rebuilding a table by hand

The recipe: create the new table, copy the rows, drop the old table, rename the new one. This file changes `books.pages` from text to `INTEGER NOT NULL`:

```sql
-- korbase:no-transaction
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
CREATE TABLE "books_new" (
    "id" INTEGER PRIMARY KEY,
    "author_id" INTEGER NOT NULL,
    "pages" INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY ("author_id") REFERENCES "authors" ("id") ON DELETE CASCADE
);
INSERT INTO "books_new" ("id", "author_id", "pages")
    SELECT "id", "author_id", COALESCE(CAST("pages" AS INTEGER), 0) FROM "books";
DROP TABLE "books";
ALTER TABLE "books_new" RENAME TO "books";
PRAGMA foreign_key_check;
COMMIT;
PRAGMA foreign_keys = ON;
```

Why it looks like this:

- **`PRAGMA foreign_keys = OFF` does nothing inside a transaction.** Without it, `DROP TABLE "books"` would delete or refuse on rows that other tables point to. So the file must run outside korbase's transaction: its first line is exactly `-- korbase:no-transaction`, and the file opens its own `BEGIN IMMEDIATE`.
- **Turn the pragma back on at the end.** The connection goes back to the pool, and a forgotten `OFF` would stay on it.
- **`make` writes a `-- Generated by korbase` line first.** Put the `no-transaction` line above it, or delete the header, since the marker must be line 1.
- **`DROP TABLE` also drops the table's indexes.** Add a `CREATE INDEX` for each one after the rename.
- **`PRAGMA foreign_key_check` lists broken rows** as a result, but it does not stop the file. Run it by hand on a copy first.
- **Test the file on a copy of the database.** If a statement fails, the connection stays inside the open `BEGIN IMMEDIATE`, with foreign keys off. `apply` reports the error: stop the program, which rolls the transaction back, fix the file and run `apply` again.

## Going further

- **Not in the builder yet.** Aggregates such as `count`, `sum` and `group by` are not in the query builder. Write them as raw SQL, in a migration file or with `db.fetch`. `make` proposes dropping an index that is not in the models, with a review marker, so move such an index into a model or delete that line.
- **Decimals compare as text.** `ORDER BY amount` puts `'10.00'` before `'9.00'`, and `SUM(amount)` reads the text as floating point numbers. For money, sort and add in Python, or store whole cents in an `int` field.
- **One thread per connection.** Each pooled connection owns a thread, so a slow query never blocks the event loop. A `Transaction` keeps its connection until the block ends, and when the pool is empty the next caller waits. With `size=1`, requests that use `kdb.transaction` run one after the other.
- **Status tags.** `execute` returns Postgres-style tags (`INSERT 0 1`, `UPDATE 3`), so code that reads them works on both.
- **A local disk only.** WAL does not work on a network file system. Keep the file on a disk of the machine that runs the processes.
- **Tests.** `sqlite::memory:` makes a fast, empty database per test, with no Docker. The [testing](testing.md) page uses Postgres: the same `create_all(db, models)` call works on either.

**Next:** [Testing](testing.md)
