"""Filter grammar, sorting and pagination (SPEC §9).

OpenProject takes filters as a JSON array in a query parameter::

    filters=[{"status":{"operator":"o","values":[]}},
             {"dueDate":{"operator":"<>d","values":["","2026-08-01"]}}]

This module owns that serialization and validates operator/filter-type
combinations *locally* — an unsupported combination fails before the request
is sent, with the allowed set listed, instead of coming back as an opaque 400.

Three gotchas are encoded here once and for all:

* OpenProject's ``offset`` parameter is a **1-based page number**, not a record
  offset. :func:`pagination_params` maps ``page`` → ``offset`` directly.
* Booleans are ``"t"`` / ``"f"``; open-ended date ranges use ``<>d`` with an
  empty bound (no sentinel dates).
* Wire names are camelCase; every tool parameter and sort key is snake_case and
  mapped by :func:`to_wire_name`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, Field

from openproject_mcp.client.errors import InputValidationError

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Filter",
    "FilterType",
    "Op",
    "RawFilter",
    "date_range_filter",
    "make_filter",
    "normalize_value",
    "pagination_params",
    "principal_filter",
    "query_params",
    "register_filter_type",
    "serialize_filters",
    "serialize_sort_by",
    "status_filter",
    "to_snake_name",
    "to_wire_name",
    "validate_operator",
]

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class Op(StrEnum):
    """The verified operator vocabulary (SPEC §9.1)."""

    EQ = "="
    NEQ = "!"
    ANY = "*"
    NONE = "!*"
    CONTAINS = "~"
    NOT_CONTAINS = "!~"
    SEARCH = "**"
    OPEN = "o"
    CLOSED = "c"
    TODAY = "t"
    THIS_WEEK = "w"
    DAYS_AGO = "t-"
    DAYS_AHEAD = "t+"
    LESS_DAYS_AHEAD = "<t+"
    MORE_DAYS_AHEAD = ">t+"
    MORE_DAYS_AGO = ">t-"
    LESS_DAYS_AGO = "<t-"
    ON_DATE = "=d"
    BETWEEN_DATES = "<>d"
    GTE = ">="
    LTE = "<="
    ALL_OF = "&="


ALL_OPERATORS: frozenset[str] = frozenset(op.value for op in Op)


class FilterType(StrEnum):
    """Filter value strategies, mirroring OpenProject's strategy classes."""

    LIST = "list"
    LIST_OPTIONAL = "list_optional"
    LIST_ALL = "list_all"
    STATUS = "status"
    RELATION = "relation"
    TEXT = "text"
    SEARCH = "search"
    INTEGER = "integer"
    FLOAT = "float"
    DATE = "date"
    DATETIME_PAST = "datetime_past"
    BOOLEAN = "boolean"


ALLOWED_OPERATORS: dict[FilterType, frozenset[str]] = {
    FilterType.LIST: frozenset({Op.EQ, Op.NEQ}),
    FilterType.LIST_OPTIONAL: frozenset({Op.EQ, Op.NEQ, Op.ANY, Op.NONE}),
    FilterType.LIST_ALL: frozenset({Op.EQ, Op.NEQ, Op.ALL_OF}),
    FilterType.STATUS: frozenset({Op.EQ, Op.NEQ, Op.OPEN, Op.CLOSED, Op.ANY}),
    FilterType.RELATION: frozenset({Op.EQ, Op.ANY, Op.NONE}),
    FilterType.TEXT: frozenset({Op.CONTAINS, Op.NOT_CONTAINS}),
    FilterType.SEARCH: frozenset({Op.SEARCH, Op.CONTAINS}),
    FilterType.INTEGER: frozenset({Op.EQ, Op.NEQ, Op.GTE, Op.LTE}),
    FilterType.FLOAT: frozenset({Op.EQ, Op.NEQ, Op.GTE, Op.LTE}),
    FilterType.DATE: frozenset(
        {
            Op.BETWEEN_DATES,
            Op.ON_DATE,
            Op.TODAY,
            Op.THIS_WEEK,
            Op.DAYS_AGO,
            Op.DAYS_AHEAD,
            Op.MORE_DAYS_AGO,
            Op.LESS_DAYS_AGO,
            Op.MORE_DAYS_AHEAD,
            Op.LESS_DAYS_AHEAD,
        }
    ),
    FilterType.DATETIME_PAST: frozenset(
        {
            Op.BETWEEN_DATES,
            Op.ON_DATE,
            Op.TODAY,
            Op.THIS_WEEK,
            Op.DAYS_AGO,
            Op.MORE_DAYS_AGO,
            Op.LESS_DAYS_AGO,
        }
    ),
    FilterType.BOOLEAN: frozenset({Op.EQ}),
}

