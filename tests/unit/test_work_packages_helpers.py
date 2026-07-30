"""The pure helpers behind the work-package tools.

Everything here is offline and synchronous: projections, the custom-field read
shape (SPEC §6.2.1), the update sentinel, include capping (G1) and the form
error translation (SPEC §4.5). The tools themselves are covered end-to-end in
``tests/protocol/test_work_packages.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from openproject_mcp.client.errors import InputValidationError, ValidationFailedError
from openproject_mcp.projections import TruncatedList
from openproject_mcp.tools.work_packages import (
    KEEP,
    WatcherRow,
    _api_path,
    _availability,
    _custom_fields,
    _duration_from_hours,
    _is_clear,
    _is_keep,
    _numeric_id,
    _raise_form_validation_errors,
    _truncated,
)
from tests.fixtures.work_packages_payloads import (
    CREATE_FORM_INVALID_STATUS,
    CREATE_FORM_OK,
    WORK_PACKAGE_DETAIL,
    WORK_PACKAGE_SCHEMA_5_1,
)

# --- conversions ----------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(7.5, "PT7H30M"), (8, "PT8H"), (0.25, "PT0H15M"), (0, "PT0H"), (1.75, "PT1H45M")],
)
def test_duration_from_hours(hours: float, expected: str) -> None:
    assert _duration_from_hours(hours) == expected


def test_keep_sentinel_distinguishes_omitted_from_cleared() -> None:
    assert _is_keep(KEEP)
    assert not _is_keep(None)
    assert not _is_keep("12")
    assert _is_clear(None)
    assert _is_clear("none")
    assert _is_clear("NULL")
    assert _is_clear("")
    assert not _is_clear("12")


def test_numeric_id_points_at_the_id_producing_tool() -> None:
    assert _numeric_id(12, field="assignee", produced_by="search_principals") == 12
    assert _numeric_id(" 13 ", field="assignee", produced_by="search_principals") == 13

    with pytest.raises(InputValidationError) as excinfo:
        _numeric_id("me", field="assignee", produced_by="search_principals")
    assert "get_instance_info" in (excinfo.value.hint or "")

    with pytest.raises(InputValidationError) as excinfo:
        _numeric_id("Grace Hopper", field="assignee", produced_by="search_principals")
    assert "search_principals" in (excinfo.value.hint or "")


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/api/v3/work_packages/schemas/5-1", "work_packages/schemas/5-1"),
        ("https://op.test/api/v3/statuses", "statuses"),
        ("/api/v3/work_packages?offset=2", "work_packages"),
        ("", None),
        (None, None),
    ],
)
def test_api_path_strips_the_api_root(href: Any, expected: str | None) -> None:
    assert _api_path(href) == expected


# --- projections ----------------------------------------------------------


def test_availability_reads_the_optional_module_links() -> None:
    assert _availability(WORK_PACKAGE_DETAIL) == {
        "dev_links": True,
        "meetings": False,
        "files": True,
    }
    assert _availability({"_links": {}}) == {
        "dev_links": False,
        "meetings": False,
        "files": False,
    }


def test_custom_fields_use_the_canonical_read_shape() -> None:
    values = [
        value.model_dump() for value in _custom_fields(WORK_PACKAGE_DETAIL, WORK_PACKAGE_SCHEMA_5_1)
    ]
    assert values == [
        {
            "key": "customField7",
            "name": "Ticket URL",
            "type": "string",
            "value": "https://tickets.example.com/OP-1",
            "value_ids": None,
        },
        {
            "key": "customField9",
            "name": "Reviewers",
            "type": "user",
            "value": ["Grace Hopper", "Alan Turing"],
            "value_ids": [12, 13],
        },
        {
            "key": "customField12",
            "name": "Severity",
            "type": "list",
            "value": "High",
            "value_ids": [4],
        },
    ]


def test_custom_fields_survive_a_missing_schema() -> None:
    values = _custom_fields(WORK_PACKAGE_DETAIL, None)
    assert [value.key for value in values] == ["customField7", "customField9", "customField12"]
    assert all(value.name is None and value.type is None for value in values)
    assert values[2].value == "High", "values come from the payload, names from the schema"


def test_custom_fields_skip_unset_values() -> None:
    payload = {
        "customField3": None,
        "customField4": "set",
        "_links": {"customField5": {"href": None}},
    }
    assert [value.key for value in _custom_fields(payload, None)] == ["customField4"]


def test_plural_custom_fields_settings_link_is_not_a_field() -> None:
    # Live 16.6 payload shape: a ``customFields`` (plural) HTML settings link
    # sits in _links next to real customField<N> keys and must be ignored.
    payload = {
        "customField1": "real value",
        "_links": {
            "customFields": {
                "href": "/projects/demo/settings/custom_fields",
                "type": "text/html",
                "title": "Custom fields",
            },
        },
    }
    assert [value.key for value in _custom_fields(payload, None)] == ["customField1"]


def test_formattable_custom_fields_surface_raw_text() -> None:
    payload = {"customField2": {"format": "markdown", "raw": "# Notes", "html": "<h1>Notes</h1>"}}
    values = _custom_fields(payload, {"customField2": {"type": "Formattable", "name": "Notes"}})
    assert values[0].value == "# Notes"
    assert values[0].type == "text"


# --- include capping (G1) -------------------------------------------------


def test_truncated_caps_at_twenty_and_reports_the_pointer() -> None:
    rows = [WatcherRow(id=index, name=f"user {index}") for index in range(25)]
    capped = _truncated(TruncatedList[WatcherRow], rows, 42, more_via="list_users()")
    assert len(capped.items) == 20
    assert capped.truncated is True
    assert capped.total == 42
    assert capped.more_via == "list_users()"


def test_truncated_stays_quiet_when_nothing_was_dropped() -> None:
    rows = [WatcherRow(id=1, name="Grace Hopper")]
    capped = _truncated(TruncatedList[WatcherRow], rows, 1, more_via="list_users()")
    assert capped.truncated is False
    assert capped.more_via is None, "no pointer when there is nothing more to fetch"


def test_truncated_never_reports_a_total_below_what_it_holds() -> None:
    rows = [WatcherRow(id=1, name="a"), WatcherRow(id=2, name="b")]
    assert _truncated(TruncatedList[WatcherRow], rows, 0, more_via=None).total == 2


# --- form flow (SPEC §4.5) ------------------------------------------------


def test_a_clean_form_raises_nothing() -> None:
    _raise_form_validation_errors(CREATE_FORM_OK)


def test_form_errors_become_violations_with_the_allowed_values() -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        _raise_form_validation_errors(CREATE_FORM_INVALID_STATUS)

    error = excinfo.value
    assert error.http_status == 422
    assert error.violations == [
        {"attribute": "status", "message": "Status is not set to one of the allowed values."}
    ]
    assert error.hint == "Allowed values for status: New, In progress."
    assert error.error_identifier == (
        "urn:openproject-org:api:v3:errors:PropertyConstraintViolation"
    )


def test_form_errors_without_allowed_values_still_point_at_the_schema_tool() -> None:
    form = {
        "_embedded": {
            "validationErrors": {
                "subject": {"_type": "Error", "message": "Subject can't be blank."}
            }
        }
    }
    with pytest.raises(ValidationFailedError) as excinfo:
        _raise_form_validation_errors(form)
    assert "get_work_package_schema" in (excinfo.value.hint or "")
