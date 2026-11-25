# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Pyright-only checks for the typing of arithmetic. Not collected by pytest."""

from __future__ import annotations

import sys
from decimal import Decimal

from korbase import Column

if sys.version_info >= (3, 11):
    from typing import assert_type
else:
    from typing_extensions import assert_type


class Cols:
    """Typed columns."""

    n: Column[int]
    f: Column[float]
    d: Column[Decimal]
    s: Column[str]
    on: Column[int | None]
    of: Column[float | None]


def check_arith(c: Cols) -> None:
    """Assert the result types of arithmetic. Never executed."""
    assert_type(c.n + 1, "Arith[int]")
    assert_type(c.n + c.n, "Arith[int]")
    assert_type(10 - c.n, "Arith[int]")
    assert_type((c.n + 1) * 2, "Arith[int]")
    assert_type(c.n % 2, "Arith[int]")
    assert_type(c.n / 2.5, "Arith[float]")
    assert_type(c.f * 2, "Arith[float]")
    assert_type(2 * c.f, "Arith[float]")
    assert_type(c.n + c.f, "Arith[float]")
    assert_type(c.d + 1, "Arith[Decimal]")
    assert_type(1 - c.d, "Arith[Decimal]")
    assert_type(c.d * c.d, "Arith[Decimal]")
    assert_type(c.n + c.d, "Arith[Decimal]")
    assert_type(c.on + 1, "Arith[int | None]")
    assert_type(c.n + c.on, "Arith[int | None]")
    assert_type(c.of * 2, "Arith[float | None]")
    _ = c.s + 1  # type: ignore
    _ = c.f % 2  # type: ignore
    _ = c.n + "a"  # type: ignore