#: Wire filter name → value strategy. Names absent from this table are accepted
#: with a permissive check (operator must exist in the vocabulary) so custom
#: fields (``customField12``) and instance-specific filters keep working.
FILTER_TYPES: dict[str, FilterType] = {
    # work packages
    "id": FilterType.LIST,
    "subject": FilterType.TEXT,
    "description": FilterType.TEXT,
    "search": FilterType.SEARCH,
    "typeahead": FilterType.SEARCH,
    "subjectOrId": FilterType.SEARCH,
    "status": FilterType.STATUS,
    "type": FilterType.LIST,
    "priority": FilterType.LIST,
    "author": FilterType.LIST,
    "assignee": FilterType.LIST_OPTIONAL,
    "assigneeOrGroup": FilterType.LIST_OPTIONAL,
    "responsible": FilterType.LIST_OPTIONAL,
    "watcher": FilterType.LIST,
    "project": FilterType.LIST,
    "subprojectId": FilterType.RELATION,
    "onlySubproject": FilterType.RELATION,
    "version": FilterType.LIST_OPTIONAL,
    "category": FilterType.LIST_OPTIONAL,
    "parent": FilterType.RELATION,
    "ancestor": FilterType.RELATION,
    "children": FilterType.RELATION,
    "dueDate": FilterType.DATE,
    "startDate": FilterType.DATE,
    "datesInterval": FilterType.DATE,
    "createdAt": FilterType.DATETIME_PAST,
    "updatedAt": FilterType.DATETIME_PAST,
    "percentageDone": FilterType.INTEGER,
    "estimatedTime": FilterType.FLOAT,
    "storyPoints": FilterType.INTEGER,
    # time entries
    "spentOn": FilterType.DATE,
    "user": FilterType.LIST,
    "workPackage": FilterType.LIST,
    "entityId": FilterType.LIST,
    "entityType": FilterType.LIST,
    "activity": FilterType.LIST,
    # projects
    "active": FilterType.BOOLEAN,
    "name_and_identifier": FilterType.SEARCH,
    "parent_id": FilterType.RELATION,
    # notifications
    "readIAN": FilterType.BOOLEAN,
    "reason": FilterType.LIST,
    # principals / memberships
    "principal": FilterType.LIST,
    "member": FilterType.LIST,
    "group": FilterType.LIST,
    "name": FilterType.TEXT,
}

#: Per-resource overrides for names whose strategy differs by endpoint.
RESOURCE_FILTER_TYPES: dict[str, dict[str, FilterType]] = {
    "principals": {"status": FilterType.LIST, "type": FilterType.LIST},
    "projects": {"id": FilterType.LIST, "ancestor": FilterType.LIST},
}

#: snake_case → wire name, for the cases mechanical camelCasing gets wrong.
#: The identity entries are the ones OpenProject really does spell snake_case:
#: ``anyNameAttribute`` or ``nameAndIdentifier`` would come back as a 400.
WIRE_NAME_OVERRIDES: dict[str, str] = {
    "read_ian": "readIAN",
    "any_name_attribute": "any_name_attribute",
    "name_and_identifier": "name_and_identifier",
    "parent_id": "parent_id",
    "subject_or_id": "subjectOrId",
}

