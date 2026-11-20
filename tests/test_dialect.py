"""Tests for the SQL dialect helpers."""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal
from typing import Optional

import pytest

from korbase._dialect import PG, SQLITE
from korbase._ident import KorbaseError


class Color(enum.Enum):
    RED = "red"


def test_placeholders_and_ops() -> None:
    assert PG.placeholder(2) == "$2"
    assert SQLITE.placeholder(2) == "?2"
    assert PG.any_of('"a"', "$1") == '"a" = ANY($1)'
    assert SQLITE.any_of('"a"', "?1") == '"a" IN (SELECT value FROM json_each(?1))'
    assert (PG.like_op(False), PG.like_op(True)) == ("LIKE", "ILIKE")
    assert (SQLITE.like_op(False), SQLITE.like_op(True)) == ("LIKE", "LIKE")
    assert SQLITE.integer_pk_identity and not PG.integer_pk_identity


@pytest.mark.parametrize(
    ("tp", "sql"),
    [
        (int, "INTEGER"),
        (bool, "INTEGER"),
        (float, "REAL"),
        (str, "TEXT"),
        (uuid.UUID, "TEXT"),
        (Decimal, "TEXT"),
        (dt.date, "TEXT"),
        (dt.time, "TEXT"),
        (dt.datetime, "TEXT"),
        (bytes, "BLOB"),
        (dict, "TEXT"),
        (list, "TEXT"),
        (list[int], "TEXT"),
        (Color, "TEXT"),
        (Optional[int], "INTEGER"),
        (Optional[bytes], "BLOB"),
    ],
)
def test_sqlite_types(tp: object, sql: str) -> None:
    assert SQLITE.sql_type(tp) == sql


def test_unknown_type() -> None:
    with pytest.raises(KorbaseError):
        SQLITE.sql_type(object)
    with pytest.raises(KorbaseError):
        PG.sql_type(int)


_UTC = dt.timezone.utc
_CASES = [
    (bool, True, 1),
    (int, 5, 5),
    (float, 1.5, 1.5),
    (str, "x", "x"),
    (bytes, b"\x00\x01", b"\x00\x01"),
    (uuid.UUID, uuid.UUID(int=7), str(uuid.UUID(int=7))),
    (Decimal, Decimal("1.2345678901234567890123"), "1.2345678901234567890123"),
    (dt.date, dt.date(2024, 2, 29), "2024-02-29"),
    (dt.time, dt.time(1, 2, 3), "01:02:03"),
    (dt.datetime, dt.datetime(2024, 1, 2, 3, 4, 5), "2024-01-02T03:04:05.000000"),
    (dt.datetime, dt.datetime(2024, 1, 2, 3, 4, 5, 6, _UTC), "2024-01-02T03:04:05.000006+00:00"),
    (dict, {"b": 1, "a": [1, 2]}, '{"b":1,"a":[1,2]}'),
    (list[int], [3, 1], "[3,1]"),
    (Color, Color.RED, "red"),
    (Optional[uuid.UUID], uuid.UUID(int=1), str(uuid.UUID(int=1))),
    (Optional[int], None, None),
]


@pytest.mark.parametrize(("tp", "value", "stored"), _CASES)
def test_round_trip(tp: object, value: object, stored: object) -> None:
    encoded = SQLITE.encode_param(value)
    assert encoded == stored
    assert SQLITE.decode_value(tp, encoded) == value


def test_aware_datetime_converted_to_utc() -> None:
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    value = dt.datetime(2024, 1, 1, 12, 0, tzinfo=tz)
    encoded = SQLITE.encode_param(value)
    assert encoded == "2024-01-01T06:30:00.000000+00:00"
    back = SQLITE.decode_value(dt.datetime, encoded)
    assert back == value and back.utcoffset() == dt.timedelta(0)


def test_any_of_array_params() -> None:
    assert SQLITE.encode_param([1, 2]) == "[1,2]"
    ids = [uuid.UUID(int=1), uuid.UUID(int=2)]
    assert SQLITE.encode_param(ids) == f'["{ids[0]}","{ids[1]}"]'


def test_json_unsupported_raises() -> None:
    with pytest.raises(TypeError):
        SQLITE.encode_param({"a": object()})


def test_pg_is_identity() -> None:
    value = dt.datetime(2024, 1, 1)
    assert PG.encode_param(value) is value
    assert PG.decode_value(dt.datetime, "raw") == "raw"


def test_encoded_datetimes_sort_in_time_order() -> None:
    zones = [_UTC, dt.timezone(dt.timedelta(hours=-8)), dt.timezone(dt.timedelta(hours=9))]
    base = dt.datetime(2024, 5, 1, 0, 0, tzinfo=_UTC)
    moments = [base + dt.timedelta(seconds=s * 3607, microseconds=s) for s in range(30)]
    shuffled = [m.astimezone(zones[i % 3]) for i, m in enumerate(reversed(moments))]
    texts = sorted(SQLITE.encode_param(m) for m in shuffled)
    assert [SQLITE.decode_value(dt.datetime, t) for t in texts] == moments


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Plain(enum.Enum):
    A = 1
    B = 2


def test_sqlite_enum_types_and_values() -> None:
    assert SQLITE.sql_type(Level) == "INTEGER"
    assert SQLITE.sql_type(Plain) == "INTEGER"
    assert SQLITE.sql_type(Color) == "TEXT"
    assert SQLITE.encode_param(Level.HIGH) == 2
    assert type(SQLITE.encode_param(Level.HIGH)) is int
    assert SQLITE.decode_value(Level, 2) is Level.HIGH
    assert SQLITE.decode_value(Color, "red") is Color.RED


def test_sqlite_enum_decode_failure() -> None:
    with pytest.raises(KorbaseError, match="Level"):
        SQLITE.decode_value(Level, 9)


def test_sqlite_nan_rejected_inf_ok() -> None:
    with pytest.raises(KorbaseError, match="NaN"):
        SQLITE.encode_param(float("nan"))
    assert SQLITE.encode_param(float("inf")) == float("inf")
    assert SQLITE.encode_param(float("-inf")) == float("-inf")


def test_sqlite_bytes_in_list_rejected() -> None:
    with pytest.raises(KorbaseError, match="bytes"):
        SQLITE.encode_param([b"a"])
