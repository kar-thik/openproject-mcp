"""Payload builders: link hrefs, clearing with {"href": null}, custom fields."""

from __future__ import annotations

import pytest

from openproject_mcp.client.errors import InputValidationError
from openproject_mcp.client.payloads import (
    build_write_payload,
    custom_field_payload,
    formattable_field,
    href_for,
    link,
    links_payload,
    resolve_custom_field_key,
)
from tests.fixtures.hal_payloads import WORK_PACKAGE_SCHEMA


def test_href_and_link_builders() -> None:
    assert href_for("users", 12) == "/api/v3/users/12"
    assert link("users", 12) == {"href": "/api/v3/users/12"}


def test_clearing_a_link_uses_null_never_a_none_path() -> None:
    import json

    cleared = link("users", None)
    assert cleared == {"href": None}
    assert json.dumps(cleared) == '{"href": null}'
    assert "/users/None" not in json.dumps(cleared)


def test_links_payload_builds_known_fields() -> None:
    payload = links_payload(assignee=12, status="7", parent=None)
    assert payload == {
        "assignee": {"href": "/api/v3/users/12"},
        "status": {"href": "/api/v3/statuses/7"},
        "parent": {"href": None},
    }


def test_links_payload_rejects_unknown_fields() -> None:
    with pytest.raises(InputValidationError) as excinfo:
        links_payload(assinee=12)
    assert "assignee" in (excinfo.value.hint or "")


def test_formattable_field_wraps_markdown() -> None:
    assert formattable_field("# Title") == {"format": "markdown", "raw": "# Title"}
    assert formattable_field(None) is None


def test_build_write_payload_merges_everything() -> None:
    payload = build_write_payload(
        {"subject": "New"},
        links_payload(status=7),
        lock_version=3,
    )
    assert payload == {
        "subject": "New",
        "lockVersion": 3,
        "_links": {"status": {"href": "/api/v3/statuses/7"}},
    }


# --- custom fields --------------------------------------------------------


def test_resolve_custom_field_key_accepts_key_or_display_name() -> None:
    assert resolve_custom_field_key("customField12", WORK_PACKAGE_SCHEMA) == "customField12"
    assert resolve_custom_field_key("Severity", WORK_PACKAGE_SCHEMA) == "customField12"
    assert resolve_custom_field_key("severity", WORK_PACKAGE_SCHEMA) == "customField12"


def test_unknown_custom_field_lists_valid_keys() -> None:
    with pytest.raises(InputValidationError) as excinfo:
        resolve_custom_field_key("Urgency", WORK_PACKAGE_SCHEMA)
    hint = excinfo.value.hint or ""
    assert "customField12 (Severity)" in hint
    assert "get_work_package_schema" in hint


def test_ambiguous_custom_field_name_is_rejected() -> None:
    schema = {
        "customField1": {"type": "String", "name": "Owner", "writable": True},
        "customField2": {"type": "String", "name": "owner", "writable": True},
    }
    with pytest.raises(InputValidationError) as excinfo:
        resolve_custom_field_key("Owner", schema)
    assert "ambiguous" in str(excinfo.value).lower()


def test_scalar_custom_field_is_written_as_an_attribute() -> None:
    attributes, links = custom_field_payload({"Ticket URL": "https://x/1"}, WORK_PACKAGE_SCHEMA)
    assert attributes == {"customField7": "https://x/1"}
    assert links == {}


def test_list_custom_field_resolves_option_name_to_href() -> None:
    attributes, links = custom_field_payload({"Severity": "High"}, WORK_PACKAGE_SCHEMA)
    assert attributes == {}
    assert links == {"customField12": {"href": "/api/v3/custom_options/4"}}


def test_list_custom_field_accepts_a_numeric_id() -> None:
    _, links = custom_field_payload({"customField12": 5}, WORK_PACKAGE_SCHEMA)
    assert links == {"customField12": {"href": "/api/v3/custom_options/5"}}


def test_multi_value_custom_field_produces_a_list_of_links() -> None:
    _, links = custom_field_payload({"Reviewers": ["Grace Hopper", 13]}, WORK_PACKAGE_SCHEMA)
    assert links == {
        "customField9": [
            {"href": "/api/v3/users/12"},
            {"href": "/api/v3/users/13"},
        ]
    }


def test_clearing_a_link_custom_field() -> None:
    _, links = custom_field_payload({"Severity": None}, WORK_PACKAGE_SCHEMA)
    assert links == {"customField12": {"href": None}}


def test_unknown_option_lists_allowed_values() -> None:
    with pytest.raises(InputValidationError) as excinfo:
        custom_field_payload({"Severity": "Catastrophic"}, WORK_PACKAGE_SCHEMA)
    hint = excinfo.value.hint or ""
    assert "High" in hint and "Low" in hint


def test_non_writable_custom_field_is_rejected() -> None:
    with pytest.raises(InputValidationError) as excinfo:
        custom_field_payload({"Computed": "x"}, WORK_PACKAGE_SCHEMA)
    assert "not writable" in str(excinfo.value)
