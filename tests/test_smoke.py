"""Smoke test: the package imports and exposes a version string."""

import korbase


def test_version_is_a_string() -> None:
    assert isinstance(korbase.__version__, str)
