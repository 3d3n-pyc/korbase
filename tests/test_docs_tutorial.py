"""The tutorial (docs/tutorial.md) stays true as korbase changes: its snippets
parse, and its "The whole script" block runs against a real Postgres, in a
database created for the test and dropped after."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

TUTORIAL = Path(__file__).parent.parent / "docs" / "tutorial.md"
TOP_AWAIT = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


def whole_script() -> str:
    """The Python block under the heading "The whole script"."""
    pattern = r"^## The whole script\n.*?^```python\n(.*?)^```"
    match = re.search(pattern, TUTORIAL.read_text(), re.S | re.M)
    assert match, "no 'The whole script' block in docs/tutorial.md"
    return match.group(1)


def test_step_snippets_parse() -> None:
    """Every Python block parses; step blocks are indented parts of main()."""
    blocks = re.findall(r"^```python\n(.*?)^```", TUTORIAL.read_text(), re.S | re.M)
    assert len(blocks) >= 8
    for code in blocks:
        compile(textwrap.dedent(code), "tutorial.md", "exec", flags=ast.PyCF_ONLY_AST | TOP_AWAIT)


def _admin(dsn: str, statement: str) -> None:
    """Run one statement on the server of `dsn`."""
    import asyncpg

    async def run() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(statement)
        finally:
            await conn.close()

    asyncio.run(run())


def test_whole_script_runs(dsn: str, tmp_path: Path) -> None:
    """The script applies its migration and prints what the tutorial shows."""
    pytest.importorskip("asyncpg")
    pytest.importorskip("msgspec")
    name = f"korbase_tutorial_{uuid.uuid4().hex[:8]}"
    parts = urlsplit(dsn)
    scratch = urlunsplit(parts._replace(path=f"/{name}"))
    script = tmp_path / "blog.py"
    script.write_text(whole_script())
    _admin(dsn, f'CREATE DATABASE "{name}"')
    try:
        env = {**os.environ, "KORBASE_DSN": scratch}
        outputs = []
        for _ in range(2):  # the second run must give the same output, minus the migration line
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            outputs.append(result.stdout.splitlines())
    finally:
        _admin(dsn, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    first, second = outputs
    assert first[0] == "applied 0001_create_blog_tables.sql"
    assert first[1:] == second
    assert second[-1] == "['Hello, world']"
    assert (tmp_path / "migrations" / "0001_create_blog_tables.sql").exists()
