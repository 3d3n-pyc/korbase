"""Load relations onto rows with one extra query each."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from korbase._backend import Conn
from korbase._build import positional_builder, validating_builder
from korbase._expr import RelRef
from korbase._ident import KorbaseError, quote_ident
from korbase._meta import TableMeta, table_meta

__all__ = ["load_relations"]


def _get(row: Any, name: str) -> Any:
    """Read field `name` of a model of any kind."""
    return row[name] if isinstance(row, dict) else getattr(row, name)


def _set(row: Any, name: str, value: Any) -> None:
    """Set field `name` of a model of any kind, even a frozen one."""
    if isinstance(row, dict):
        row[name] = value
    elif hasattr(type(row), "__struct_fields__"):
        import msgspec  # Python < 3.13 refuses object.__setattr__ on a Struct

        msgspec.structs.force_setattr(row, name, value)
    else:
        object.__setattr__(row, name, value)


def _single_pk(meta: TableMeta) -> str:
    """Return the only primary key column, or raise `KorbaseError`."""
    if len(meta.pk) != 1:
        raise KorbaseError(f"relations on composite primary keys are not supported yet ({meta.name!r})")
    return meta.pk[0]


async def _fetch_in(conn: Conn, target: TableMeta, column: str, values: list[Any], validate: bool) -> list[Any]:
    """Fetch the target rows whose `column` is one of `values`."""
    if not values:
        return []
    t = quote_ident(target.name)
    names = ", ".join(f"{t}.{quote_ident(c.name)}" for c in target.columns)
    sql = f"SELECT {names} FROM {t} WHERE {t}.{quote_ident(column)} = ANY($1)"
    builder = validating_builder(target) if validate else positional_builder(target)
    return await conn.fetch(sql, [values], builder)


def _fk_target(meta: TableMeta, field: str, table_name: str) -> str | None:
    """Return the column that `meta`'s `field` references in `table_name`, if marked."""
    for c in meta.columns:
        if c.name == field and c.fk is not None:
            tbl, col = c.fk.target.split(".")
            return col if tbl == table_name else None
    return None


async def _load_one(conn: Conn, rows: list[Any], ref: RelRef, validate: bool) -> None:
    """Load a many-to-one relation: the foreign key is on this side."""
    target = table_meta(ref.rel.target_model)
    fk = ref.rel.fk_field
    key = _fk_target(ref.table, fk, target.name) or _single_pk(target)
    values = list({v for r in rows if (v := _get(r, fk)) is not None})
    found = {_get(t, key): t for t in await _fetch_in(conn, target, key, values, validate)}
    for row in rows:
        _set(row, ref.rel.name, found.get(_get(row, fk)))


async def _load_many(conn: Conn, rows: list[Any], ref: RelRef, validate: bool) -> None:
    """Load a one-to-many relation: the foreign key is on the target."""
    target = table_meta(ref.rel.target_model)
    fk = ref.rel.fk_field
    if fk not in {c.name for c in target.columns}:
        raise KorbaseError(f"relation {ref.rel.name!r}: {target.name!r} has no column {fk!r}")
    pk = _fk_target(target, fk, ref.table.name) or _single_pk(ref.table)
    ids = list({_get(r, pk) for r in rows})
    groups: dict[Any, list[Any]] = {}
    for child in await _fetch_in(conn, target, fk, ids, validate):
        groups.setdefault(_get(child, fk), []).append(child)
    for row in rows:
        _set(row, ref.rel.name, groups.get(_get(row, pk), []))


async def load_relations(conn: Conn, rows: list[Any], relrefs: Sequence[RelRef], validate: bool = False) -> None:
    """Load each of `relrefs` onto `rows`, with one extra query per relation."""
    if not rows:
        return
    for ref in relrefs:
        await (_load_many if ref.rel.many else _load_one)(conn, rows, ref, validate)
