"""Row builders: turn positional rows into model instances."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from korbase._backend import RowBuilder
from korbase._ident import KorbaseError
from korbase._meta import TableMeta, table_meta

__all__ = ["positional_builder", "named_builder", "validating_builder"]

_NAMED: dict[tuple[type, tuple[str, ...]], RowBuilder[Any]] = {}


def positional_builder(meta: TableMeta) -> RowBuilder[Any]:
    """Return a builder for rows in column order, with no validation."""
    model = meta.model
    if meta.kind == "typeddict":
        names = tuple(c.name for c in meta.columns)
        return lambda row: dict(zip(names, row, strict=True))
    return lambda row: model(*row)


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

    def build(row: Sequence[Any]) -> Any:
        """Build one model from a row."""
        data = {c: row[i] for i, c in picks}
        return data if kind == "typeddict" else model(**data)

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
