"""SQL identifier checks. Names are validated, never escaped."""

from __future__ import annotations

import re

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


class KorbaseError(Exception):
    """Base class for every error Korbase raises.

    It lives here for now, until a dedicated errors module exists.
    """


def validate_ident(name: str) -> str:
    """Return `name` if it is a safe SQL identifier, else raise `KorbaseError`.

    A safe identifier is 1 to 63 ASCII letters, digits or underscores and
    does not start with a digit.
    """
    if not isinstance(name, str) or _IDENT.fullmatch(name) is None:
        raise KorbaseError(f"invalid SQL identifier: {name!r}")
    return name


def quote_ident(name: str) -> str:
    """Return `name` validated and wrapped in double quotes."""
    return f'"{validate_ident(name)}"'
