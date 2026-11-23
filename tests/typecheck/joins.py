# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Pyright-only checks for the typing of joins. Not collected by pytest."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from gen_models import Author, Post
from gen_models_cols import cols

from korbase import Database, count, select, table

if sys.version_info >= (3, 11):
    from typing import assert_type
else:
    from typing_extensions import assert_type


@table("tags")
@dataclass
class Tag:
    """A tag on a post."""

    id: int
    label: str


async def check_joins(db: Database) -> None:
    """Assert the result types of join queries. Never executed."""
    pairs = await db.run(select(Post).join(Author))
    assert_type(pairs, list[tuple[Post, Author]])

    maybe = await db.run(select(Post).left_join(Author))
    assert_type(maybe, list[tuple[Post, Author | None]])

    trio = await db.run(select(Post).join(Author).left_join(Tag))
    assert_type(trio, list[tuple[Post, Author, Tag | None]])

    first = await db.first(select(Post).join(Author))
    assert_type(first, tuple[Post, Author] | None)

    counted = await db.run(select(Post).join(Author).columns(cols(Author).id, count()))
    assert_type(counted, list[tuple[int, int]])

    _ = await db.value(select(Post).join(Author))  # type: ignore
