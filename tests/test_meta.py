"""Tests for table metadata and identifier validation."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Optional, TypedDict

import pytest

from korbase._ident import KorbaseError, quote_ident, validate_ident
from korbase._meta import (
    FK,
    PK,
    Default,
    Index,
    Rel,
    SqlType,
    Unique,
    constraint_name,
    table,
    table_meta,
)


@table("users")
@dataclasses.dataclass
class UserDC:
    id: Annotated[int, PK()]
    email: Annotated[str, Unique()]
    bio: Optional[str] = None


@table("comments", indexes=(Index("user_id", unique=True, name="ix_c"),))
@dataclasses.dataclass
class CommentDC:
    id: Annotated[int, PK()]
    user_id: Annotated[int, FK("users.id"), Default(0)]
    user: Annotated[Optional[UserDC], Rel("user_id")] = None


class TD(TypedDict):
    id: Annotated[int, PK()]
    name: str


def test_dataclass_meta() -> None:
    m = table_meta(UserDC)
    assert m.kind == "dataclass" and m.name == "users"
    assert [c.name for c in m.columns] == ["id", "email", "bio"]
    assert m.pk == ("id",)
    assert m.columns[1].unique and m.columns[2].nullable
    assert m.columns[2].python_type is str
    assert m.relations == ()


def test_fk_default_relation_one() -> None:
    m = table_meta(CommentDC)
    col = m.columns[1]
    assert col.fk == FK("users.id") and col.default == Default(0)
    assert not m.relations[0].many and m.relations[0].target_model is UserDC
    assert m.indexes[0].unique and m.indexes[0].name == "ix_c"


def test_relation_many() -> None:
    @table("posts")
    @dataclasses.dataclass
    class Post:
        id: Annotated[int, PK()]
        comments: Annotated[list[CommentDC], Rel("post_id")] = dataclasses.field(default_factory=list)

    rel = table_meta(Post).relations[0]
    assert rel.many and rel.target_model is CommentDC and rel.fk_field == "post_id"


def test_typeddict() -> None:
    m = table("t")(TD).__korbase_table__  # type: ignore[attr-defined]
    assert m.kind == "typeddict"
    assert [c.name for c in m.columns] == ["id", "name"]


def test_struct() -> None:
    msgspec = pytest.importorskip("msgspec")

    class S(msgspec.Struct):
        a: Annotated[int, PK()]
        b: Annotated[int, PK()]
        c: Optional[int] = None

    m = table_meta(table("s")(S))
    assert m.kind == "struct" and m.pk == ("a", "b")
    assert m.columns[2].nullable


def test_composite_pk_dataclass() -> None:
    @dataclasses.dataclass
    class C:
        a: Annotated[int, PK()]
        b: Annotated[int, PK()]

    assert table_meta(table("c")(C)).pk == ("a", "b")


def test_no_pk() -> None:
    class T(TypedDict):
        a: int

    with pytest.raises(KorbaseError, match="primary key"):
        table("t")(T)


def test_unknown_index_column() -> None:
    class T(TypedDict):
        a: Annotated[int, PK()]

    with pytest.raises(KorbaseError, match="unknown column"):
        table("t", indexes=(Index("zz"),))(T)


def test_rel_missing_field() -> None:
    @dataclasses.dataclass
    class T:
        id: Annotated[int, PK()]
        p: Annotated[Optional[UserDC], Rel("nope")] = None

    with pytest.raises(KorbaseError, match="missing field"):
        table("t")(T)


def test_invalid_names() -> None:
    with pytest.raises(KorbaseError):
        table("bad name")
    with pytest.raises(KorbaseError):
        FK("nodot")
    with pytest.raises(KorbaseError):
        FK("a.b-c")

    class T(TypedDict):
        a: Annotated[int, PK()]

    with pytest.raises(KorbaseError):
        table("t", indexes=(Index("a", name="x y"),))(T)


def test_unsupported_kind() -> None:
    class Plain:
        pass

    with pytest.raises(KorbaseError, match="unsupported"):
        table("t")(Plain)


def test_undecorated() -> None:
    class Plain:
        pass

    with pytest.raises(KorbaseError, match="not a Korbase table"):
        table_meta(Plain)


@pytest.mark.parametrize("name", ["a", "_x1", "A" * 63])
def test_ident_ok(name: str) -> None:
    assert validate_ident(name) == name
    assert quote_ident(name) == f'"{name}"'


@pytest.mark.parametrize("name", ["", "A" * 64, "é", "1a", 'a"b', "a b", "a\n"])
def test_ident_bad(name: str) -> None:
    with pytest.raises(KorbaseError):
        validate_ident(name)
    with pytest.raises(KorbaseError):
        quote_ident(name)


@pytest.mark.parametrize("sql", ["varchar(80)", "numeric(10, 2)", "text[]", "double precision"])
def test_sqltype_valid(sql):
    assert SqlType(sql).sql == sql


@pytest.mark.parametrize("sql", ["Text", "text; drop", "", "varchar(x)", "text\n", 5])
def test_sqltype_invalid(sql):
    with pytest.raises(KorbaseError):
        SqlType(sql)


def test_sqltype_on_column():
    @table("st")
    @dataclasses.dataclass
    class M:
        id: Annotated[int, PK()]
        n: Annotated[str, SqlType("varchar(80)")]

    assert table_meta(M).columns[1].sql_type == "varchar(80)"
    assert table_meta(M).columns[0].sql_type is None


def test_identity_automatic_and_explicit():
    @table("ia")
    @dataclasses.dataclass
    class A:
        id: Annotated[int, PK()]

    @table("ib")
    @dataclasses.dataclass
    class B:
        id: Annotated[int, PK(identity=False)]

    @table("ic")
    @dataclasses.dataclass
    class C:
        id: Annotated[str, PK()]

    assert table_meta(A).columns[0].identity is True
    assert table_meta(B).columns[0].identity is False
    assert table_meta(C).columns[0].identity is False


def test_identity_true_invalid():
    with pytest.raises(KorbaseError):

        @table("ie")
        @dataclasses.dataclass
        class D:
            id: Annotated[str, PK(identity=True)]

    with pytest.raises(KorbaseError):

        @table("if_")
        @dataclasses.dataclass
        class E:
            a: Annotated[int, PK(identity=True)]
            b: Annotated[int, PK()]


def test_composite_pk_not_identity():
    @table("comp")
    @dataclasses.dataclass
    class F:
        a: Annotated[int, PK()]
        b: Annotated[int, PK()]

    assert not any(c.identity for c in table_meta(F).columns)


def test_fk_on_delete():
    assert FK("a.b").on_delete == "no action"
    assert FK("a.b", "cascade").on_delete == "cascade"
    with pytest.raises(KorbaseError):
        FK("a.b", "boom")  # type: ignore[arg-type]


def test_fk_set_null_needs_nullable():
    with pytest.raises(KorbaseError):

        @table("sn")
        @dataclasses.dataclass
        class G:
            id: Annotated[int, PK()]
            u: Annotated[int, FK("users.id", "set null")]

    @table("sn2")
    @dataclasses.dataclass
    class H:
        id: Annotated[int, PK()]
        u: Annotated[Optional[int], FK("users.id", "set null")] = None


def test_default_rules():
    assert Default(3).value == 3 and Default(None).value is None
    assert Default(sql="now()").sql == "now()"
    assert Default("x").value == "x" and Default(True).value is True
    with pytest.raises(KorbaseError):
        Default(1, sql="now()")
    for bad in (float("nan"), float("inf"), [1], {}, object()):
        with pytest.raises(KorbaseError):
            Default(bad)


def test_constraint_name():
    assert constraint_name("t", "pkey", ["id"]) == "t_pkey"
    assert constraint_name("t", "fkey", ["a", "b"]) == "t_a_b_fkey"
    assert constraint_name("t", "key", ["a"]) == "t_a_key"
    assert constraint_name("t", "idx", ["a", "b"]) == "t_a_b_idx"
    with pytest.raises(KorbaseError):
        constraint_name("t" * 60, "idx", ["abcd"])


def test_long_generated_names_rejected():
    with pytest.raises(KorbaseError):

        @table("t" * 40)
        @dataclasses.dataclass
        class L:
            id: Annotated[int, PK()]
            ccccccccccccccccccccccccc: Annotated[str, Unique()]

    with pytest.raises(KorbaseError):

        @table("lx", indexes=(Index("id", name="x" * 64),))
        @dataclasses.dataclass
        class M2:
            id: Annotated[int, PK()]
