# Changelog

## Unreleased

### Changed (breaking)

- Errors from the database are now korbase exceptions, the same on Postgres and SQLite, where they used to be the driver's own (`asyncpg.UniqueViolationError`, `sqlite3.IntegrityError`, `sqlite3.OperationalError`). To update: catch `korbase.UniqueViolation`, `ForeignKeyViolation`, `NotNullViolation`, `CheckViolation` (or their parent `IntegrityError`), `DatabaseBusy` and `ConnectionFailed` instead of the driver classes. The original error stays on `__cause__`. The message holds no row values, so a code that read them from `str(error)` must read `__cause__` (and not show it to users). See `docs/errors.md`.

### Added

- Bulk insert: `insert(M).rows([{...}, {...}])` and `upsert(M).rows([...]).on_conflict("id")` write many rows in one statement and return them in input order, with server defaults filled. Each dict takes the keys of `.values(**kw)`; every row needs the same keys; `.rows()` and `.values()` replace each other. `KorbaseError` for an empty list, different keys, an unknown column, more than 32766 bound values (split into batches), and an upsert with the same conflict key twice. No upgrade needed.
- Aggregates: `count()` and the column methods `.count()`, `.sum()`, `.avg()`, `.min()` and `.max()`, on Postgres and SQLite. `avg` is always a `float`, and `sum`, `min` and `max` are `None` when there are no rows. Values keep the column type, so dates and UUIDs are decoded on SQLite too. Postgres has no `min` or `max` on `uuid` columns.
- `select(M).columns(*items)` returns tuples instead of models, with `.group_by(...)`, `.having(...)`, and `.desc()` on an aggregate in `order_by`. It raises `KorbaseError` for an empty `columns()`, a plain column that is not in `group_by` next to an aggregate, and `having` with no aggregate or `group_by`. `.load()` is not available on these queries.
- `db.value(q)` (and `tx.value(q)`) returns the single value of a query with one column, or `None`: `n = await db.value(select(Task).columns(count()).where(done=False)) or 0`. `db.run` gives a list of tuples and `db.first` a tuple or `None`. No upgrade needed.
- Joins: `select(A).join(B)` and `.left_join(B)` return tuples of models, `tuple[A, B]`, and `B | None` for a left join with no match (all its primary key columns are NULL). Without `on=`, the one foreign key between the tables is used, in either direction; otherwise pass `on=cols(A).x == cols(B).y`. At most two joins. `where`, `order_by`, `.columns()` and `group_by` accept columns of any joined table, and keyword filters refer to the first model. `run` and `first` work, `value` does not. `KorbaseError` for no foreign key or several without `on=`, the same table twice, a third join, and `.load()` with a join. No upgrade needed.
- `korbase.KorbaseError` (already raised for misuse) now has a database branch: `DatabaseError`, with `IntegrityError` (`UniqueViolation`, `ForeignKeyViolation`, `NotNullViolation`, `CheckViolation`), `DatabaseBusy` and `ConnectionFailed`. `.constraint` gives the constraint name on Postgres, and `table.column` text or `None` on SQLite.
- `DatabaseBusy` marks a failure that a retry of the transaction can fix: a SQLite lock after `busy_timeout_ms`, a Postgres lock not available, or a deadlock.
- A guide page, `docs/errors.md`, and a section in the agent skill.

### Changed

- On SQLite, `make` writes the full table rebuild for a change SQLite cannot `ALTER` (type, nullability, default, primary key, unique constraint, foreign key), where it used to write a review block to fill in by hand. The file starts with `-- korbase:no-transaction`, runs with `PRAGMA foreign_keys=OFF` inside `BEGIN IMMEDIATE ... COMMIT`, and ends with `PRAGMA foreign_key_check`. A `-- korbase:review` line stays where data can be lost (type change copied with `CAST`, `NULL` to `NOT NULL`, dropped column, new `NOT NULL` column without a default, and each hand-made index, trigger or `CHECK` constraint the rebuild drops). `apply` fails and rolls back when `foreign_key_check` finds broken rows, always turns foreign keys back on, and records nothing on failure. To update: nothing; files written by earlier versions still apply.

## 0.1.0a3

Documentation release: no code change.

### Changed

- The docs, README, package description and agent skill present korbase as an ORM for Postgres and SQLite, and every install command uses `--pre` (only alphas are published).
- Guide pages show what SQLite generates (`?n`, `LIKE`, `json_each`), its column types and how migrations behave without schemas.

## 0.1.0a2

### Added

- Partial indexes, `Index(..., where=...)`, on Postgres and SQLite, with migrations.

## 0.1.0a1

First alpha: models, typed queries, relations, transactions, migrations, Postgres and SQLite, Korvex integration.

