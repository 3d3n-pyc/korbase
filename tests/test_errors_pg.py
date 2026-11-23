"""Postgres errors are re-raised as Korbase errors (skipped without KORBASE_TEST_DSN)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import asyncpg
import msgspec
import pytest

from korbase import (
    PK,
    CheckViolation,
    ConnectionFailed,
    Database,
    DatabaseError,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
    insert,
    table,
)

SCHEMA = [
    "DROP TABLE IF EXISTS kbe_child, kbe_users, kbe_defer CASCADE",
    "CREATE TABLE kbe_users (id int PRIMARY KEY, email text CONSTRAINT kbe_email_key UNIQUE,"
    " age int CONSTRAINT kbe_age_ck CHECK (age >= 0), name text NOT NULL DEFAULT 'n')",
    "CREATE TABLE kbe_child (id int PRIMARY KEY, uid int CONSTRAINT kbe_child_fk REFERENCES kbe_users (id))",
    "CREATE TABLE kbe_defer (uid int CONSTRAINT kbe_defer_fk REFERENCES kbe_users (id) DEFERRABLE INITIALLY DEFERRED)",
    "INSERT INTO kbe_users (id, email, age) VALUES (1, 'secret@example.com', 5)",
]


@table("kbe_users")
class Person(msgspec.Struct):
    id: Annotated[int, PK()]
    email: str


def _run(dsn: str, body: Callable[[Database], Awaitable[Any]]) -> None:
    """Build the schema, run `body`, then drop the schema."""

    async def main() -> None:
        async with await Database.connect(dsn) as db:
            for sql in SCHEMA:
                await db.execute(sql)
            try:
                await body(db)
            finally:
                await db.execute("DROP TABLE IF EXISTS kbe_child, kbe_users, kbe_defer CASCADE")

    asyncio.run(main())


def _check(info: pytest.ExceptionInfo[Any], constraint: str | None) -> None:
    """Check the cause, the constraint name and that no row value leaks."""
    assert isinstance(info.value.__cause__, asyncpg.PostgresError)
    assert info.value.constraint == constraint
    assert "secret@example.com" not in str(info.value)


def test_unique_through_run(dsn: str) -> None:
    async def body(db: Database) -> None:
        with pytest.raises(UniqueViolation) as info:
            await db.run(insert(Person).values(id=2, email="secret@example.com"))
        _check(info, "kbe_email_key")

    _run(dsn, body)


def test_other_violations_through_execute(dsn: str) -> None:
    async def body(db: Database) -> None:
        with pytest.raises(ForeignKeyViolation) as fk:
            await db.execute("INSERT INTO kbe_child VALUES (1, 99)")
        _check(fk, "kbe_child_fk")
        with pytest.raises(NotNullViolation) as nn:
            await db.execute("INSERT INTO kbe_users (id, name) VALUES (3, NULL)")
        assert isinstance(nn.value.__cause__, asyncpg.PostgresError)
        with pytest.raises(CheckViolation) as ck:
            await db.execute("INSERT INTO kbe_users (id, age) VALUES (4, -1)")
        _check(ck, "kbe_age_ck")

    _run(dsn, body)


def test_other_methods_and_plain_errors(dsn: str) -> None:
    async def body(db: Database) -> None:
        with pytest.raises(DatabaseError) as info:
            await db.fetch("SELECT * FROM kbe_missing")
        assert isinstance(info.value.__cause__, asyncpg.PostgresError)
        with pytest.raises(UniqueViolation):
            await db.fetchrow("INSERT INTO kbe_users (id) VALUES (1) RETURNING id")
        with pytest.raises(UniqueViolation):
            await db.execute("INSERT INTO kbe_users (id) VALUES (1); INSERT INTO kbe_users (id) VALUES (1)")

    _run(dsn, body)


def test_error_inside_transaction(dsn: str) -> None:
    async def body(db: Database) -> None:
        with pytest.raises(UniqueViolation):
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO kbe_users (id) VALUES (7)")
                await tx.execute("INSERT INTO kbe_users (id) VALUES (1)")
        assert await db.fetch("SELECT id FROM kbe_users WHERE id = 7") == []

    _run(dsn, body)


def test_deferred_constraint_fails_at_commit(dsn: str) -> None:
    async def body(db: Database) -> None:
        with pytest.raises(ForeignKeyViolation) as info:
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO kbe_defer VALUES (99)")
        assert info.value.constraint == "kbe_defer_fk"

    _run(dsn, body)


def test_foreign_exceptions_pass_through(dsn: str) -> None:
    async def body(db: Database) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            async with db.transaction():
                raise RuntimeError("boom")

    _run(dsn, body)


def test_bad_port_is_connection_failed() -> None:
    with pytest.raises(ConnectionFailed) as info:
        asyncio.run(Database.connect("postgresql://korbase:korbase@127.0.0.1:1/none"))
    assert info.value.__cause__ is not None


def test_client_side_input_error_hides_the_value(dsn: str) -> None:
    async def body(db: Database) -> None:
        with pytest.raises(DatabaseError) as info:
            await db.execute("INSERT INTO kbe_users (id) VALUES ($1)", "secret-token-123")
        assert str(info.value) == "invalid value for query argument $1"
        assert "secret-token-123" not in str(info.value)

    _run(dsn, body)


def test_interface_error_is_translated(dsn: str) -> None:
    async def body(db: Database) -> None:
        async with db.transaction() as tx:
            raw = tx._bound._conn  # type: ignore[attr-defined]
            task = asyncio.ensure_future(raw.execute("SELECT pg_sleep(0.3)"))
            await asyncio.sleep(0.05)
            with pytest.raises(DatabaseError) as info:
                await tx.execute("SELECT 1")
            assert isinstance(info.value.__cause__, asyncpg.InterfaceError)
            assert not isinstance(info.value, ConnectionFailed)
            await task

    _run(dsn, body)


def test_command_timeout_has_a_message(dsn: str) -> None:
    async def main() -> None:
        async with await Database.connect(dsn, command_timeout=0.1) as db:
            with pytest.raises(ConnectionFailed, match="timed out") as info:
                await db.execute("SELECT pg_sleep(2)")
            assert isinstance(info.value.__cause__, asyncio.TimeoutError)

    asyncio.run(main())
