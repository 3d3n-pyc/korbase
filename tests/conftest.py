"""Shared test fixtures for korbase."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def dsn() -> str:
    """Return the Postgres DSN for database tests.

    Set KORBASE_TEST_DSN (see tests/pg/start.sh). When it is unset the
    calling test is skipped, so the suite still runs without a database.
    """
    value = os.environ.get("KORBASE_TEST_DSN")
    if not value:
        pytest.skip("KORBASE_TEST_DSN is not set; run tests/pg/start.sh")
    return value
