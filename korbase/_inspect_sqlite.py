"""Read a live SQLite schema into a `Snapshot`."""

from __future__ import annotations

import re
from typing import Any

from korbase._backend import Conn
from korbase._schema import Column, ForeignKey, Index, Snapshot, Table, normalize_where

__all__ = ["inspect_sqlite"]

_IDENT = r'"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]|\w+'
_NAMED = re.compile(
    rf"CONSTRAINT\s+({_IDENT})\s+(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_TABLES = (
    "SELECT name, sql FROM sqlite_schema WHERE type = 'table' "
    "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND name <> 'korbase_migrations' ORDER BY name"
)


def _quote(name: str) -> str:
    """Quote an identifier read from `sqlite_schema`, doubling any quote character."""
    return '"' + name.replace('"', '""') + '"'


async def _pragma(conn: Conn, name: str, table: str) -> list[Any]:
    """Run `PRAGMA name(table)`; `table` comes from `sqlite_schema`, never from a caller."""
    return await conn.fetch(f"PRAGMA {name}({_quote(table)})", [], tuple)


def _unquote(ident: str) -> str:
    """Strip the quotes of one SQL identifier."""
    ident = ident.strip()
    if ident[:1] == '"':
        return ident[1:-1].replace('""', '"')
    if ident[:1] in ("`", "["):
        return ident[1:-1]
    return ident


def _named_constraints(sql: str | None) -> dict[tuple[str, tuple[str, ...]], str]:
    """Map `(kind, columns)` to the name given in `CONSTRAINT name ...` clauses."""
    out: dict[tuple[str, tuple[str, ...]], str] = {}
    for m in _NAMED.finditer(sql or ""):
        kind = m.group(2).split()[0].upper()
        cols = tuple(_unquote(c) for c in m.group(3).split(","))
        out[(kind, cols)] = _unquote(m.group(1))
    return out


def _columns(rows: list[Any]) -> tuple[list[Column], tuple[str, ...]]:
    """Build the columns and the primary key from `table_xinfo` rows."""
    visible = [r for r in rows if r[6] == 0]
    pk = tuple(r[1] for r in sorted((r for r in visible if r[5]), key=lambda r: r[5]))
    cols = []
    for r in visible:
        ident = len(pk) == 1 and r[5] == 1 and " ".join(r[2].upper().split()) == "INTEGER"
        cols.append(Column(r[1], " ".join(r[2].upper().split()), not (r[3] or r[5]), r[4], ident))
    return cols, pk


async def _index_columns(conn: Conn, index: str) -> tuple[str | None, ...]:
    """Return the column names of one index, in order (`None` for an expression)."""
    rows = await conn.fetch(f"PRAGMA index_info({_quote(index)})", [], tuple)
    return tuple(r[2] for r in sorted(rows, key=lambda r: r[0]))


def _predicate(sql: str) -> str:
    """Return the text after the top-level `WHERE` of a `CREATE INDEX` statement."""
    depth = 0
    for m in re.finditer(r"[()]|\s+WHERE\s+", sql, re.IGNORECASE):
        if m.group() in "()":
            depth += 1 if m.group() == "(" else -1
        elif depth == 0:
            return normalize_where(sql[m.end() :])
    return ""


async def _where(conn: Conn, index: str) -> str | None:
    """Return the normalised predicate of a partial index, or None."""
    rows = await conn.fetch("SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?", [index], tuple)
    return (_predicate(rows[0][0]) or None) if rows and rows[0][0] else None


async def _keys(
    conn: Conn, table: str, names: dict[tuple[str, tuple[str, ...]], str]
) -> tuple[list[tuple[str, tuple[str, ...]]], list[Index]]:
    """Read the unique constraints and the plain indexes of one table."""
    uniques: list[tuple[str, tuple[str, ...]]] = []
    indexes: list[Index] = []
    for r in await _pragma(conn, "index_list", table):
        cols = await _index_columns(conn, r[1])
        if r[3] == "pk" or None in cols:
            continue
        if r[3] == "u":
            key = tuple(c for c in cols if c)
            uniques.append((names.get(("UNIQUE", key), f"{table}_{'_'.join(key)}_key"), key))
        else:
            where = await _where(conn, r[1]) if r[4] else None
            indexes.append(Index(r[1], tuple(c for c in cols if c), bool(r[2]), where))
    return sorted(uniques), sorted(indexes, key=lambda i: i.name)


async def _foreign_keys(
    conn: Conn, table: str, names: dict[tuple[str, tuple[str, ...]], str], pks: dict[str, tuple[str, ...]]
) -> list[ForeignKey]:
    """Read the foreign keys of one table, grouped by constraint id."""
    groups: dict[int, list[Any]] = {}
    for r in await _pragma(conn, "foreign_key_list", table):
        groups.setdefault(r[0], []).append(r)
    out = []
    for rows in groups.values():
        rows.sort(key=lambda r: r[1])
        cols = tuple(r[3] for r in rows)
        target = rows[0][2]
        tcols = tuple(r[4] for r in rows) if rows[0][4] else pks.get(target, ())
        name = names.get(("FOREIGN", cols), f"{table}_{'_'.join(cols)}_fkey")
        out.append(ForeignKey(name, cols, target, tcols, rows[0][6].upper()))
    return sorted(out, key=lambda f: f.name)


async def inspect_sqlite(conn: Conn) -> Snapshot:
    """Read the ordinary tables of a SQLite database into a `Snapshot`.

    SQLite does not store constraint names: they come from the `CONSTRAINT`
    clauses of the `CREATE TABLE` text when present, else Korbase's own naming.
    The `korbase_migrations` table is left out.
    """
    tables = await conn.fetch(_TABLES, [], tuple)
    cols = {t[0]: _columns(await _pragma(conn, "table_xinfo", t[0])) for t in tables}
    pks = {n: c[1] for n, c in cols.items()}
    result: dict[str, Table] = {}
    for name, sql in tables:
        names = _named_constraints(sql)
        pk = pks[name]
        uniques, indexes = await _keys(conn, name, names)
        result[name] = Table(
            name=name,
            columns=tuple(cols[name][0]),
            pk=pk or None,
            pk_name=names.get(("PRIMARY", pk), f"{name}_pkey") if pk else None,
            foreign_keys=tuple(await _foreign_keys(conn, name, names, pks)),
            uniques=tuple(uniques),
            indexes=tuple(indexes),
        )
    return Snapshot(result)
