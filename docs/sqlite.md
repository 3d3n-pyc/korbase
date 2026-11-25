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

No extra is needed: `pip install --pre korbase` is enough. The scheme `sqlite:` picks the backend. korbase needs SQLite 3.35 or later and raises `RuntimeError` on an older one.

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

Only one process writes at a time. A second writer waits for `busy_timeout_ms`, then fails with `korbase.DatabaseBusy`. The original `sqlite3.OperationalError: database is locked` is on `__cause__`. The transaction did not run, so you can retry it (see [Errors](errors.md#retry-when-the-database-is-busy)).

Keep write transactions short, and do slow work (network calls, for example) outside them. Raise `busy_timeout_ms` if short waits are normal for your load. Several processes can share one file, each with its own `Database`: that is safe, and the tests do it with two writers at once.

## Migrations on SQLite

`python -m korbase.migrate make|apply|status` and `create_all` work with a `sqlite:` DSN. `make` leaves the database untouched: it builds the models in a scratch memory database and compares. Each file runs in its own `BEGIN IMMEDIATE` transaction, and two `apply` runs at once apply each file once. There are no schemas on SQLite.

SQLite can only do a few `ALTER TABLE` changes. For these, `make` writes plain statements:

- `CREATE TABLE`, with the foreign keys inside it.
- `ADD COLUMN`, when the column is nullable or has a constant default.
- `CREATE INDEX` and `DROP INDEX`, including partial indexes (`Index(..., where=...)`).
- `DROP COLUMN`, `DROP TABLE` and `DROP INDEX`, each with a `-- korbase:review` line.

For every other change, `make` writes the whole [table rebuild](#table-rebuilds) for you. That covers a change of type, nullability, default, primary key, unique constraint or foreign key, a new `NOT NULL` column without a default, and a column with a key or an expression default.

### Table rebuilds

The recipe SQLite documents is: create the new table, copy the rows, drop the old table, rename the new one. `make` writes it, with the foreign key handling around it. You do not write SQL by hand.

Here `books.subtitle` goes from `str | None` to `str`, so the column becomes `NOT NULL`. This is the file `make` wrote:

```sql
-- korbase:no-transaction
-- Generated by korbase on 2026-11-23.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TABLE "books__new" (
    "id" INTEGER PRIMARY KEY,
    "author_id" INTEGER NOT NULL,
    "subtitle" TEXT NOT NULL,
    "pages" TEXT NOT NULL,
    CONSTRAINT "books_author_id_fkey" FOREIGN KEY ("author_id") REFERENCES "authors" ("id") ON DELETE NO ACTION
);
-- korbase:review books.subtitle becomes NOT NULL, fails if NULLs exist
INSERT INTO "books__new" ("id", "author_id", "subtitle", "pages") SELECT "id", "author_id", "subtitle", "pages" FROM "books";
DROP TABLE "books";
ALTER TABLE "books__new" RENAME TO "books";
PRAGMA foreign_key_check;
COMMIT;
PRAGMA foreign_keys=ON;
```

Step by step:

1. `-- korbase:no-transaction` (line 1) tells `apply` not to wrap the file in its own transaction. The file needs this because `PRAGMA foreign_keys` has no effect inside a transaction.
2. `PRAGMA foreign_keys=OFF` stops `DROP TABLE "books"` from deleting or refusing rows that other tables point to.
3. `BEGIN IMMEDIATE` takes the write lock and starts the transaction that the file commits itself.
4. `CREATE TABLE "books__new"` is the table as your models now describe it. Its foreign keys are inside it.
5. `INSERT ... SELECT` copies every row. A column whose type changed is copied with `CAST(...)`.
6. `DROP TABLE "books"` removes the old table. SQLite drops its indexes too, so `make` creates them again after the rename.
7. `ALTER TABLE ... RENAME TO` gives the new table the old name.
8. `PRAGMA foreign_key_check` lists rows that break a foreign key. If it finds any, `apply` fails (see below).
9. `COMMIT` makes the change permanent.
10. `PRAGMA foreign_keys=ON` turns the checks back on for the connection.

A rebuild that cannot lose data, for example `NOT NULL` to nullable, has no review line and is ready to apply.

### When a rebuild is marked for review

A `-- korbase:review` line stays above the `INSERT` when the copy can lose data or fail:

- **A type change.** The values are copied with `CAST`.
- **A column that becomes `NOT NULL`.** The copy fails if any row holds `NULL`.
- **A dropped column.** Its data is gone.
- **A new `NOT NULL` column without a default.** The copy fails if the table has rows.
- **Objects korbase does not manage.** A rebuild drops the table's hand-made indexes, its triggers and its `CHECK` constraints. Each one gets a review line naming it: recreate it at the end of the file, then delete the line.

`apply` refuses the file until you delete these lines. Before you do, look at your data.

`CAST` does not complain about bad values. `CAST('abc' AS INTEGER)` gives `0`, `CAST('' AS REAL)` gives `0.0`, and `CAST('7 pages' AS INTEGER)` gives `7`. A text that is not a clean number turns into `0` or a different number, and the row still copies. Run a `SELECT` on the old table first and look at the rows that change:

```sql
SELECT id, pages FROM books WHERE CAST(pages AS INTEGER) IS NOT pages;
```

For a column that becomes `NOT NULL`, count the rows that would fail:

```sql
SELECT count(*) FROM books WHERE subtitle IS NULL;
```

If rows come back, fix the data, or edit the `SELECT` in the file, for example with `COALESCE("subtitle", '')`, then delete the review line.

### What `apply` does when a foreign key breaks

`apply` runs the file one statement at a time. If `PRAGMA foreign_key_check` returns rows, `apply` raises an error that names the tables, such as `foreign key check failed, broken rows in: books (1)`. Then it:

- rolls back the transaction, so the old table is back and nothing is half changed;
- turns `foreign_keys` back on, always, also when the file fails for another reason;
- deletes the version row, so the file is not recorded and `status` still shows it as pending.

Fix the rows or the file, then run `apply` again. Files before it stay applied.

If the process is killed while the file runs, the version row can stay, and the file counts as applied. Check what the file did, then delete its row from `korbase_migrations` to run it again.

Files written by earlier versions, which hold a review block and your own SQL, still apply as before.

## Going further

- **Aggregates work as on Postgres.** `count`, `sum`, `avg`, `min`, `max`, `group_by` and `having` are in the query builder (see [Queries](queries.md#aggregates-and-grouping)). Dates and UUIDs come back as Python values, though SQLite stores them as text. Raw SQL is only needed for what the builder does not cover. `make` proposes dropping an index that is not in the models, with a review marker, so move such an index into a model or delete that line.
- **No arithmetic on Decimals.** A `Decimal` column or value in `+ - * / %` raises `KorbaseError` on SQLite: the text would be computed as a floating point number. Use Postgres, or store whole cents in an `int` field (see [Arithmetic](queries.md#what-korbase-refuses)). A division by zero gives `None` here, and an error on Postgres.
- **Decimals compare as text.** `ORDER BY amount` puts `'10.00'` before `'9.00'`, and `SUM(amount)` reads the text as floating point numbers. For money, sort and add in Python, or store whole cents in an `int` field.
- **One thread per connection.** Each pooled connection owns a thread, so a slow query never blocks the event loop. A `Transaction` keeps its connection until the block ends, and when the pool is empty the next caller waits. With `size=1`, requests that use `kdb.transaction` run one after the other.
- **Status tags.** `execute` returns Postgres-style tags (`INSERT 0 1`, `UPDATE 3`), so code that reads them works on both.
- **A local disk only.** WAL does not work on a network file system. Keep the file on a disk of the machine that runs the processes.
- **Tests.** `sqlite::memory:` makes a fast, empty database per test, with no Docker. The [testing](testing.md) page uses Postgres: the same `create_all(db, models)` call works on either.

**Next:** [Testing](testing.md)
