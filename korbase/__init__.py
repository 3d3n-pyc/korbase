"""Async ORM for Postgres, built the Korvex way."""

from __future__ import annotations

from importlib.metadata import version

__all__ = ["__version__"]

__version__: str = version("korbase")
