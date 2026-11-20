"""Models for the generated typed columns check. Not collected by pytest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from korbase import PK, Rel, table


@table("authors")
@dataclass
class Author:
    """An author."""

    id: Annotated[int, PK()]
    name: str


@table("posts")
@dataclass
class Post:
    """A post."""

    id: Annotated[int, PK()]
    title: str
    author_id: int
    subtitle: str | None = None
    author: Annotated[Author | None, Rel("author_id")] = None
