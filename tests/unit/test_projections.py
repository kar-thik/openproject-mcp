"""The shared output projections (SPEC §5.2, §6.2.1, §6.3).

These are the shapes several tool modules build from the same HAL payloads, so
they live in ``projections.py`` and are constructed through one classmethod
each. hrefs must never survive the trip.
"""

from __future__ import annotations

import pytest

from openproject_mcp.projections import RelationRow, WorkPackageRow, custom_field_type_name
from tests.fixtures.work_packages_payloads import WORK_PACKAGE_DETAIL

RELATION = {
    "_type": "Relation",
    "type": "follows",
    "reverseType": "precedes",
    "lag": 3,
    "description": {"format": "plain", "raw": "Wait for sign-off.", "html": "<p>…</p>"},
    "_links": {
        "self": {"href": "/api/v3/relations/650"},
        "from": {"href": "/api/v3/work_packages/1234", "title": "Ship the client"},
        "to": {"href": "/api/v3/work_packages/1300", "title": "Ship the tools"},
    },
}


def test_work_package_row_drops_hrefs_and_keeps_refs() -> None:
    row = WorkPackageRow.from_hal(WORK_PACKAGE_DETAIL)
    assert row.id == 1234
    assert row.status is not None and row.status.model_dump() == {"id": 7, "name": "In progress"}
    assert row.project is not None and row.project.id == 5
    assert row.percentage_done == 40
    assert "href" not in row.model_dump_json()


def test_work_package_row_survives_an_empty_payload() -> None:
    assert WorkPackageRow.from_hal({}).model_dump() == WorkPackageRow().model_dump()


def test_relation_row_spells_out_both_ends() -> None:
    row = RelationRow.from_hal(RELATION)
    assert row.model_dump() == {
        "id": 650,
        "type": "follows",
        "reverse_type": "precedes",
        "from_work_package": {"id": 1234, "name": "Ship the client"},
        "to_work_package": {"id": 1300, "name": "Ship the tools"},
        "lag": 3,
        "description": "Wait for sign-off.",
    }


def test_relation_row_drops_a_non_integer_lag() -> None:
    assert RelationRow.from_hal({"lag": True}).lag is None
    assert RelationRow.from_hal({"lag": "3"}).lag is None


@pytest.mark.parametrize(
    ("schema_type", "expected"),
    [
        ("CustomOption", "list"),
        ("[]CustomOption", "list"),
        ("[]User", "user"),
        ("String", "string"),
        ("Formattable", "text"),
        ("SomethingNew", "something_new"),
        ("", None),
        (None, None),
        (12, None),
    ],
)
def test_custom_field_type_names(schema_type: object, expected: str | None) -> None:
    assert custom_field_type_name(schema_type) == expected
