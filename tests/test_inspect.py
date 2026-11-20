"""Tests for schema inspection against a real Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from korbase._asyncpg import AsyncpgBackend
from korbase._inspect import inspect
from korbase._schema import Column, ForeignKey, Index, Snapshot, Table, normalize_where

SCHEMA = "korbase_test_inspect"

DDL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
CREATE TABLE {SCHEMA}.users (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email text NOT NULL UNIQUE,
    name varchar(80),
    balance numeric(10,2) NOT NULL DEFAULT 0,
    meta jsonb,
    tags text[],
    created_at timestamptz NOT NULL DEFAULT now(),
    role text NOT NULL DEFAULT 'user'
);
CREATE INDEX users_name_idx ON {SCHEMA}.users (name);
CREATE UNIQUE INDEX users_name_role_key ON {SCHEMA}.users (name, role);
CREATE TABLE {SCHEMA}.memberships (
    user_id bigint NOT NULL REFERENCES {SCHEMA}.users (id) ON DELETE CASCADE,
    group_id integer NOT NULL,
    PRIMARY KEY (user_id, group_id)
);
CREATE TABLE {SCHEMA}.korbase_migrations (id integer);
"""


def _run(fn: Callable[[Any], Awaitable[None]], dsn: str) -> None:
    """Open a pool, run `fn` on one connection, and drop the test schema."""

    async def main() -> None:
        pool = await AsyncpgBackend().connect(dsn)
        try:
            async with pool.acquire() as conn:
                try:
                    await fn(conn)
                finally:
                    await conn.execute_script(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        finally:
            await pool.close()

    asyncio.run(main())


def test_inspect_snapshot(dsn: str) -> None:
    """A schema with every feature is read back exactly."""

    async def check(conn: Any) -> None:
        await conn.execute_script(DDL)
        snap = await inspect(conn, SCHEMA)
        users = Table(
            name="users",
            columns=(
                Column("id", "bigint", False, None, True),
                Column("email", "text", False, None, False),
                Column("name", "character varying(80)", True, None, False),
                Column("balance", "numeric(10,2)", False, "0", False),
                Column("meta", "jsonb", True, None, False),
                Column("tags", "text[]", True, None, False),
                Column("created_at", "timestamp with time zone", False, "now()", False),
                Column("role", "text", False, "'user'::text", False),
            ),
            pk=("id",),
            pk_name="users_pkey",
            uniques=(("users_email_key", ("email",)),),
            indexes=(
                Index("users_name_idx", ("name",), False),
                Index("users_name_role_key", ("name", "role"), True),
            ),
        )
        memberships = Table(
            name="memberships",
            columns=(
                Column("user_id", "bigint", False, None, False),
                Column("group_id", "integer", False, None, False),
            ),
            pk=("user_id", "group_id"),
            pk_name="memberships_pkey",
            foreign_keys=(ForeignKey("memberships_user_id_fkey", ("user_id",), "users", ("id",), "CASCADE"),),
        )
        assert snap == Snapshot({"users": users, "memberships": memberships})

    _run(check, dsn)


def test_execute_script_two_statements(dsn: str) -> None:
    """execute_script runs several statements in one call."""

    async def check(conn: Any) -> None:
        await conn.execute_script(
            f"CREATE SCHEMA {SCHEMA}; CREATE TABLE {SCHEMA}.t (id integer); INSERT INTO {SCHEMA}.t VALUES (1)"
        )
        rows = await conn.fetch(f"SELECT id FROM {SCHEMA}.t", [], tuple)
        assert rows == [(1,)]

    _run(check, dsn)


def test_normalize_where() -> None:
    """Whitespace collapses and one outer paren level goes; others stay."""
    assert normalize_where("  a   IS\nNOT NULL ") == "a IS NOT NULL"
    assert normalize_where("(due IS NOT NULL)") == "due IS NOT NULL"
    assert normalize_where("((a))") == "(a)"
    assert normalize_where("(a) AND (b)") == "(a) AND (b)"


def test_inspect_partial_index(dsn: str) -> None:
    """A partial index comes back with its predicate; a plain one has None."""

    async def check(conn: Any) -> None:
        await conn.execute_script(
            f"CREATE SCHEMA {SCHEMA}; CREATE TABLE {SCHEMA}.t (id integer, due date);"
            f"CREATE INDEX t_due ON {SCHEMA}.t (due) WHERE due IS NOT NULL;"
            f"CREATE INDEX t_id ON {SCHEMA}.t (id)"
        )
        idx = {i.name: i for i in (await inspect(conn, SCHEMA)).tables["t"].indexes}
        assert idx["t_due"].where == "due IS NOT NULL"
        assert idx["t_id"].where is None

    _run(check, dsn)
