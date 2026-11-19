# Working on korbase

Instructions for AI coding agents working on this repository.

korbase is an async ORM for Postgres, with SQLite to come. It is pure
Python. Models are msgspec Structs, dataclasses or TypedDicts. It runs on
korpg or asyncpg, and has an optional Korvex integration.

## Setup and checks

```sh
uv sync                              # Python, the tools, and the dependencies
```

Before saying a change is done, run what CI runs:

```sh
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Tests need a Postgres server, named by the `KORBASE_TEST_DSN` environment
variable. To start one:

```sh
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:18
```

## Rules

- **Python 3.10 and later.** Write `X | Y` only in annotations, with
  `from __future__ import annotations` at the top of the module. Use
  `Union[X, Y]` and `Optional[X]` elsewhere.
- **msgspec and pydantic are optional.** Import them lazily, only where
  a model of that kind is used, never at the top of a module.
- **Pure Python.** Do not add a compiled dependency to korbase itself.
- **Before 1.0, break the API directly**: no deprecation shims. Every
  change goes in `CHANGELOG.md` under `## Unreleased`, with how to update.
- **New behavior needs a test.**
- **Docstrings are documentation.** Write them for users, in plain English.
- **Code, comments, docs and commit messages are in English.** Commits
  follow Conventional Commits (`feat:`, `fix:`, `docs:`, `perf:`...).
