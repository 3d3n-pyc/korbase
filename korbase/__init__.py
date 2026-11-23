"""Async ORM for Postgres and SQLite, built the Korvex way.

@table("users")
class User(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str

db = await Database.connect(dsn)
users = await db.run(select(User).where(cols(User).name == param("n")), n="Ada")
"""

from __future__ import annotations

from importlib.metadata import version

from korbase._backend import Backend
from korbase._db import Database, Transaction
from korbase._expr import Aggregate, Column, Expr, Param, RelRef, cols, count, param
from korbase._ident import KorbaseError
from korbase._meta import FK, PK, Default, Index, Rel, SqlType, Unique, table
from korbase._query import delete, insert, select, update, upsert

__all__ = [
    "table",
    "PK",
    "FK",
    "Unique",
    "Default",
    "SqlType",
    "Index",
    "Rel",
    "Column",
    "Aggregate",
    "Expr",
    "Param",
    "RelRef",
    "cols",
    "param",
    "count",
    "select",
    "insert",
    "update",
    "delete",
    "upsert",
    "Database",
    "Transaction",
    "KorbaseError",
    "Backend",
    "__version__",
]

__version__: str = version("korbase")