#: Sort keys accepted by ``list_work_packages`` / ``search_work_packages``.
WORK_PACKAGE_SORT_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "subject",
        "type",
        "status",
        "priority",
        "author",
        "assignee",
        "responsible",
        "project",
        "version",
        "category",
        "parent",
        "start_date",
        "due_date",
        "created_at",
        "updated_at",
        "percentage_done",
        "estimated_time",
        "spent_time",
        "story_points",
    }
)

SORT_DIRECTIONS: frozenset[str] = frozenset({"asc", "desc"})


class Filter(BaseModel):
    """One entry of the wire filter array."""

    name: str = Field(description="Wire (camelCase) filter name, e.g. 'dueDate'.")
    operator: str = Field(description="Filter operator, e.g. '=', '!*', '<>d'.")
    values: list[str] = Field(default_factory=list[str], description="Filter values as strings.")

    def to_wire(self) -> dict[str, dict[str, Any]]:
        return {self.name: {"operator": self.operator, "values": list(self.values)}}


class RawFilter(BaseModel):
    """The typed escape hatch exposed to tools (SPEC §9.2).

    Deliberately a typed array rather than a JSON-in-a-string parameter so the
    model cannot produce escaping failures.
    """

    name: str = Field(description="Filter name as OpenProject spells it, e.g. 'customField12'.")
    operator: str = Field(description="Filter operator, e.g. '=', '~', '<>d'.")
    values: list[str] = Field(
        default_factory=list[str],
        description="Values for the operator; may be empty for '*' / '!*'.",
    )


def to_wire_name(name: str) -> str:
    """Map a snake_case tool parameter or sort key to its camelCase wire name.

    Already-camelCase names pass through unchanged, so callers can pass either.
    """
    if name in WIRE_NAME_OVERRIDES:
        return WIRE_NAME_OVERRIDES[name]
    if "_" not in name:
        return name
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def to_snake_name(name: str) -> str:
    """Inverse of :func:`to_wire_name` for wire keys we surface to the model.

    ``"estimatedTime"`` → ``"estimated_time"``; consecutive capitals are kept
    together (``"readIAN"`` → ``"read_ian"``).
    """
    for snake, wire in WIRE_NAME_OVERRIDES.items():
        if wire == name:
            return snake
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return spaced.lower()


def filter_type_for(name: str, resource: str | None = None) -> FilterType | None:
    """Look up the value strategy for a wire filter name."""
    if resource is not None:
        override = RESOURCE_FILTER_TYPES.get(resource, {}).get(name)
        if override is not None:
            return override
    return FILTER_TYPES.get(name)


def register_filter_type(name: str, filter_type: FilterType, resource: str | None = None) -> None:
    """Teach the validator about an additional filter name.

    Phase 2/3 tool modules use this for resource-specific filters rather than
    editing this table.
    """
    if resource is None:
        FILTER_TYPES[name] = filter_type
    else:
        RESOURCE_FILTER_TYPES.setdefault(resource, {})[name] = filter_type


def _format_operators(operators: Iterable[str]) -> str:
    return ", ".join(sorted(operators))


def validate_operator(name: str, operator: str, resource: str | None = None) -> None:
    """Raise :class:`InputValidationError` if the operator is wrong for this filter.

    Unknown filter names are checked against the global vocabulary only —
    custom fields and instance-specific filters must stay usable.
    """
    if operator not in ALL_OPERATORS:
        raise InputValidationError(
            f"Unknown filter operator {operator!r}.",
            hint=f"Allowed operators: {_format_operators(ALL_OPERATORS)}.",
        )
    filter_type = filter_type_for(name, resource)
    if filter_type is None:
        return
    allowed = ALLOWED_OPERATORS[filter_type]
    if operator not in allowed:
        raise InputValidationError(
            f"Filter {name!r} does not support operator {operator!r}.",
            hint=(
                f"{name!r} is a {filter_type.value} filter; allowed operators: "
                f"{_format_operators(allowed)}."
            ),
        )


