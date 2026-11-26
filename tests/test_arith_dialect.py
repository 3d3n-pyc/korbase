"""Tests for the dialect hook that renders arithmetic value operands."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from korbase._dialect import PG, SQLITE
from korbase._ident import KorbaseError


def test_postgres_operands() -> None:
    assert PG.arith_operand("$1", 3) == "$1"
    assert PG.arith_operand("$1", 1.5) == "$1::float8"
    assert PG.arith_operand("$1", Decimal("1.5")) == "$1::numeric"


def test_sqlite_operands() -> None:
    assert SQLITE.arith_operand("?1", 3) == "?1"
    assert SQLITE.arith_operand("?1", 1.5) == "?1"


def test_sqlite_refuses_decimal() -> None:
    with pytest.raises(KorbaseError, match="Decimal values is not supported on SQLite"):
        SQLITE.arith_operand("?1", Decimal("1.5"))


@pytest.mark.parametrize("dialect", [PG, SQLITE])
@pytest.mark.parametrize("value", [True, False, None, "1", b"1", uuid.uuid4(), dt.date(2020, 1, 1)])
def test_other_types_refused(dialect, value: object) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(KorbaseError, match="arithmetic needs a number"):
        dialect.arith_operand("$1", value)
