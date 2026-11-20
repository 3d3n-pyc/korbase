"""Table metadata: markers, the `table` decorator and `table_meta`."""

from __future__ import annotations

import dataclasses
import math
import re
import sys
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

from korbase._ident import KorbaseError, validate_ident

__all__ = [
    "PK",
    "FK",
    "Unique",
    "Default",
    "SqlType",
    "constraint_name",
    "Index",
    "Rel",
    "ColumnMeta",
    "RelMeta",
    "TableMeta",
    "table",
    "table_meta",
]

_ATTR = "__korbase_table__"
_SQL_TYPE = re.compile(r"[a-z][a-z0-9_ ]*(\(\d+(,\s*\d+)?\))?(\[\])?")
_ON_DELETE = ("cascade", "restrict", "set null", "set default", "no action")
_UNSET: Any = object()


@dataclass(frozen=True)
class PK:
    """Marks a field as (part of) the primary key.

    `identity=None` (the default) makes a single integer key an identity
    column. `True` forces it and `False` turns it off. `True` is only valid
    on a single integer key.
    """

    identity: bool | None = None


@dataclass(frozen=True)
class SqlType:
    """Overrides the SQL type of a column, for example `SqlType("varchar(80)")`.

    The type must be lowercase, such as `numeric(10, 2)`, `text[]` or
    `double precision`. Anything else raises `KorbaseError`.
    """

    sql: str

    def __post_init__(self) -> None:
        """Check that the type is a plain lowercase SQL type."""
        if not isinstance(self.sql, str) or _SQL_TYPE.fullmatch(self.sql) is None:
            raise KorbaseError(f"invalid SQL type: {self.sql!r}")


@dataclass(frozen=True)
class FK:
    """Marks a field as a foreign key to `"table.column"`.

    `on_delete` is one of `"cascade"`, `"restrict"`, `"set null"`,
    `"set default"` and `"no action"` (the default).
    """

    target: str
    on_delete: Literal["cascade", "restrict", "set null", "set default", "no action"] = "no action"

    def __post_init__(self) -> None:
        """Check that the target is two valid identifiers and on_delete is known."""
        parts = self.target.split(".") if isinstance(self.target, str) else []
        if len(parts) != 2:
            raise KorbaseError(f"FK target must be 'table.column': {self.target!r}")
        for part in parts:
            validate_ident(part)
        if self.on_delete not in _ON_DELETE:
            raise KorbaseError(f"invalid FK on_delete: {self.on_delete!r}")


@dataclass(frozen=True)
class Unique:
    """Marks a column as unique."""


@dataclass(frozen=True)
class Default:
    """Stores a column default, used by migrations.

    Give a literal `value` (None, bool, finite int or float, or str) or a raw
    `sql` expression, not both. `sql` is written as is into the migration, so
    it must come from trusted code, never from user input. With neither, the
    default is NULL.
    """

    value: Any = _UNSET
    sql: str | None = dataclasses.field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Check that exactly one of `value` and `sql` is set and valid."""
        if self.value is _UNSET:
            object.__setattr__(self, "value", None if self.sql is None else _UNSET)
        elif self.sql is not None:
            raise KorbaseError("Default takes a value or sql, not both")
        if self.sql is not None and not isinstance(self.sql, str):
            raise KorbaseError("Default sql must be a string")
        if self.sql is None and not _literal_ok(self.value):
            raise KorbaseError(f"unsupported Default value: {self.value!r}")


def _literal_ok(value: Any) -> bool:
    """Return True if `value` can be a literal column default."""
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int)


@dataclass(frozen=True)
class Index:
    """An index over one or more columns of the table."""

    columns: tuple[str, ...]
    unique: bool = False
    name: str | None = None

    def __init__(self, *columns: str, unique: bool = False, name: str | None = None) -> None:
        """Build an index on `columns`."""
        object.__setattr__(self, "columns", tuple(columns))
        object.__setattr__(self, "unique", unique)
        object.__setattr__(self, "name", name)


@dataclass(frozen=True)
class Rel:
    """Marks a field as a relation, not a column."""

    fk_field: str


@dataclass(frozen=True)
class ColumnMeta:
    """One column of a table."""

    name: str
    python_type: Any
    nullable: bool
    pk: bool
    fk: FK | None
    unique: bool
    default: Default | None
    sql_type: str | None = None
    identity: bool = False


@dataclass(frozen=True)
class RelMeta:
    """One relation of a table. The target model is resolved on first use."""

    name: str
    target: Any
    fk_field: str
    many: bool
    owner: Any = None
    _cache: list[Any] = dataclasses.field(default_factory=list, repr=False, compare=False)

    @property
    def target_model(self) -> Any:
        """Return the related model, resolving a forward reference on first use."""
        if not self._cache:
            self._cache.append(self._resolve())
        return self._cache[0]

    def _resolve(self) -> Any:
        """Evaluate the target in the owner's module; raise `KorbaseError` if it fails."""
        target = self.target
        if isinstance(target, type):
            return target
        if isinstance(target, typing.ForwardRef):
            target = target.__forward_arg__
        module = sys.modules.get(getattr(self.owner, "__module__", ""))
        scope = dict(vars(module)) if module else {}
        if self.owner is not None:
            scope.setdefault(self.owner.__name__, self.owner)
        try:
            found = eval(target, scope) if isinstance(target, str) else target  # noqa: S307
        except Exception:
            found = None
        if not isinstance(found, type):
            raise KorbaseError(f"relation {self.name!r}: cannot resolve target {target!r}")
        return found