def _as_values(values: Sequence[Any] | Any) -> list[Any]:
    """A filter's values as a list: a lone scalar (or string) becomes ``[value]``."""
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        return [values]
    return list(cast(Sequence[Any], values))


def normalize_value(value: Any) -> str:
    """Coerce a Python value to its wire string.

    Booleans become ``"t"`` / ``"f"``; ``None`` becomes ``""`` (the empty bound
    of an open-ended ``<>d`` range); dates and datetimes become ISO 8601.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def make_filter(
    name: str,
    operator: str | Op,
    values: Sequence[Any] | Any = (),
    *,
    resource: str | None = None,
) -> Filter:
    """Build one validated :class:`Filter`.

    ``name`` may be snake_case (mapped) or already camelCase. ``values`` accepts
    a scalar or a sequence.
    """
    wire_name = to_wire_name(name)
    op_value = operator.value if isinstance(operator, Op) else str(operator)
    validate_operator(wire_name, op_value, resource)
    return Filter(
        name=wire_name,
        operator=op_value,
        values=[normalize_value(item) for item in _as_values(values)],
    )


def filter_from_raw(raw: RawFilter | Mapping[str, Any], *, resource: str | None = None) -> Filter:
    """Validate a caller-supplied ``raw_filters`` entry."""
    data = raw.model_dump() if isinstance(raw, RawFilter) else dict(raw)
    name = data.get("name")
    operator = data.get("operator")
    if not isinstance(name, str) or not isinstance(operator, str):
        raise InputValidationError(
            "Each raw filter needs a string 'name' and 'operator'.",
            hint='Example: {"name": "customField12", "operator": "=", "values": ["4"]}',
        )
    return make_filter(name, operator, _as_values(data.get("values") or []), resource=resource)


def serialize_filters(filters: Iterable[Filter | Mapping[str, Any]] | None) -> str | None:
    """Serialize filters to the wire JSON array; ``None`` when there are none."""
    if filters is None:
        return None
    entries: list[dict[str, Any]] = []
    for item in filters:
        resolved = item if isinstance(item, Filter) else filter_from_raw(item)
        entries.append(resolved.to_wire())
    if not entries:
        return None
    return json.dumps(entries, separators=(",", ":"))


def serialize_sort_by(
    sort_by: Sequence[Sequence[str]] | None,
    *,
    allowed_keys: Iterable[str] | None = None,
) -> str | None:
    """Serialize ``[["due_date", "asc"]]`` to ``[["dueDate","asc"]]``.

    Keys are snake_case in tool parameters and mapped to camelCase here; an
    unknown key raises with the allowed set listed (SPEC §5.8).
    """
    if not sort_by:
        return None
    allowed = frozenset(allowed_keys) if allowed_keys is not None else None
    pairs: list[list[str]] = []
    for entry in sort_by:
        if isinstance(entry, str):
            key, direction = entry, "asc"
        elif len(entry) == 1:
            key, direction = entry[0], "asc"
        elif len(entry) == 2:
            key, direction = entry[0], entry[1]
        else:
            raise InputValidationError(
                f"Invalid sort entry {list(entry)!r}.",
                hint='Each entry is [key] or [key, "asc"|"desc"], e.g. ["due_date", "asc"].',
            )
        direction = direction.lower()
        if direction not in SORT_DIRECTIONS:
            raise InputValidationError(
                f"Invalid sort direction {direction!r} for sort key {key!r}.",
                hint="Direction must be 'asc' or 'desc'.",
            )
        if allowed is not None and key not in allowed:
            raise InputValidationError(
                f"Unknown sort key {key!r}.",
                hint=f"Allowed sort keys: {', '.join(sorted(allowed))}.",
            )
        pairs.append([to_wire_name(key), direction])
    return json.dumps(pairs, separators=(",", ":"))


def pagination_params(page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, int]:
    """Map ``page``/``page_size`` to OpenProject's ``offset``/``pageSize``.

    ``offset`` is a 1-based **page number** upstream, which is why this is a
    direct mapping and not a multiplication. Values are clamped to sane bounds;
    the server may clamp ``pageSize`` further and the envelope reports what
    actually came back.
    """
    safe_page = max(int(page or 1), 1)
    safe_size = min(max(int(page_size or DEFAULT_PAGE_SIZE), 1), MAX_PAGE_SIZE)
    return {"offset": safe_page, "pageSize": safe_size}


def query_params(
    *,
    filters: Iterable[Filter | Mapping[str, Any]] | None = None,
    page: int | None = None,
    page_size: int | None = None,
    sort_by: Sequence[Sequence[str]] | None = None,
    sort_keys: Iterable[str] | None = None,
    group_by: str | None = None,
    show_sums: bool | None = None,
    select: Sequence[str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a complete query-parameter dict for ``httpx``'s ``params=``.

    Only the parameters that were actually requested appear in the result, so
    the URL stays minimal and comparable in tests.
    """
    params: dict[str, Any] = {}
    serialized = serialize_filters(filters)
    if serialized is not None:
        params["filters"] = serialized
    if page is not None or page_size is not None:
        params.update(
            pagination_params(page or 1, page_size if page_size is not None else DEFAULT_PAGE_SIZE)
        )
    sort = serialize_sort_by(sort_by, allowed_keys=sort_keys)
    if sort is not None:
        params["sortBy"] = sort
    if group_by:
        params["groupBy"] = to_wire_name(group_by)
    if show_sums is not None:
        params["showSums"] = "true" if show_sums else "false"
    if select:
        params["select"] = ",".join(select)
    if extra:
        params.update({key: value for key, value in extra.items() if value is not None})
    return params


