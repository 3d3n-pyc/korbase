"""Tests for the SQLite inspector."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from korbase._inspect_sqlite import inspect_sqlite
from korbase._schema import Column, ForeignKey, Index, Snapshot
from korbase._sqlite import SqliteBackend

_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email text  NOT NULL, age INTEGER DEFAULT 18,
  note TEXT, UNIQUE (email));
CREATE TABLE korbase_migrations (id INTEGER PRIMARY KEY);
CREATE TABLE posts (a INTEGER NOT NULL, b INTEGER NOT NULL, user_id INTEGER, slug TEXT,
  CONSTRAINT "posts_pk" PRIMARY KEY (a, b),
  CONSTRAINT "posts_user_fk" FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
  CONSTRAINT "posts_slug_uq" UNIQUE (slug));
CREATE INDEX posts_user_idx ON posts (user_id);
CREATE UNIQUE INDEX posts_ab_idx ON posts (b, a);
CREATE TABLE plain (x INTEGER, y TEXT, FOREIGN KEY (x) REFERENCES users);
CREATE TABLE "we""ird" (id INTEGER PRIMARY KEY, v TEXT);
CREATE INDEX "we""ird_v" ON "we""ird" (v);
"""


def _inspect(path: Path) -> Snapshot:
    async def go() -> Snapshot:
        pool = await SqliteBackend().connect(f"sqlite:///{path}")
        try:
            async with pool.acquire() as conn:
                return await inspect_sqlite(conn)
        finally:
            await pool.close()

    return asyncio.run(go())


def test_inspect_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.close()
    snap = _inspect(path)
    assert sorted(snap.tables) == ["plain", "posts", "users", 'we"ird']

    users = snap.tables["users"]
    assert users.columns == (
        Column("id", "INTEGER", False, None, True),
        Column("email", "TEXT", False, None, False),
        Column("age", "INTEGER", True, "18", False),
        Column("note", "TEXT", True, None, False),
    )
    assert (users.pk, users.pk_name) == (("id",), "users_pkey")
    assert users.uniques == (("users_email_key", ("email",)),)
    assert users.foreign_keys == () and users.indexes == ()

    posts = snap.tables["posts"]
    assert not any(c.identity for c in posts.columns)
    assert (posts.pk, posts.pk_name) == (("a", "b"), "posts_pk")
    assert posts.foreign_keys == (ForeignKey("posts_user_fk", ("user_id",), "users", ("id",), "CASCADE"),)
    assert posts.uniques == (("posts_slug_uq", ("slug",)),)
    assert posts.indexes == (
        Index("posts_ab_idx", ("b", "a"), True),
        Index("posts_user_idx", ("user_id",), False),
    )

    plain = snap.tables["plain"]
    assert plain.pk is None and plain.pk_name is None
    assert plain.foreign_keys == (ForeignKey("plain_x_fkey", ("x",), "users", ("id",), "NO ACTION"),)


def test_quote_in_table_name(tmp_path: Path) -> None:
    path = tmp_path / "q.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.close()
    weird = _inspect(path).tables['we"ird']
    assert weird.pk == ("id",)
    assert weird.indexes == (Index('we"ird_v', ("v",), False),)
