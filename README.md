# korbase

An async ORM for Postgres, with SQLite to come. It is pure Python.

korbase is pre-alpha. Nothing works yet.

- **Models** are msgspec Structs, dataclasses or TypedDicts.
- **Drivers**: runs on [korpg](https://github.com/3d3n-pyc/korpg) or asyncpg.
- **Korvex**: optional integration with the
  [Korvex](https://github.com/3d3n-pyc/korvex) web framework.
- **Python**: 3.10 and later.

## Setup

```sh
uv sync                              # Python, the tools, and the dependencies
```

## Tests

Tests need a Postgres server, given by the `KORBASE_TEST_DSN` environment
variable:

```sh
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:18
export KORBASE_TEST_DSN=postgresql://postgres:postgres@localhost:5432/postgres
uv run pytest
```

## Contributing

See [AGENTS.md](AGENTS.md) for the rules and the checks CI runs. Every
change is listed in [CHANGELOG.md](CHANGELOG.md).