# --- common filter recipes ------------------------------------------------


def status_filter(scope: str = "open", status_ids: Sequence[int | str] | None = None) -> Filter:
    """Build the explicit status filter every WP tool sends (SPEC §6.2).

    ``status_ids`` overrides ``scope``. Scopes: ``open`` → ``o``,
    ``closed`` → ``c``, ``all`` → ``*``. The server's implicit "open only"
    default is therefore never relied upon.
    """
    if status_ids:
        return make_filter("status", Op.EQ, list(status_ids))
    normalized = (scope or "open").lower()
    if normalized == "open":
        return make_filter("status", Op.OPEN)
    if normalized == "closed":
        return make_filter("status", Op.CLOSED)
    if normalized == "all":
        return make_filter("status", Op.ANY)
    raise InputValidationError(
        f"Unknown status_scope {scope!r}.",
        hint="status_scope must be one of: open, closed, all.",
    )


def date_range_filter(
    name: str,
    *,
    after: dt.date | str | None = None,
    before: dt.date | str | None = None,
) -> Filter:
    """Build an open-ended ``<>d`` range: ``["", "2026-08-01"]`` and friends.

    At least one bound is required; an omitted bound is sent as an empty string
    rather than a sentinel date.
    """
    if after is None and before is None:
        raise InputValidationError(
            f"A date range on {name!r} needs at least one bound.",
            hint="Pass 'after', 'before', or both (ISO YYYY-MM-DD).",
        )
    return make_filter(name, Op.BETWEEN_DATES, [after, before])


def principal_filter(name: str, values: Sequence[Any] | Any) -> Filter:
    """Build a principal filter honoring ``"me"`` and ``"none"``.

    ``"none"`` becomes the ``!*`` (no value) operator — the correct way to ask
    for unassigned work packages. ``"me"`` is passed through: the WP and
    time-entry filters accept it (the capabilities API does not, SPEC §6.1).
    """
    normalized = [normalize_value(item) for item in _as_values(values)]
    if len(normalized) == 1 and normalized[0].lower() == "none":
        return make_filter(name, Op.NONE)
    return make_filter(name, Op.EQ, normalized)
