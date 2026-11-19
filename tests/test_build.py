"""Tests for row builders and the asyncpg backend."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Annotated, Optional, TypedDict

import msgspec
import pytest

from korbase._asyncpg import AsyncpgBackend
from korbase._build import named_builder, positional_builder, validating_builder
from korbase._ident import KorbaseError
from korbase._meta import PK, table, table_meta


@table("people")
@dataclasses.dataclass
class PersonDC:
    id: Annotated[int, PK()]
    name: str
    bio: Optional[str] = None


@table("people")
class PersonStruct(msgspec.Struct):
    id: Annotated[int, PK()]
    name: str
    bio: Optional[str] = None


@table("people")
class PersonTD(TypedDict):
    id: Annotated[int, PK()]
    name: str
    bio: Optional[str]


@table("notes")
@dataclasses.dataclass
class NoteDC:
    id: Annotated[int, PK()]
    body: Optional[str]


@table("notes")
class NoteStruct(msgspec.Struct):
    id: Annotated[int, PK()]
    body: Optional[str]


@table("notes")
class NoteTD(TypedDict):
    id: Annotated[int, PK()]
    body: Optional[str]


@table("counts")
@dataclasses.dataclass
class CountDC:
    id: Annotated[int, PK()]
    n: int = 5
    tags: list = dataclasses.field(default_factory=list)


@table("counts")
class CountStruct(msgspec.Struct):
    id: Annotated[int, PK()]
    n: int = 5


class _CountBase(TypedDict):
    id: Annotated[int, PK()]


@table("counts")
class CountTD(_CountBase, total=False):
    n: int


@table("scores")
@dataclasses.dataclass
class ScoreDC:
    id: Annotated[int, PK()]
    score: float


def test_positional_builds_dataclass() -> None:
    build = positional_builder(table_meta(PersonDC))
    assert build((1, "Ann", None)) == PersonDC(1, "Ann", None)


def test_positional_builds_struct() -> None:
    build = positional_builder(table_meta(PersonStruct))
    assert build((1, "Ann", "hi")) == PersonStruct(1, "Ann", "hi")


def test_positional_builds_typeddict() -> None:
    build = positional_builder(table_meta(PersonTD))
    assert build((1, "Ann", None)) == {"id": 1, "name": "Ann", "bio": None}


def test_named_maps_by_column_name_and_ignores_extra() -> None:
    build = named_builder(PersonDC, ["name", "extra", "id"])
    assert build(("Ann", "ignored", 1)) == PersonDC(1, "Ann", None)


def test_named_maps_struct_and_typeddict() -> None:
    struct_build = named_builder(PersonStruct, ["bio", "name", "id"])
    assert struct_build((None, "Ann", 1)) == PersonStruct(1, "Ann", None)
    td_build = named_builder(PersonTD, ["id", "name", "bio"])
    assert td_build((2, "Bo", "x")) == {"id": 2, "name": "Bo", "bio": "x"}


def test_named_raises_on_missing_required_column() -> None:
    with pytest.raises(KorbaseError, match="missing columns for PersonDC: name"):
        named_builder(PersonDC, ["id"])


def test_named_uses_default_for_missing_optional_field() -> None:
    build = named_builder(PersonDC, ["id", "name"])
    assert build((1, "Ann")) == PersonDC(1, "Ann", None)


def test_named_missing_nullable_without_default_is_an_error() -> None:
    with pytest.raises(KorbaseError, match="missing columns for NoteDC: body"):
        named_builder(NoteDC, ["id"])
    with pytest.raises(KorbaseError, match="missing columns for NoteStruct: body"):
        named_builder(NoteStruct, ["id"])
    with pytest.raises(KorbaseError, match="missing columns for NoteTD: body"):
        named_builder(NoteTD, ["id"])


def test_named_missing_non_nullable_with_default_uses_default() -> None:
    assert named_builder(CountDC, ["id"])((1,)) == CountDC(1, 5)
    assert named_builder(CountDC, ["id"])((1,)).tags == []
    assert named_builder(CountStruct, ["id"])((1,)) == CountStruct(1, 5)
    assert named_builder(CountTD, ["id"])((1,)) == {"id": 1}


def test_named_builder_is_cached() -> None:
    first = named_builder(PersonDC, ["id", "name"])
    assert named_builder(PersonDC, ["id", "name"]) is first


def test_validating_converts_and_builds() -> None:
    build = validating_builder(table_meta(ScoreDC))
    result = build((1, 3))
    assert result == ScoreDC(1, 3.0)
    assert isinstance(result.score, float)


def test_validating_rejects_wrong_type() -> None:
    build = validating_builder(table_meta(PersonStruct))
    with pytest.raises(msgspec.ValidationError):
        build((1, 2, None))


async def _run_each_kind(dsn: str) -> None:
    """Fetch and execute against each model kind on one real connection."""
    pool = await AsyncpgBackend().connect(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("CREATE TEMP TABLE kb_people (id int PRIMARY KEY, name text, bio text)", [])
            tag = await conn.execute(
                "INSERT INTO kb_people VALUES ($1, $2, $3), ($4, $5, $6)", [1, "Ann", None, 2, "Bo", "x"]
            )
            assert tag == "INSERT 0 2"
            for model in (PersonDC, PersonStruct, PersonTD):
                builder = positional_builder(table_meta(model))
                rows = await conn.fetch("SELECT id, name, bio FROM kb_people ORDER BY id", [], builder)
                assert len(rows) == 2
                row = await conn.fetchrow("SELECT id, name, bio FROM kb_people WHERE id = $1", [2], builder)
                assert row is not None
                assert await conn.fetchrow("SELECT id, name, bio FROM kb_people WHERE id = $1", [99], builder) is None
    finally:
        await pool.close()


def test_asyncpg_each_model_kind(dsn: str) -> None:
    asyncio.run(_run_each_kind(dsn))


async def _run_rollback(dsn: str) -> None:
    """Check that an exception inside transaction() rolls the work back."""
    pool = await AsyncpgBackend().connect(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("CREATE TEMP TABLE kb_tx (id int)", [])
            with pytest.raises(RuntimeError, match="boom"):
                async with conn.transaction():
                    await conn.execute("INSERT INTO kb_tx VALUES ($1)", [1])
                    raise RuntimeError("boom")
            rows = await conn.fetch("SELECT id FROM kb_tx", [], lambda r: r[0])
            assert rows == []
    finally:
        await pool.close()


def test_asyncpg_transaction_rolls_back(dsn: str) -> None:
    asyncio.run(_run_rollback(dsn))
