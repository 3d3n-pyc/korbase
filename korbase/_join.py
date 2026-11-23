"""Helpers for joins: finding the `ON` condition, checking tables, and decoding slices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from korbase._expr import Column, Expr, ParamCollector, compile_expr
from korbase._ident import KorbaseError, quote_ident
from korbase._meta import TableMeta

__all__ = ["Join", "JoinPart", "MAX_JOINS"]

MAX_JOINS = 2
_CHILDREN = ("left", "right", "inner", "column")


@dataclass(frozen=True)
class Join:
    """One joined table: its `meta`, whether it is a `LEFT JOIN`, and the `ON` condition."""

    meta: TableMeta
    left: bool
    on: Expr


@dataclass(frozen=True)
class JoinPart:
    """Where one model sits in a join's output.

    `meta` describes the model, `start` and `stop` slice its columns out of the
    output row, and `left` is true for a `LEFT JOIN`: the model is then `None`
    when all its primary-key columns are NULL.
    """

    meta: TableMeta
    start: int
    stop: int
    left: bool


def column_refs(node: Any) -> list[Column[Any]]:
    """Return every column used inside an expression, order or aggregate."""
    if isinstance(node, Column):
        return [node]
    found: list[Column[Any]] = []
    for name in _CHILDREN:
        child = getattr(node, name, None)
        if child is not None and not isinstance(child, (str, int, float)):
            found += column_refs(child)
    return found


def check_tables(nodes: Any, allowed: set[str]) -> None:
    """Raise `KorbaseError` if a node uses a column of a table not in `allowed`."""
    for node in nodes:
        for col in column_refs(node):
            if col.table.name not in allowed:
                raise KorbaseError(f"column {col.table.name}.{col.name} is from a table that is not in the query")


def table_names(base: TableMeta, joins: tuple[Join, ...]) -> set[str]:
    """Return the names of the base table and the joined tables."""
    return {base.name} | {j.meta.name for j in joins}


def _fk_pairs(new: TableMeta, old: TableMeta) -> list[Expr]:
    """Return `new.col = old.col` for each FK between the two tables, either direction."""
    pairs: list[Expr] = []
    for a, b in ((new, old), (old, new)):
        for col in a.columns:
            if col.fk is not None and col.fk.target.split(".")[0] == b.name:
                target = col.fk.target.split(".")[1]
                pairs.append(Column(a, col.name) == Column(b, target))
    return pairs


def infer_on(new: TableMeta, existing: list[TableMeta]) -> Expr:
    """Return the one FK condition linking `new` to the tables in the query, or raise."""
    found = [p for old in existing for p in _fk_pairs(new, old)]
    if len(found) != 1:
        kind = "no" if not found else "several"
        raise KorbaseError(f"{kind} foreign key links {new.name!r} to the query; pass on=...")
    return found[0]


def make_join(base: TableMeta, joins: tuple[Join, ...], meta: TableMeta, left: bool, on: Any) -> Join:
    """Check a new join against the query and return it, inferring `on` when it is None."""
    if len(joins) >= MAX_JOINS:
        raise KorbaseError(f"at most {MAX_JOINS} joins are supported")
    if meta.name in table_names(base, joins):
        raise KorbaseError(f"table {meta.name!r} is already in the query; self-joins are not supported")
    if on is None:
        on = infer_on(meta, [base] + [j.meta for j in joins])
    elif not isinstance(on, Expr):
        raise KorbaseError(f"on= takes a condition expression, got {type(on).__name__}")
    check_tables([on], table_names(base, joins) | {meta.name})
    return Join(meta, left, on)


def from_sql(base: TableMeta, joins: tuple[Join, ...], ctx: ParamCollector) -> str:
    """Return `"a" JOIN "b" ON ... LEFT JOIN "c" ON ...`."""
    sql = quote_ident(base.name)
    for j in joins:
        kind = "LEFT JOIN" if j.left else "JOIN"
        sql += f" {kind} {quote_ident(j.meta.name)} ON {compile_expr(j.on, ctx)}"
    return sql


def parts_of(base: TableMeta, joins: tuple[Join, ...]) -> tuple[JoinPart, ...]:
    """Return where each model's columns sit in the output row."""
    parts: list[JoinPart] = []
    start = 0
    for meta, left in [(base, False)] + [(j.meta, j.left) for j in joins]:
        parts.append(JoinPart(meta, start, start + len(meta.columns), left))
        start += len(meta.columns)
    return tuple(parts)


def select_names(base: TableMeta, joins: tuple[Join, ...]) -> str:
    """Return every column of every table, qualified, in row order."""
    metas = [base] + [j.meta for j in joins]
    return ", ".join(f"{quote_ident(m.name)}.{quote_ident(c.name)}" for m in metas for c in m.columns)
