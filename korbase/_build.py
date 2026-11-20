"""Row builders: turn positional rows into model instances."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Sequence
from typing import Any

from korbase._backend import RowBuilder
from korbase._ident import KorbaseError
from korbase._meta import TableMeta, table_meta

__all__ = ["positional_builder", "named_builder", "validating_builder"]

_NAMED: dict[tuple[type, tuple[str, ...]], RowBuilder[Any]] = {}


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


def positional_builder(meta: TableMeta) -> RowBuilder[Any]:
    """Return a builder for rows in column order, with no validation."""
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


def named_builder(model: type, column_names: Sequence[str]) -> RowBuilder[Any]:
    """Return a builder mapping result columns by name to `model` fields.

    Extra columns are ignored. Raises `KorbaseError` if a required field
    has no column.
    """
    key = (model, tuple(column_names))
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

    _NAMED[key] = build
    return build


def validating_builder(meta: TableMeta) -> RowBuilder[Any]:
    """Return a builder that validates each row with `msgspec.convert`."""
    import msgspec

    names = tuple(c.name for c in meta.columns)
    model = meta.model

    def build(row: Sequence[Any]) -> Any:
        """Validate one row and build the model."""
        return msgspec.convert(dict(zip(names, row, strict=True)), model, from_attributes=True)

    return build