- Project skeleton.
- Models: `@table` on msgspec Structs, dataclasses and TypedDicts, with `PK`, `FK`, `Unique`, `Default`, `Index` and `Rel` markers. No upgrade needed, since there was no API before.
- Queries: `select`, `insert`, `update`, `delete` and `upsert` builders, compiled to SQL with `$n` placeholders. Columns come from `cols(Model).field`.
- `Database` and `Transaction` with `run`, `first`, `fetch`, `fetchrow`, `execute` and `transaction()` (a nested transaction is a savepoint). The asyncpg backend is installed with `korbase[asyncpg]`.
- `.load(...)` for one-to-many and many-to-one relations, one extra query per relation.
- Korvex integration: `korbase.korvex.install(app, dsn)`, with `Depends(kdb.database)` and `Depends(kdb.transaction)`.
- Migrations: `python -m korbase.migrate make|apply|status` and `korbase.migrate.make/apply/status/create_all`. `make` diffs the models against the live schema and writes a numbered SQL file to review; risky statements carry `-- korbase:review`, which `apply` refuses until removed. `apply` checks checksums and order, takes an advisory lock and runs each file in its own transaction.
- New markers for migrations: `SqlType`, `PK(identity=...)`, `FK(..., on_delete=...)`, `Default(sql=...)`.
- Documentation site (MkDocs): tutorial, guide, glossary, API reference, and an agent skill shipped in the package.

### Changed (breaking)

- `insert` and `upsert` always return the rows they wrote, and have no `.returning()`. To update: delete `.returning()` from every `insert(...)` and `upsert(...)`; `run` gives a list and `first` gives the row.
- `run` on an `update` or a `delete` returns the number of rows changed, an `int`, not the command tag such as `"UPDATE 1"`. To update: use the number, for example `if await db.run(q) == 0`, or add `.returning()` to get the rows as a list. `first` on an `update` or `delete` still needs `.returning()`.
- A `None` given to a `param` that is compared with `==`, `!=` or `in_()` now raises `KorbaseError`, where it used to match nothing. To update: use `.where(column=None)` or `column.is_null()` when you mean NULL, and check the value before you run the query when it may be absent.
- `make` only compares the tables of the models you pass, so other tables in `public` are no longer reported as drops. To update: nothing, unless you relied on `make` to drop the table of a removed model. Run `make --all-tables` (or `make(..., all_tables=True)`) for that, then keep only the `DROP TABLE` lines you want.

### Added

- `.where(*conditions, **equals)` on `select`, `update` and `delete`: several conditions in one call, and `column=value` pairs, which accept a plain value or a `param`. A keyword set to `None` means `IS NULL`.
- `python -m korbase.migrate dev` and `korbase.migrate.dev(db, models, directory, message)`: `make` then `apply` in one step, for local work. It stops and applies nothing when the new file holds a `-- korbase:review` line.
- Typed results: `select`, `insert`, `upsert`, `update` and `delete` are generic over the model, so `run` is typed as `list[Model]` or `int`, and `first` as `Model | None`. `fetch` and `fetchrow` with `model=` are typed the same way.
- `python -m korbase.stubs <models.module> -o <file>` generates a module with a typed `cols()`: `cols(Post).title` is a `Column[str]` and `cols(Post).titel` is a type error. At runtime it is the same object as `korbase.cols`. Rerun it after changing models; `--check` exits 1 if the file is out of date, for CI.
- Column operators are typed: `like` and `ilike` only on `str` columns, and `<`, `==` and `in_()` take values of the column type. Keyword filters such as `.where(title=...)` are still checked at runtime only.
- `@table` keeps the class type, so `run(select(Post))` is `list[Post]` for the type checker.
- SQLite backend on the standard library `sqlite3` (SQLite 3.35 or later): `Database.connect("sqlite:///app.db")` or `"sqlite::memory:"`, with `size=` and `busy_timeout_ms=`. WAL, foreign keys and a busy timeout are set on each connection; transactions use `BEGIN IMMEDIATE`, so one process writes at a time. Migrations work on SQLite, but it cannot alter a column type, nullability, default, primary key, unique constraint or foreign key in place: `make` writes a `-- korbase:review` block with the intended change, and you write the table rebuild by hand (see `docs/sqlite.md`). Limits: `sqlite::memory:` uses one connection; `ilike` only ignores case for ASCII letters; raw SQL uses `?` placeholders and returns stored values (`1` for `True`, text for dates and decimals); partial indexes and aggregates are not in the builder yet. No upgrade needed.

### Changed (typing only)

- Comparing a typed column with a value of another type, such as `Column[int] == "3"`, is now a type error. Nothing changes at runtime. To update: fix the value or the column, or add `# type: ignore` where it is on purpose.
