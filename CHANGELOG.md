# Changelog

## Unreleased

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