# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Pyright-only checks for typed columns. Not collected by pytest."""

from __future__ import annotations

from korbase import Column, param


class PostColumns:
    """Typed column view of a post."""

    title: Column[str]
    id: Column[int]


p = PostColumns()

_ = p.title == "x"
_ = p.id == 1
_ = p.id == None  # noqa: E711
_ = p.id == param("i")
_ = p.id < 3
_ = p.id.in_([1, 2])
_ = p.id.in_(param("ids"))
_ = p.title.like("a%")
_ = p.title.ilike("a%")

_ = p.title == 1  # type: ignore
_ = p.id == "x"  # type: ignore
_ = p.id < "x"  # type: ignore
_ = p.id.in_(["a"])  # type: ignore
_ = p.id.like("a%")  # type: ignore
