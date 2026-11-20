"""Shared test fixtures for korbase."""

from __future__ import annotations

import os
from pathlib import Path

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


@pytest.fixture(params=["sqlite", "pg"], ids=["sqlite", "pg"])
def backend_dsn(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """Return a DSN for each backend: a fresh SQLite file, and Postgres when configured.

    The `pg` variant is skipped when KORBASE_TEST_DSN is unset.
    """
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'test.db'}"
    value = os.environ.get("KORBASE_TEST_DSN")
    if not value:
        pytest.skip("KORBASE_TEST_DSN is not set; run tests/pg/start.sh")
    return value
