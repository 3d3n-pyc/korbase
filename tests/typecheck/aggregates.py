# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Pyright-only checks for the typing of aggregates. Not collected by pytest."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date

from gen_models import Post
from gen_models_cols import cols

from korbase import Column, Database, count, select, table

if sys.version_info >= (3, 11):
    from typing import assert_type
else:
    from typing_extensions import assert_type


@table("events")
@dataclass
class Event:
    """An event with a day."""

    id: int
    day: date


class EventColumns:
    """Typed columns of `Event`."""

    id: Column[int]
    day: Column[date]


async def check_aggregates(db: Database, events: EventColumns) -> None:
    """Assert the result types of aggregate queries. Never executed."""
    rows = await db.run(select(Post).columns(cols(Post).id, count()))
    assert_type(rows, list[tuple[int, int]])

    total = await db.value(select(Post).columns(cols(Post).id.sum()))
    assert_type(total, int | None)

    average = await db.value(select(Post).columns(cols(Post).id.avg()))
    assert_type(average, float | None)

    latest = await db.value(select(Event).columns(events.day.max()))
    assert_type(latest, date | None)

    pair = await db.first(select(Post).columns(cols(Post).id, count()))
    assert_type(pair, tuple[int, int] | None)

    n = await db.value(select(Post).columns(count()))
    assert_type(n, int | None)

    _ = await db.value(select(Post).columns(cols(Post).id, count()))  # type: ignore
