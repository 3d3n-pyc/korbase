"""Row builders: turn positional rows into model instances."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Sequence
from functools import partial
from typing import Any, Optional

from korbase._backend import RowBuilder
from korbase._dialect import PG, Dialect, _strip_optional
from korbase._expr import Arith
from korbase._ident import KorbaseError
from korbase._meta import TableMeta, table_meta

__all__ = ["positional_builder", "named_builder", "validating_builder", "tuple_builder", "join_builder"]

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
        import msgspec

        return names, {f.name for f in msgspec.structs.fields(model) if f.required}
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


def _to_float(raw: Any) -> Any:
    """Return `raw` as a float, keeping `None`."""
    return None if raw is None else float(raw)


def _to_int(raw: Any) -> Any:
    """Return `raw` as an int, keeping `None`."""
    return None if raw is None else int(raw)


def _item_decoder(item: Any, column: Any, dialect: Dialect) -> Any:
    """Return the function that decodes one selected item, or None to keep the value."""
    if getattr(item, "kind", None) == "avg":
        return _to_float
    if isinstance(item, Arith):
        return {int: _to_int, float: _to_float}.get(item.py_type)
    if column is None:
        return None
    tp = _strip_optional(next(c.python_type for c in column.table.columns if c.name == column.name))
    if tp is int:
        return _to_int
    if tp in _PLAIN or dialect is PG:
        return None
    return partial(dialect.decode_value, tp)


def tuple_builder(items: Sequence[Any], outputs: Sequence[Any], dialect: Dialect) -> RowBuilder[Any]:
    """Return a builder turning a row of selected columns and aggregates into a typed tuple."""
    fns = [_item_decoder(i, c, dialect) for i, c in zip(items, outputs, strict=True)]
    if not any(fns):
        return tuple
    pairs = [(i, fn) for i, fn in enumerate(fns) if fn is not None]

    def build(row: Sequence[Any]) -> tuple[Any, ...]:
        """Decode one row into a tuple."""
        values = list(row)
        for i, fn in pairs:
            values[i] = fn(values[i])
        return tuple(values)

    return build


def _part_builder(part: Any, validate: bool, dialect: Dialect) -> Any:
    """Return `build(row)` making one model (or `None` for an unmatched left join) from a joined row."""
    meta = part.meta
    make = validating_builder(meta, dialect) if validate else positional_builder(meta, dialect)
    start, stop = part.start, part.stop
    pk = [start + i for i, c in enumerate(meta.columns) if c.pk]
    if not part.left:
        return lambda row: make(row[start:stop])

    def build(row: Sequence[Any]) -> Any:
        """Build the model, or `None` when all its primary-key columns are NULL."""
        if all(row[i] is None for i in pk):
            return None
        return make(row[start:stop])

    return build


def join_builder(parts: Sequence[Any], validate: bool, dialect: Dialect) -> RowBuilder[Any]:
    """Return a builder turning a joined row into a tuple of models, one per `JoinPart`."""
    builders = [_part_builder(p, validate, dialect) for p in parts]
    return lambda row: tuple(b(row) for b in builders)
