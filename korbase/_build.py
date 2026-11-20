"""Row builders: turn positional rows into model instances."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Sequence
from functools import partial
from typing import Any, Optional

from korbase._backend import RowBuilder
from korbase._dialect import PG, Dialect, _strip_optional
from korbase._ident import KorbaseError
from korbase._meta import TableMeta, table_meta

__all__ = ["positional_builder", "named_builder", "validating_builder"]

_NAMED: dict[tuple[type, tuple[str, ...], str], RowBuilder[Any]] = {}
_PLAIN = (int, float, str, bytes)


def _positional_params(model: type) -> list[str]:
    """Return the names `model` accepts positionally, in order."""
    if hasattr(model, "__struct_fields__"):
        return list(model.__match_args__)  # type: ignore[attr-defined]
    try:
        params = inspect.signature(model).parameters.values()
    except (TypeError, ValueError):
        return []
    return [p.name for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]


def _positional_safe(meta: TableMeta) -> bool:
    """Tell whether `model(*row)` maps a row onto the columns correctly."""
    cols = [c.name for c in meta.columns]
    return _positional_params(meta.model)[: len(cols)] == cols


def _init_names(model: type) -> set[str]:
    """Return the names `model.__init__` accepts."""
    if hasattr(model, "__struct_fields__"):
        return set(model.__struct_fields__)  # type: ignore[attr-defined]
    return set(inspect.signature(model).parameters)


def _maker(model: type, names: Sequence[str]) -> Any:
    """Return `make(values)` building a `model` from values for `names`.

    Fields that `__init__` does not accept (`init=False`) are set afterwards.
    """
    accepted = _init_names(model)
    late = [(i, n) for i, n in enumerate(names) if n not in accepted]
    if not late:
        keys = tuple(names)
        return lambda values: model(**dict(zip(keys, values, strict=True)))
    early = [(i, n) for i, n in enumerate(names) if n in accepted]

    def make(values: Sequence[Any]) -> Any:
        """Build one model, then set the `init=False` fields."""
        obj = model(**{n: values[i] for i, n in early})
        for i, n in late:
            object.__setattr__(obj, n, values[i])
        return obj

    return make


def _decoders(meta: TableMeta, names: Sequence[str], dialect: Dialect) -> list[tuple[int, Any]]:
    """Return `(index, decode)` for the result columns `names` that need decoding."""
    types = {c.name: _strip_optional(c.python_type) for c in meta.columns}
    return [
        (i, partial(dialect.decode_value, types[n]))
        for i, n in enumerate(names)
        if n in types and types[n] not in _PLAIN
    ]


def _decoding(build: RowBuilder[Any], pairs: list[tuple[int, Any]]) -> RowBuilder[Any]:
    """Wrap `build` so the columns in `pairs` are decoded first."""
    if not pairs:
        return build

    def decode(row: Sequence[Any]) -> Any:
        """Decode the stored values of one row, then build the model."""
        values = list(row)
        for i, fn in pairs:
            values[i] = fn(values[i])
        return build(values)

    return decode


def decoding_rows(model: type, names: Sequence[str], dialect: Dialect) -> Any:
    """Return `decode(row)` giving the row with its stored values converted (raw SQL with a model)."""
    pairs = _decoders(table_meta(model), names, dialect)

    def decode(row: Sequence[Any]) -> Sequence[Any]:
        """Convert the stored values of one row."""
        values = list(row)
        for i, fn in pairs:
            values[i] = fn(values[i])
        return values

    return decode


def positional_builder(meta: TableMeta, dialect: Optional[Dialect] = None) -> RowBuilder[Any]:
    """Return a builder for rows in column order, with no validation."""
    dialect = dialect or PG
    names = tuple(c.name for c in meta.columns)
    build: RowBuilder[Any] = _positional(meta)
    return build if dialect is PG else _decoding(build, _decoders(meta, names, dialect))


def _positional(meta: TableMeta) -> RowBuilder[Any]:
    """Return the undecoded positional builder."""
    model = meta.model
    names = tuple(c.name for c in meta.columns)
    if meta.kind == "typeddict":
        return lambda row: dict(zip(names, row, strict=True))
    if _positional_safe(meta):
        return lambda row: model(*row)
    return _maker(model, names)


def _field_names(model: type) -> tuple[list[str], set[str]]:
    """Return all field names and those with no Python default."""
    meta = table_meta(model)
    names = [c.name for c in meta.columns]
    if meta.kind == "typeddict":
        return names, set(model.__required_keys__)  # type: ignore[attr-defined]
    if meta.kind == "struct":
        fields = model.__struct_fields__  # type: ignore[attr-defined]
        n_default = len(model.__struct_defaults__)  # type: ignore[attr-defined]
        return names, set(fields[: len(fields) - n_default])
    required = {
        f.name
        for f in dataclasses.fields(model)  # type: ignore[arg-type]
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    return names, required


def named_builder(model: type, column_names: Sequence[str], dialect: Dialect = PG) -> RowBuilder[Any]:
    """Return a builder mapping result columns by name to `model` fields.

    Extra columns are ignored. Raises `KorbaseError` if a required field
    has no column.
    """
    key = (model, tuple(column_names), dialect.name)
    cached = _NAMED.get(key)
    if cached is not None:
        return cached
    names, required = _field_names(model)
    missing = [n for n in names if n in required and n not in column_names]
    if missing:
        raise KorbaseError(f"missing columns for {model.__name__}: {', '.join(missing)}")
    picks = [(i, c) for i, c in enumerate(column_names) if c in names]
    kind = table_meta(model).kind
    make = None if kind == "typeddict" else _maker(model, [c for _, c in picks])
    idx = [i for i, _ in picks]

    def build(row: Sequence[Any]) -> Any:
        """Build one model from a row."""
        if make is None:
            return {c: row[i] for i, c in picks}
        return make([row[i] for i in idx])

    result: RowBuilder[Any] = build
    if dialect is not PG:
        result = _decoding(build, _decoders(table_meta(model), column_names, dialect))
    _NAMED[key] = result
    return result


def validating_builder(meta: TableMeta, dialect: Optional[Dialect] = None) -> RowBuilder[Any]:
    """Return a builder that validates each row with `msgspec.convert`."""
    import msgspec

    dialect = dialect or PG

    names = tuple(c.name for c in meta.columns)
    model = meta.model

    def build(row: Sequence[Any]) -> Any:
        """Validate one row and build the model."""
        return msgspec.convert(dict(zip(names, row, strict=True)), model, from_attributes=True)

    return build if dialect is PG else _decoding(build, _decoders(meta, names, dialect))
