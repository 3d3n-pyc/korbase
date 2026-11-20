"""Read a live Postgres schema into a `Snapshot`."""

from __future__ import annotations

from typing import Any

from korbase._backend import Conn
from korbase._schema import Column, ForeignKey, Index, Snapshot, Table, normalize_where

__all__ = ["inspect"]

_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}

_COLUMNS = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), a.attidentity <> ''
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE n.nspname = $1 AND c.relkind IN ('r', 'p') AND c.relname <> 'korbase_migrations'
ORDER BY c.relname, a.attnum
"""

_CONSTRAINTS = """
SELECT c.relname, k.conname, k.contype::text, k.confdeltype::text, t.relname,
  ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY u(n, o)
        JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n ORDER BY u.o),
  ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY u(n, o)
        JOIN pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.n ORDER BY u.o)
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class t ON t.oid = k.confrelid
WHERE n.nspname = $1 AND k.contype IN ('p', 'u', 'f') AND c.relkind IN ('r', 'p')
ORDER BY c.relname, k.conname
"""

_INDEXES = """
SELECT c.relname, i.relname, x.indisunique,
  ARRAY(SELECT a.attname::text FROM unnest(x.indkey::int2[]) WITH ORDINALITY u(n, o)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = u.n ORDER BY u.o),
  pg_get_expr(x.indpred, x.indrelid)
FROM pg_index x
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1 AND c.relkind IN ('r', 'p')
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = x.indexrelid
                  AND k.contype IN ('p', 'u'))
ORDER BY c.relname, i.relname
"""


def _rows(conn: Conn, sql: str, schema: str) -> Any:
    """Run one catalog query and return the rows as tuples."""
    return conn.fetch(sql, [schema], tuple)


def _split(constraints: list[Any]) -> dict[str, dict[str, list[Any]]]:
    """Group constraint rows by table, then by kind (`p`, `u`, `f`)."""
    out: dict[str, dict[str, list[Any]]] = {}
    for row in constraints:
        out.setdefault(row[0], {"p": [], "u": [], "f": []})[row[2]].append(row)
    return out


def _table(name: str, cols: list[Column], cons: dict[str, list[Any]], idx: list[Index]) -> Table:
    """Assemble one `Table` from its columns, constraint rows and indexes."""
    pk = cons["p"][0] if cons["p"] else None
    return Table(
        name=name,
        columns=tuple(cols),
        pk=tuple(pk[5]) if pk else None,
        pk_name=pk[1] if pk else None,
        foreign_keys=tuple(ForeignKey(r[1], tuple(r[5]), r[4], tuple(r[6]), _ACTIONS[r[3]]) for r in cons["f"]),
        uniques=tuple((r[1], tuple(r[5])) for r in cons["u"]),
        indexes=tuple(idx),
    )


async def inspect(conn: Conn, schema: str = "public") -> Snapshot:
    """Read the ordinary tables of `schema` into a `Snapshot`.

    The `korbase_migrations` table is left out.
    """
    columns: dict[str, list[Column]] = {}
    for r in await _rows(conn, _COLUMNS, schema):
        columns.setdefault(r[0], []).append(Column(r[1], r[2], r[3], r[4], r[5]))
    cons = _split(await _rows(conn, _CONSTRAINTS, schema))
    indexes: dict[str, list[Index]] = {}
    for r in await _rows(conn, _INDEXES, schema):
        indexes.setdefault(r[0], []).append(Index(r[1], tuple(r[3]), r[2], normalize_where(r[4]) if r[4] else None))
    empty: dict[str, list[Any]] = {"p": [], "u": [], "f": []}
    return Snapshot({n: _table(n, c, cons.get(n, empty), indexes.get(n, [])) for n, c in columns.items()})
