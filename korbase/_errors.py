"""Exceptions raised by Korbase, the same on Postgres and SQLite."""

from __future__ import annotations


class KorbaseError(Exception):
    """Base class for every error Korbase raises."""


class DatabaseError(KorbaseError):
    """An error raised by the database.

    `constraint` is the name of the violated constraint, when the driver gives it.
    """

    def __init__(self, message: str, *, constraint: str | None = None) -> None:
        super().__init__(message)
        self.constraint = constraint


class IntegrityError(DatabaseError):
    """A row broke a constraint of the database."""


class UniqueViolation(IntegrityError):
    """A row repeats a value that must be unique."""


class ForeignKeyViolation(IntegrityError):
    """A row refers to a row that does not exist."""


class NotNullViolation(IntegrityError):
    """A row leaves a required column empty."""


class CheckViolation(IntegrityError):
    """A row fails a CHECK constraint."""


class DatabaseBusy(DatabaseError):
    """The database is locked, a lock could not be taken, or a deadlock hit.

    Retry the transaction.
    """


class ConnectionFailed(DatabaseError):
    """The connection to the database was lost or could not be opened."""