@dataclass(frozen=True)
class TableMeta:
    """Everything Korbase knows about one model."""

    name: str
    model: type
    kind: Literal["struct", "dataclass", "typeddict"]
    columns: tuple[ColumnMeta, ...]
    pk: tuple[str, ...]
    relations: tuple[RelMeta, ...]
    indexes: tuple[Index, ...]


def _kind_and_names(model: type) -> tuple[str, list[str]]:
    """Return the model kind and its field names in declaration order."""
    if hasattr(model, "__struct_fields__"):
        return "struct", list(model.__struct_fields__)
    if dataclasses.is_dataclass(model):
        return "dataclass", [f.name for f in dataclasses.fields(model)]
    if typing.is_typeddict(model):
        return "typeddict", list(_hints(model))
    raise KorbaseError(f"unsupported model kind: {model!r}")


def _split(hint: Any) -> tuple[Any, tuple[Any, ...]]:
    """Split `Annotated[T, ...]` into the type and its metadata."""
    if typing.get_origin(hint) is Annotated:
        args = typing.get_args(hint)
        return args[0], tuple(args[1:])
    return hint, ()


def _strip_optional(tp: Any) -> tuple[Any, bool]:
    """Return the type without `None` and whether `None` was allowed."""
    if typing.get_origin(tp) in (Union, types.UnionType):
        args = typing.get_args(tp)
        rest = tuple(a for a in args if a is not type(None))
        if len(rest) < len(args) and len(rest) == 1:
            return rest[0], True
    return tp, False


def _relation(model: type, name: str, tp: Any, rel: Rel) -> RelMeta:
    """Build a `RelMeta` from a relation field's type."""
    if typing.get_origin(tp) is list:
        return RelMeta(name, typing.get_args(tp)[0], rel.fk_field, True, model)
    return RelMeta(name, tp, rel.fk_field, False, model)


def _hints(model: type) -> dict[str, Any]:
    """Return the type hints; names not defined yet become forward references."""
    local: dict[str, Any] = {}
    while True:
        try:
            return typing.get_type_hints(model, localns=local or None, include_extras=True)
        except NameError as exc:
            if not exc.name or exc.name in local:
                raise
            local[exc.name] = typing.ForwardRef(exc.name)


def _column(name: str, tp: Any, nullable: bool, marks: tuple[Any, ...]) -> ColumnMeta:
    """Build a `ColumnMeta` from a field's type and markers."""
    fks = [m for m in marks if isinstance(m, FK)]
    defaults = [m for m in marks if isinstance(m, Default)]
    sql_types = [m for m in marks if isinstance(m, SqlType)]
    if fks and fks[0].on_delete == "set null" and not nullable:
        raise KorbaseError(f"column {name!r}: 'set null' needs a nullable column")
    return ColumnMeta(
        name=validate_ident(name),
        python_type=tp,
        nullable=nullable,
        pk=any(isinstance(m, PK) for m in marks),
        fk=fks[0] if fks else None,
        unique=any(isinstance(m, Unique) for m in marks),
        default=defaults[0] if defaults else None,
        sql_type=sql_types[0].sql if sql_types else None,
    )


