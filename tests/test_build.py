"""Tests for row builders and the asyncpg backend."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Annotated, Optional, TypedDict

import msgspec
import pytest

from korbase import Database
from korbase._asyncpg import AsyncpgBackend
from korbase._build import named_builder, positional_builder, validating_builder
from korbase._ident import KorbaseError
from korbase._meta import PK, Rel, table, table_meta


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


@table("kw")
class KwStruct(msgspec.Struct, kw_only=True):
    id: Annotated[int, PK()]
    name: str


@table("kw")
@dataclasses.dataclass(frozen=True)
class InitFalse:
    id: Annotated[int, PK()]
    name: str = dataclasses.field(init=False)


@table("kw")
class RelFirst(msgspec.Struct):
    other: Annotated[Optional[PersonStruct], Rel("id")] = None
    id: Annotated[int, PK()] = 0
    name: str = ""


def test_positional_kw_only_struct() -> None:
    assert positional_builder(table_meta(KwStruct))((1, "a")) == KwStruct(id=1, name="a")


def test_positional_init_false_field() -> None:
    obj = positional_builder(table_meta(InitFalse))((1, "a"))
    assert (obj.id, obj.name) == (1, "a")


def test_positional_rel_declared_first() -> None:
    obj = positional_builder(table_meta(RelFirst))((7, "x"))
    assert (obj.id, obj.name, obj.other) == (7, "x", None)


def test_named_init_false_field() -> None:
    obj = named_builder(InitFalse, ["name", "id"])(("a", 1))
    assert (obj.id, obj.name) == (1, "a")


@table("kw_items")
class KwDefaults(msgspec.Struct, kw_only=True):
    a: int = 1
    b: Annotated[int, PK()]
    c: int = 3


def test_named_reports_missing_required_kw_only_field() -> None:
    """A required kw_only field after defaulted ones is still required."""
    with pytest.raises(KorbaseError, match="missing columns for KwDefaults: b"):
        named_builder(KwDefaults, ["a", "c"])


@pytest.mark.parametrize("validate", [False, True])
def test_sqlite_select_builds_kw_only_struct(tmp_path: Path, validate: bool) -> None:
    """Rows from SQLite build kw_only structs correctly, positional or validating."""

    async def main() -> None:
        """Create a table, insert a row and read it back."""
        dsn = f"sqlite:///{tmp_path / 'kw.db'}"
        async with await Database.connect(dsn, validate=validate) as db:
            await db.execute("CREATE TABLE kw_items (a INTEGER, b INTEGER, c INTEGER)")
            await db.execute("INSERT INTO kw_items (a, b, c) VALUES (10, 20, 30)")
            rows = await db.fetch("SELECT a, b, c FROM kw_items", model=KwDefaults)
            assert rows == [KwDefaults(a=10, b=20, c=30)]

    asyncio.run(main())
