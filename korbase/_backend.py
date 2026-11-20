"""Backend protocols: what Korbase needs from a database driver."""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, TypeVar

__all__ = ["Backend", "Pool", "Conn", "RowBuilder"]

M = TypeVar("M")
M_co = TypeVar("M_co", covariant=True)


class RowBuilder(Protocol[M_co]):
    """Turns one result row into a model.

    The row is positional: its values come in the same order as the
    columns of the `SELECT`. Builders never see column names.
    """

    def __call__(self, row: Sequence[Any], /) -> M_co:
        """Build one model from one row."""
        ...


class Conn(Protocol):
    """One database connection."""

    def fetch(self, sql: str, args: Sequence[object], builder: RowBuilder[M]) -> Awaitable[list[M]]:
        """Run `sql` and build one model per row, in `SELECT` order."""
        ...

    def fetchrow(self, sql: str, args: Sequence[object], builder: RowBuilder[M]) -> Awaitable[M | None]:
        """Run `sql` and build the first row, or return `None` if empty."""
        ...

    def fetch_columns(
        self, sql: str, args: Sequence[object]
    ) -> Awaitable[tuple[Sequence[str], list[Sequence[object]]]]:
        """Run `sql` and return the column names (in `SELECT` order) with the rows.

        The names are known even when there are no rows.
        """
        ...

    def execute(self, sql: str, args: Sequence[object]) -> Awaitable[str]:
        """Run `sql` and return the driver's status tag, such as `INSERT 0 1`."""
        ...

    def transaction(self) -> AbstractAsyncContextManager[None]:
        """Open a transaction: commit on success, roll back on error."""
        ...


class Pool(Protocol):
    """A pool of connections."""

    def acquire(self) -> AbstractAsyncContextManager[Conn]:
        """Borrow a connection for the length of an `async with` block."""
        ...

    def close(self) -> Awaitable[None]:
        """Close every connection."""
        ...


class Backend(Protocol):
    """A database driver."""

    def connect(self, dsn: str, **opts: Any) -> Awaitable[Pool]:
        """Open a pool on `dsn`. Extra options go to the driver."""
        ...
