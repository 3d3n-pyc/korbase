"""Every Python block in docs/ parses and imports only names korbase has."""

from __future__ import annotations

import ast
import importlib
import re
import textwrap
from pathlib import Path

import pytest

DOCS = Path(__file__).parent.parent / "docs"
BLOCK = re.compile(r"^( *)```python\n(.*?)^\1```", re.S | re.M)


def blocks() -> list[tuple[str, str]]:
    """(page, code) for every ```python block in the docs."""
    found = []
    for page in sorted(DOCS.rglob("*.md")):
        for _, code in BLOCK.findall(page.read_text()):
            found.append((page.relative_to(DOCS).as_posix(), textwrap.dedent(code)))
    return found


@pytest.mark.parametrize(("page", "code"), blocks())
def test_block_parses_and_imports_exist(page: str, code: str) -> None:
    tree = ast.parse(code, filename=page)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("korbase"):
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), f"{page}: {node.module} has no {alias.name}"
