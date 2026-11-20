# Glossary

Short meanings for the words the guide uses. Each one ends with where korbase covers it.

## Async and await

Some work, like waiting for the database, takes time. An `async def` function can pause at an `await` and let other work run meanwhile. This is how one Python process serves many requests at once.

In korbase: every query method is async, so you write `await db.run(...)`. See [Transactions and queries](transactions.md).

## Backend

The part that talks to the database. korbase does not speak to Postgres itself. It asks a driver to do it, and the driver is the backend. The default is asyncpg for Postgres. A `sqlite:` DSN uses a backend built on the standard library `sqlite3` module.

In korbase: the DSN picks the backend, and `Database.connect(dsn, backend=...)` overrides it. See [Connect and close](transactions.md#connect-and-close).

## Checksum

A short fingerprint of a file. If one byte of the file changes, the fingerprint changes too. It is a quick way to tell that a file was edited.

In korbase: `apply` stores the SHA-256 of each migration file it runs. `status` shows `changed` when a file no longer matches. See [Versions and checksums](migrations.md#going-further).

## Column

One named piece of data in a table, with a type, like `name` (text) or `age` (integer). Each row has a value for every column.

In korbase: a field of a model is a column, and `cols(User).name` is that column in a query. See [Columns and parameters](queries.md#run-a-query).

## Compiled query

SQL text built from your query code, with placeholders where the values go. It is built once, and then reused for every run, which is faster than building it each time.

In korbase: a builder compiles itself the first time it runs. See [Compiled once](queries.md#parameters-queries-built-once).

## Connection pool

Opening a connection to the database is slow, so a pool opens a few and lends them out. A query borrows one, uses it, and gives it back.

In korbase: `Database` holds a pool. Its size is set with `min_size` and `max_size` on Postgres, and with `size=` on SQLite. See [Pool size](korvex.md#pool-size) and [SQLite](sqlite.md).

## Foreign key

A column whose values must exist in another table. A `posts.author_id` that points to `users.id` cannot name a user who does not exist. You also choose what happens to the posts when the user is deleted.

In korbase: the `FK("users.id", on_delete=...)` marker. See [Foreign keys](models.md#foreign-keys).

## Index

A side structure that lets the database find rows by some columns without reading the whole table. It makes reads faster and writes a little slower.

In korbase: `Index("author_id", "created_at")` in `@table(..., indexes=(...))`. See [Markers](models.md#markers).

## Migration

A saved, numbered change to the database structure, like "create the users table". Applied in order, migrations bring any database to the same shape, and they stay in version control.

In korbase: a `0001_name.sql` file, written by `make` and run by `apply`. For local work, `dev` does both in one step. See [Migrations](migrations.md).

## Model

A Python class that describes one table. Its fields are the columns. Rows come back from queries as instances of it.

In korbase: a msgspec Struct, a dataclass or a TypedDict under `@table("name")`. See [Models](models.md).

## N+1 queries

A slow pattern: one query gets 100 users, then 100 more queries get each user's posts. That is 101 round trips to the database when 2 would do.

In korbase: `.load(...)` fills a relation for all the rows with one extra query. See [One query per relation](relations.md#one-query-per-relation).

## NULL

The database's value for "no value". It is not zero and not an empty string. A column that allows it is nullable.

In korbase: a field typed `Optional[T]` is nullable. Any other field is `NOT NULL`. In a query, `.where(note=None)` or `.is_null()` tests for it, because `== NULL` never matches. See [Nullability](models.md#nullability).

## Parameter

A value sent to the database separately from the SQL text. The SQL holds a placeholder (`$1` on Postgres, `?1` on SQLite), and the value travels on its own.

In korbase: `param("name")` marks a value that you give when the query runs. A plain value, as in `.where(name="Ada")`, is sent the same way. A `param` compared with `==` cannot be `None`. See [Columns and parameters](queries.md#run-a-query).

## Primary key

The column, or columns, that identify one row. No two rows share the same key, and it is never NULL.

In korbase: the `PK()` marker. A single `int` key is an identity column, which the database numbers for you. See [Primary keys](models.md#primary-keys).

## Relation

A link between two tables, like a user and their posts, usually built on a foreign key. It lets you load the related rows together.

In korbase: `Rel("fk_field")` on a field that is not a column. See [Relations](relations.md).

## Review marker

A comment in a migration file that says "a person must check this statement", for example one that drops a column and loses its data.

In korbase: the line `-- korbase:review`. `apply` refuses to run while one is left in a pending file. See [Review markers](migrations.md#reviewing-a-file).

## Rollback

Undoing every change made in a transaction, as if it never happened. It runs by itself when an error leaves the block.

In korbase: an exception inside `async with db.transaction()` rolls back. Tests use this to leave no data behind. See [Testing](testing.md).

## Savepoint

A bookmark inside a transaction. You can roll back to it and undo only what came after, while the earlier work stays.

In korbase: a nested `tx.transaction()` is a savepoint. See [Transactions](transactions.md#transactions).

## Schema

Two meanings. The shape of a database: its tables, columns, keys and indexes. Also, in Postgres, a named folder of tables inside a database. Tables go in `public` by default.

In korbase: `make` compares your models with their tables in the `public` schema, and leaves other tables alone unless you pass `--all-tables`. See [How make works](migrations.md#going-further).

## Scratch schema

A temporary schema made only to be thrown away. It gives a safe place to build something and compare it, without touching your real tables.

In korbase: `make` builds your models in `korbase_scratch_<pid>_<random>`, compares, and rolls it back. See [How make works](migrations.md#going-further).

## SQL injection

An attack where a user types SQL into a form field, and the program pastes it into a query as text. The database then runs the attacker's commands. Sending values as parameters stops it.

In korbase: every value is a parameter (`$n` on Postgres, `?n` on SQLite), and names are checked and quoted. Raw SQL text is not checked, so never build it from user input. See [Raw SQL](transactions.md#raw-sql).

## Transaction

A group of changes that succeed together or not at all. If anything fails, the database goes back to how it was before.

In korbase: `async with db.transaction() as tx:`. It commits at the end of the block and rolls back on error. See [Transactions](transactions.md#transactions).

## Unique constraint

A rule that no two rows may have the same value in a column, like an email. The database refuses a second row with the same value.

In korbase: the `Unique()` marker. See [Markers](models.md#markers).

## Version table

A table where the tool records what it has already done, so it does not do it twice.

In korbase: `korbase_migrations` holds one row per applied migration: name, checksum and time. See [Versions and checksums](migrations.md#going-further).

## Advisory lock

A lock that the database offers but does not tie to any table. Programs agree to take it, and then only one holder runs at a time. The others wait.

In korbase: each `apply` takes one, so two runs at once apply each file once. See [Applying](migrations.md#going-further).