def _resolve_identity(columns: list[ColumnMeta], raw: dict[str, bool | None]) -> list[ColumnMeta]:
    """Resolve each PK's identity flag; reject identity on non-integer or composite keys."""
    single = sum(c.pk for c in columns) == 1
    out = []
    for col in columns:
        wanted = raw.get(col.name)
        int_key = col.pk and single and col.python_type is int
        if wanted is True and not int_key:
            raise KorbaseError(f"PK(identity=True) needs a single integer key: {col.name!r}")
        flag = bool(int_key and col.sql_type is None) if wanted is None else bool(wanted)
        out.append(dataclasses.replace(col, identity=flag))
    return out


def _build(model: type, names: list[str]) -> tuple[list[ColumnMeta], list[RelMeta]]:
    """Split a model's fields into columns and relations."""
    hints = _hints(model)
    columns: list[ColumnMeta] = []
    relations: list[RelMeta] = []
    for name in names:
        tp, marks = _split(hints[name])
        tp, nullable = _strip_optional(tp)
        rels = [m for m in marks if isinstance(m, Rel)]
        if rels:
            relations.append(_relation(model, validate_ident(name), tp, rels[0]))
        else:
            columns.append(_column(name, tp, nullable, marks))
    return columns, relations


def _raw_identity(model: type, names: list[str]) -> dict[str, bool | None]:
    """Return the `identity` value of each field's PK marker."""
    hints = _hints(model)
    raw: dict[str, bool | None] = {}
    for name in names:
        for mark in _split(hints[name])[1]:
            if isinstance(mark, PK):
                raw[name] = mark.identity
    return raw


def constraint_name(table: str, kind: str, cols: tuple[str, ...] | list[str] = ()) -> str:
    """Return the fixed name of a constraint or index.

    `kind` is `pkey`, `fkey`, `key` or `idx`. Raises `KorbaseError` if the
    name is longer than 63 bytes.
    """
    name = f"{table}_pkey" if kind == "pkey" else f"{table}_{'_'.join(cols)}_{kind}"
    return validate_ident(name)


def _check_names(table_name: str, columns: list[ColumnMeta], indexes: tuple[Index, ...]) -> None:
    """Raise `KorbaseError` if a name this table would produce is too long."""
    constraint_name(table_name, "pkey")
    for col in columns:
        if col.fk is not None:
            constraint_name(table_name, "fkey", (col.name,))
        if col.unique:
            constraint_name(table_name, "key", (col.name,))
    for index in indexes:
        if index.name is None:
            constraint_name(table_name, "idx", index.columns)


def _check(
    table_name: str,
    columns: list[ColumnMeta],
    relations: list[RelMeta],
    indexes: tuple[Index, ...],
) -> None:
    """Raise `KorbaseError` if the table is inconsistent."""
    known = {c.name for c in columns}
    if not any(c.pk for c in columns):
        raise KorbaseError(f"table {table_name!r} has no primary key")
    for rel in relations:
        if not rel.many and rel.fk_field not in known:
            raise KorbaseError(f"relation {rel.name!r} points to missing field {rel.fk_field!r}")
    for index in indexes:
        if not index.columns:
            raise KorbaseError("an Index needs at least one column")
        for col in index.columns:
            if col not in known:
                raise KorbaseError(f"Index uses unknown column {col!r}")
        if index.name is not None:
            validate_ident(index.name)
    _check_names(table_name, columns, indexes)


_Model = typing.TypeVar("_Model", bound=type)


def table(name: str, *, indexes: tuple[Index, ...] = ()) -> Callable[[_Model], _Model]:
    """Declare a model as the table `name`.

    Works on msgspec Structs, dataclasses and TypedDicts. Fields marked
    `PK`, `FK`, `Unique`, `Default` or `Rel` inside `Annotated[...]` shape
    the table. Raises `KorbaseError` if the model is not valid.
    """
    validate_ident(name)
    indexes = tuple(indexes)

    def decorate(model: _Model) -> _Model:
        """Attach the table metadata to `model`."""
        kind, names = _kind_and_names(model)
        columns, relations = _build(model, names)
        _check(name, columns, relations, indexes)
        columns = _resolve_identity(columns, _raw_identity(model, names))
        pk = tuple(c.name for c in columns if c.pk)
        meta = TableMeta(name, model, kind, tuple(columns), pk, tuple(relations), indexes)  # type: ignore[arg-type]
        setattr(model, _ATTR, meta)
        return model

    return decorate


def table_meta(model: type) -> TableMeta:
    """Return the `TableMeta` of a model decorated with `@table`."""
    meta = model.__dict__.get(_ATTR) if isinstance(model, type) else None
    if meta is None:
        raise KorbaseError(f"{model!r} is not a Korbase table; use @table")
    return meta
