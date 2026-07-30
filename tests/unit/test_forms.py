"""The shared form pre-flight (SPEC §4.5).

``tools/_forms.py`` is what every write module runs before committing: read the
form's ``validationErrors``, turn them into one typed 422, and merge the form's
defaulted payload with ours. The domain wrappers that supply the subject and the
fallback hint are covered in each module's own tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from openproject_mcp.client.errors import ValidationFailedError
from openproject_mcp.tools._forms import (
    allowed_titles,
    allowed_value_hints,
    form_payload,
    form_schema,
    merge_form_payload,
    raise_validation_errors,
)

CLEAN_FORM: dict[str, Any] = {
    "_embedded": {"payload": {"subject": "from the form"}, "validationErrors": {}}
}

REJECTED_FORM: dict[str, Any] = {
    "_embedded": {
        "validationErrors": {
            "status": {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Status is not writable.",
                "_embedded": {"details": {"attribute": "status"}},
            }
        },
        "schema": {
            "status": {
                "type": "Status",
                "_links": {
                    "allowedValues": [
                        {"href": "/api/v3/statuses/1", "title": "New"},
                        {"href": "/api/v3/statuses/7", "title": "In progress"},
                    ]
                },
            }
        },
    }
}


# --- sections -------------------------------------------------------------


def test_form_sections_are_none_when_the_server_omits_them() -> None:
    assert form_payload({}) is None
    assert form_schema({}) is None
    assert form_payload({"_embedded": {"payload": "not an object"}}) is None


def test_form_payload_unwraps_the_embedded_object() -> None:
    assert form_payload(CLEAN_FORM) == {"subject": "from the form"}


# --- allowed values -------------------------------------------------------


def test_allowed_titles_read_link_objects_and_embedded_resources() -> None:
    assert allowed_titles({"_links": {"allowedValues": [{"href": "/s/1", "title": "New"}]}}) == [
        "New"
    ]
    assert allowed_titles({"_embedded": {"allowedValues": [{"name": "Bug"}]}}) == ["Bug"]
    assert allowed_titles({"_links": {"allowedValues": {"href": "/statuses"}}}) == [], (
        "a lone lookup URL lists nothing"
    )
    assert allowed_titles(None) == []


def test_allowed_value_hints_name_one_attribute_each() -> None:
    errors = REJECTED_FORM["_embedded"]["validationErrors"]
    assert allowed_value_hints(REJECTED_FORM, errors) == [
        "Allowed values for status: New, In progress."
    ]


# --- raising --------------------------------------------------------------


def test_a_clean_form_raises_nothing() -> None:
    raise_validation_errors(CLEAN_FORM, subject="version", fallback_hint="unused")


def test_errors_become_one_typed_422_with_violations() -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        raise_validation_errors(
            REJECTED_FORM,
            subject="work package",
            hints=allowed_value_hints,
            fallback_hint="unused when the schema lists values",
        )

    error = excinfo.value
    assert error.http_status == 422
    assert error.violations == [{"attribute": "status", "message": "Status is not writable."}]
    assert error.hint == "Allowed values for status: New, In progress."
    assert error.error_identifier == (
        "urn:openproject-org:api:v3:errors:PropertyConstraintViolation"
    )


def test_the_fallback_hint_covers_a_form_that_lists_nothing() -> None:
    form = {"_embedded": {"validationErrors": {"name": {"_type": "Error", "message": "Taken."}}}}
    with pytest.raises(ValidationFailedError) as excinfo:
        raise_validation_errors(
            form, subject="version", hints=allowed_value_hints, fallback_hint="list_versions shows."
        )
    assert excinfo.value.hint == "list_versions shows."


def test_a_form_without_a_usable_message_still_names_the_subject() -> None:
    form = {"_embedded": {"validationErrors": {"name": {"_type": "Error"}}}}
    with pytest.raises(ValidationFailedError) as excinfo:
        raise_validation_errors(form, subject="version", fallback_hint="fix it")
    assert excinfo.value.message == "OpenProject rejected the version."


# --- merging --------------------------------------------------------------


def test_merge_keeps_form_defaults_and_our_links() -> None:
    base = {
        "_type": "WorkPackage",
        "subject": "from the form",
        "scheduleManually": False,
        "_links": {"self": {"href": "/api/v3/work_packages/1"}, "status": {"href": "/s/1"}},
    }
    override = {
        "subject": "ours",
        "_links": {"status": {"href": "/s/7"}, "attachments": [{"href": "/a/91"}]},
    }

    merged = merge_form_payload(base, override)

    assert merged["subject"] == "ours"
    assert merged["scheduleManually"] is False
    assert "_type" not in merged
    assert "self" not in merged["_links"], "a create payload never echoes a self link"
    assert merged["_links"]["status"] == {"href": "/s/7"}
    assert merged["_links"]["attachments"] == [{"href": "/a/91"}]


def test_merging_an_absent_form_payload_leaves_ours_untouched() -> None:
    payload = {"subject": "ours", "_links": {"project": {"href": "/api/v3/projects/5"}}}
    assert merge_form_payload({}, payload) == payload
