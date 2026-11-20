"""SQL dialect differences between Postgres and SQLite. Pure: no I/O, no drivers."""

from __future__ import annotations

import datetime
import enum
import json
import types
import typing
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from korbase._ident import KorbaseError

__all__ = ["Dialect", "PG", "SQLITE", "sqlite_type"]

_SQLITE_TYPES: dict[Any, str] = {
    bool: "INTEGER",
    int: "INTEGER",
    float: "REAL",
    str: "TEXT",
    bytes: "BLOB",
    uuid.UUID: "TEXT",
    datetime.datetime: "TEXT",
    datetime.date: "TEXT",
    datetime.time: "TEXT",
    Decimal: "TEXT",
    dict: "TEXT",
    list: "TEXT",
}


def _strip_optional(tp: Any) -> Any:
    """Return `X` for `Optional[X]` / `X | None`, else `tp` unchanged."""
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def sqlite_type(py_type: Any) -> str:
    """Return the SQLite column type for a Python type (Optional is ignored).

    Dates, times, UUIDs and Decimals are stored as TEXT, JSON values as TEXT.
    Raises `KorbaseError` for a type with no mapping.
    """
    tp = _strip_optional(py_type)
    found = _SQLITE_TYPES.get(typing.get_origin(tp) or tp)
    if found is None and isinstance(tp, type) and issubclass(tp, enum.Enum):
        ints = all(type(m.value) is int for m in tp)
        found = "INTEGER" if ints and len(tp) else "TEXT"
    if found is None:
        raise KorbaseError(f"no SQLite type for {py_type!r}")
    return found


def _json_default(value: Any) -> Any:
    """Make the non-JSON types that can sit inside a list or dict serializable."""
    if isinstance(value, bytes):
        raise KorbaseError("SQLite cannot put bytes in a JSON value or in_() list; use raw SQL or hex text")
    encoded = _sqlite_encode(value)
    if encoded is value:
        raise TypeError(f"not JSON serializable: {value!r}")
    return encoded


def _sqlite_encode(value: Any) -> Any:
    """Convert a Python value to what SQLite stores (see `Dialect.encode_param`)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, enum.Enum):
        return _sqlite_encode(value.value)
    if isinstance(value, float) and value != value:
        raise KorbaseError("SQLite cannot store NaN")
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            value = value.astimezone(datetime.timezone.utc)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"), default=_json_default)
    return value


def _sqlite_decode(tp: Any, raw: Any) -> Any:
    """Convert a stored SQLite value back to the Python type `tp`."""
    if raw is None:
        return None
    tp = _strip_optional(tp)
    base = typing.get_origin(tp) or tp
    if base is bool:
        return bool(raw)
    if base is datetime.datetime:
        return datetime.datetime.fromisoformat(raw)
    if base is datetime.date:
        return datetime.date.fromisoformat(raw)
    if base is datetime.time:
        return datetime.time.fromisoformat(raw)
    if base is uuid.UUID:
        return uuid.UUID(raw)
    if base is Decimal:
        return Decimal(raw)
    if base in (dict, list):
        return json.loads(raw)
    if isinstance(base, type) and issubclass(base, enum.Enum):
        try:
            return base(raw)
        except ValueError as exc:
            raise KorbaseError(f"cannot decode {raw!r} as {base.__name__}") from exc
    return raw


def _identity(value: Any) -> Any:
    """Return `value` unchanged (Postgres drivers do their own conversion)."""
    return value


def _identity_decode(tp: Any, raw: Any) -> Any:
    """Return `raw` unchanged (Postgres drivers do their own conversion)."""
    return raw


@dataclass(frozen=True)
class Dialect:
    """One SQL dialect: placeholders, operators and value conversion.

    `name` is `"postgres"` or `"sqlite"`. `integer_pk_identity` is true when an
    auto-numbered key must be written `INTEGER PRIMARY KEY` (SQLite).
    `like_ascii_only` is true when case-insensitive LIKE only folds ASCII
    letters (SQLite).
    """

    name: str
    placeholder_prefix: str
    integer_pk_identity: bool
    like_ascii_only: bool

    def placeholder(self, n: int) -> str:
        """Return the numbered parameter marker: `$n` (Postgres) or `?n` (SQLite)."""
        return f"{self.placeholder_prefix}{n}"

    def any_of(self, column_sql: str, ph: str) -> str:
        """Return the SQL testing that a column equals one of an array parameter.

        SQLite receives the array as JSON text and unpacks it with `json_each`.
        """
        if self.name == "sqlite":
            return f"{column_sql} IN (SELECT value FROM json_each({ph}))"
        return f"{column_sql} = ANY({ph})"

    def like_op(self, case_insensitive: bool) -> str:
        """Return `LIKE` or `ILIKE`.

        SQLite has no ILIKE: its LIKE is already case-insensitive, but only for
        ASCII letters, so both cases return `LIKE`.
        """
        if self.name == "sqlite" or not case_insensitive:
            return "LIKE"
        return "ILIKE"

    def encode_param(self, value: Any) -> Any:
        """Convert a Python value to a query parameter (SQLite only; Postgres is a no-op).

        Aware datetimes become UTC text `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, which
        sorts in time order. Lists and dicts become compact JSON text.
        """
        return _sqlite_encode(value) if self.name == "sqlite" else _identity(value)

    def decode_value(self, py_type: Any, raw: Any) -> Any:
        """Convert a stored value back to the field's Python type (Postgres is a no-op)."""
        if self.name == "sqlite":
            return _sqlite_decode(py_type, raw)
        return _identity_decode(py_type, raw)

    def sql_type(self, py_type: Any) -> str:
        """Return the SQLite type of a Python type. Postgres types live in `_ddl`."""
        if self.name != "sqlite":
            raise KorbaseError("Postgres types are mapped by korbase._ddl.sql_type")
        return sqlite_type(py_type)


PG = Dialect("postgres", "$", False, False)
SQLITE = Dialect("sqlite", "?", True, True)
