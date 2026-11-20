# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Pyright-only checks for the generated columns. Not collected by pytest."""

from __future__ import annotations

from gen_models import Author, Post
from gen_models_cols import cols

from korbase import Column, RelRef

title: Column[str] = cols(Post).title
subtitle: Column[str | None] = cols(Post).subtitle
author: RelRef[Author] = cols(Post).author
_ = cols(Post).title == "x"

_ = cols(Post).titel  # type: ignore
bad: Column[int] = cols(Post).title  # type: ignore
