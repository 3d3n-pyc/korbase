"""The agent skill (.agents/skills/korbase/SKILL.md) stays true as korbase
changes: its snippets parse, every name it imports from korbase exists, and
its complete script runs against a real Postgres."""

from __future__ import annotations

import ast
import asyncio
import importlib
import re
import sys
import textwrap
import types
from pathlib import Path
from typing import Any

SKILL = Path(__file__).parent.parent / ".agents" / "skills" / "korbase" / "SKILL.md"
BLOCK = re.compile(r"^( *)```python\n(.*?)^\1```", re.S | re.M)
TOP_AWAIT = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


def snippets() -> list[str]:
    """Every Python block of the skill, dedented."""
    return [textwrap.dedent(code) for _, code in BLOCK.findall(SKILL.read_text())]


def test_snippets_parse_and_import_what_exists() -> None:
    """Each block parses and each name imported from korbase exists."""
    for code in snippets():
        tree = compile(code, "SKILL.md", "exec", flags=ast.PyCF_ONLY_AST | TOP_AWAIT)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("korbase"):
                module = importlib.import_module(node.module)
                for alias in node.names:
                    assert hasattr(module, alias.name), f"{node.module}.{alias.name}"


def test_complete_script_runs(dsn: str) -> None:
    """The models, queries and script run together and pass their check."""
    found = {
        k: next(c for c in snippets() if k in c) for k in ("class Book", "BY_AUTHOR =", "async def main", "test_script")
    }
    models, queries, script, check = found.values()
    module = types.ModuleType("skill_app")
    sys.modules["skill_app"] = module
    ns: dict[str, Any] = module.__dict__
    try:
        for name, code in (("models", models), ("queries", queries), ("script", script), ("check", check)):
            exec(compile(code, f"SKILL.md {name}", "exec"), ns)
        ns["test_script"](dsn)
    finally:
        asyncio.run(_drop(dsn))
        del sys.modules["skill_app"]


async def _drop(dsn: str) -> None:
    """Drop the tables the script created."""
    from korbase import Database

    async with await Database.connect(dsn) as db:
        await db.execute("DROP TABLE IF EXISTS books, authors")


def test_packaged_copy_is_identical() -> None:
    """The copy shipped in the wheel matches the skill."""
    packaged = SKILL.parent.parent.parent.parent / "korbase" / "_skill" / "SKILL.md"
    assert packaged.read_bytes() == SKILL.read_bytes(), (
        "copy .agents/skills/korbase/SKILL.md to korbase/_skill/SKILL.md"
    )
