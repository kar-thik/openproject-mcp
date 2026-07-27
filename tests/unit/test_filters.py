"""Filter grammar: operator matrix, golden wire strings, pagination mapping."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from openproject_mcp.client.errors import InputValidationError
from openproject_mcp.client.filters import (
    ALLOWED_OPERATORS,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    FilterType,
    Op,
    date_range_filter,
    filter_from_raw,
    make_filter,
    normalize_value,
    pagination_params,
    principal_filter,
    query_params,
    serialize_filters,
    serialize_sort_by,
    status_filter,
    to_snake_name,
    to_wire_name,
    validate_operator,
)

# --- operator x type matrix ------------------------------------------------

REPRESENTATIVE_FILTERS: dict[FilterType, str] = {
    FilterType.LIST: "type",
    FilterType.LIST_OPTIONAL: "assignee",
    FilterType.STATUS: "status",
    FilterType.RELATION: "parent",
    FilterType.TEXT: "subject",
    FilterType.SEARCH: "typeahead",
    FilterType.INTEGER: "percentageDone",
    FilterType.DATE: "dueDate",
    FilterType.DATETIME_PAST: "updatedAt",
    FilterType.BOOLEAN: "active",
}


@pytest.mark.parametrize(("filter_type", "name"), list(REPRESENTATIVE_FILTERS.items()))
def test_allowed_operators_are_accepted(filter_type: FilterType, name: str) -> None:
    for operator in ALLOWED_OPERATORS[filter_type]:
        validate_operator(name, operator)


@pytest.mark.parametrize(("filter_type", "name"), list(REPRESENTATIVE_FILTERS.items()))
def test_disallowed_operators_fail_locally_with_the_allowed_set(
    filter_type: FilterType, name: str
) -> None:
    forbidden = {op.value for op in Op} - ALLOWED_OPERATORS[filter_type]
    for operator in sorted(forbidden):
        with pytest.raises(InputValidationError) as excinfo:
            validate_operator(name, operator)
        hint = excinfo.value.hint or ""
        for allowed in ALLOWED_OPERATORS[filter_type]:
            assert allowed in hint


def test_unknown_operator_is_rejected_even_for_unknown_filters() -> None:
    with pytest.raises(InputValidationError) as excinfo:
        validate_operator("customField12", "LIKE")
    assert "Allowed operators" in (excinfo.value.hint or "")


def test_unknown_filter_name_allows_any_valid_operator() -> None:
    validate_operator("customField12", "=")
    validate_operator("customField12", "~")


def test_resource_override_changes_the_matrix() -> None:
    validate_operator("status", "o")  # work packages
    with pytest.raises(InputValidationError):
        validate_operator("status", "o", resource="principals")


# --- serialization golden cases -------------------------------------------


def test_serialize_filters_golden_wire_format() -> None:
    filters = [
        status_filter("open"),
        make_filter("assignee", Op.EQ, ["me"]),
        date_range_filter("due_date", before="2026-08-01"),
    ]
    assert serialize_filters(filters) == json.dumps(
        [
            {"status": {"operator": "o", "values": []}},
            {"assignee": {"operator": "=", "values": ["me"]}},
            {"dueDate": {"operator": "<>d", "values": ["", "2026-08-01"]}},
        ],
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("scope", "operator"),
    [("open", "o"), ("closed", "c"), ("all", "*")],
)
def test_status_scope_maps_to_operator(scope: str, operator: str) -> None:
    assert status_filter(scope).operator == operator
    assert status_filter(scope).values == []


def test_status_ids_override_scope() -> None:
    filter_ = status_filter("open", status_ids=[7, 12])
    assert filter_.operator == "="
    assert filter_.values == ["7", "12"]


def test_unknown_status_scope_is_rejected() -> None:
    with pytest.raises(InputValidationError):
        status_filter("halfway")


@pytest.mark.parametrize(
    ("after", "before", "expected"),
    [
        ("2026-01-01", None, ["2026-01-01", ""]),
        (None, "2026-12-31", ["", "2026-12-31"]),
        ("2026-01-01", "2026-12-31", ["2026-01-01", "2026-12-31"]),
    ],
)
def test_open_ended_date_ranges_use_empty_bounds(
    after: str | None, before: str | None, expected: list[str]
) -> None:
    filter_ = date_range_filter("spent_on", after=after, before=before)
    assert filter_.operator == "<>d"
    assert filter_.values == expected
    assert filter_.name == "spentOn"


def test_date_range_requires_a_bound() -> None:
    with pytest.raises(InputValidationError):
        date_range_filter("dueDate")


def test_principal_filter_handles_me_and_none() -> None:
    assert principal_filter("assignee", "me").to_wire() == {
        "assignee": {"operator": "=", "values": ["me"]}
    }
    assert principal_filter("assignee", "none").to_wire() == {
        "assignee": {"operator": "!*", "values": []}
    }
    assert principal_filter("assignee", [3, 4]).values == ["3", "4"]


def test_top_level_only_is_parent_none() -> None:
    assert make_filter("parent", Op.NONE).to_wire() == {"parent": {"operator": "!*", "values": []}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "t"),
        (False, "f"),
        (None, ""),
        (12, "12"),
        ("me", "me"),
        (dt.date(2026, 7, 26), "2026-07-26"),
        (dt.datetime(2026, 7, 26, 12, 30), "2026-07-26T12:30:00"),
    ],
)
def test_normalize_value(value: object, expected: str) -> None:
    assert normalize_value(value) == expected


def test_serialize_filters_returns_none_when_empty() -> None:
    assert serialize_filters(None) is None
    assert serialize_filters([]) is None


def test_raw_filters_are_validated() -> None:
    parsed = filter_from_raw({"name": "customField12", "operator": "=", "values": ["4"]})
    assert parsed.to_wire() == {"customField12": {"operator": "=", "values": ["4"]}}
    with pytest.raises(InputValidationError):
        filter_from_raw({"name": "dueDate", "operator": "="})
    with pytest.raises(InputValidationError):
        filter_from_raw({"operator": "="})


# --- names ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("snake", "wire"),
    [
        ("due_date", "dueDate"),
        ("start_date", "startDate"),
        ("updated_at", "updatedAt"),
        ("created_at", "createdAt"),
        ("subproject_id", "subprojectId"),
        ("percentage_done", "percentageDone"),
        ("spent_on", "spentOn"),
        ("work_package", "workPackage"),
        ("entity_id", "entityId"),
        ("read_ian", "readIAN"),
        ("custom_field_12", "customField12"),
        ("name_and_identifier", "name_and_identifier"),
        ("status", "status"),
        ("dueDate", "dueDate"),
    ],
)
def test_to_wire_name(snake: str, wire: str) -> None:
    assert to_wire_name(snake) == wire


@pytest.mark.parametrize(
    ("wire", "snake"),
    [
        ("dueDate", "due_date"),
        ("estimatedTime", "estimated_time"),
        ("readIAN", "read_ian"),
        ("status", "status"),
    ],
)
def test_to_snake_name(wire: str, snake: str) -> None:
    assert to_snake_name(wire) == snake


# --- sorting --------------------------------------------------------------


def test_sort_by_is_mapped_to_camel_case() -> None:
    assert serialize_sort_by([["due_date", "asc"], ["updated_at", "desc"]]) == json.dumps(
        [["dueDate", "asc"], ["updatedAt", "desc"]], separators=(",", ":")
    )


def test_sort_by_defaults_direction_to_asc() -> None:
    assert serialize_sort_by([["due_date"]]) == '[["dueDate","asc"]]'


def test_unknown_sort_key_lists_allowed_keys() -> None:
    with pytest.raises(InputValidationError) as excinfo:
        serialize_sort_by([["deadline", "asc"]], allowed_keys={"due_date", "id"})
    assert "due_date" in (excinfo.value.hint or "")


def test_invalid_sort_direction_is_rejected() -> None:
    with pytest.raises(InputValidationError):
        serialize_sort_by([["due_date", "sideways"]])


# --- pagination -----------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "page_size", "expected"),
    [
        (1, 20, {"offset": 1, "pageSize": 20}),
        (2, 20, {"offset": 2, "pageSize": 20}),
        (7, 100, {"offset": 7, "pageSize": 100}),
        (0, 20, {"offset": 1, "pageSize": 20}),
        (-3, 0, {"offset": 1, "pageSize": DEFAULT_PAGE_SIZE}),
        (1, 500, {"offset": 1, "pageSize": MAX_PAGE_SIZE}),
    ],
)
def test_page_maps_to_one_based_offset(page: int, page_size: int, expected: dict) -> None:
    assert pagination_params(page, page_size) == expected


def test_query_params_assembles_the_full_query() -> None:
    params = query_params(
        filters=[status_filter("all"), make_filter("project", Op.EQ, ["demo"])],
        page=3,
        page_size=25,
        sort_by=[["due_date", "asc"]],
        group_by="status",
        show_sums=True,
    )
    assert params == {
        "filters": '[{"status":{"operator":"*","values":[]}},'
        '{"project":{"operator":"=","values":["demo"]}}]',
        "offset": 3,
        "pageSize": 25,
        "sortBy": '[["dueDate","asc"]]',
        "groupBy": "status",
        "showSums": "true",
    }


def test_query_params_omits_absent_pieces() -> None:
    assert query_params() == {}
    assert query_params(extra={"select": None}) == {}
