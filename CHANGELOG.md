# Changelog

## Unreleased

- Project skeleton.
- Models: `@table` on msgspec Structs, dataclasses and TypedDicts, with `PK`, `FK`, `Unique`, `Default`, `Index` and `Rel` markers. No upgrade needed, since there was no API before.
- Queries: `select`, `insert`, `update`, `delete` and `upsert` builders, compiled to SQL with `$n` placeholders. Columns come from `cols(Model).field`.
- `Database` and `Transaction` with `run`, `first`, `fetch`, `fetchrow`, `execute` and `transaction()` (a nested transaction is a savepoint). The asyncpg backend is installed with `korbase[asyncpg]`.
- `.load(...)` for one-to-many and many-to-one relations, one extra query per relation.
- Korvex integration: `korbase.korvex.install(app, dsn)`, with `Depends(kdb.database)` and `Depends(kdb.transaction)`.
