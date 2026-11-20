"""Plain data describing a database schema, as read from Postgres."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Column", "ForeignKey", "Index", "Table", "Snapshot"]


@dataclass(frozen=True)
class Column:
    """One column: type as Postgres prints it, default as `pg_get_expr` prints it."""

    name: str
    type: str
    nullable: bool
    default: str | None
    identity: bool


@dataclass(frozen=True)
class ForeignKey:
    """A foreign key constraint."""

    name: str
    columns: tuple[str, ...]
    target_table: str
    target_columns: tuple[str, ...]
    on_delete: str


@dataclass(frozen=True)
class Index:
    """An index that does not back a primary key or unique constraint."""

    name: str
    columns: tuple[str, ...]
    unique: bool


@dataclass(frozen=True)
class Table:
    """One table: columns in order, keys and indexes sorted by name."""

    name: str
    columns: tuple[Column, ...]
    pk: tuple[str, ...] | None
    pk_name: str | None
    foreign_keys: tuple[ForeignKey, ...] = ()
    uniques: tuple[tuple[str, tuple[str, ...]], ...] = ()
    indexes: tuple[Index, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """All tables of a schema, by name."""

    tables: dict[str, Table] = field(default_factory=dict)
