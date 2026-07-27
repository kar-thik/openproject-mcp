"""HAL parsing, including property tests for id-from-href and collection unwrap."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openproject_mcp.client.hal import (
    collection,
    duration_hours,
    formattable,
    id_from_href,
    ref,
    refs,
    self_id,
)
from tests.fixtures.hal_payloads import WORK_PACKAGE, WORK_PACKAGE_COLLECTION

# --- id_from_href ---------------------------------------------------------


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/api/v3/work_packages/1234", 1234),
        ("/api/v3/projects/my-project", "my-project"),
        ("https://openproject.test/api/v3/users/12", 12),
        ("/api/v3/work_packages/1234/", 1234),
        ("/api/v3/statuses/7?foo=bar", 7),
        ("/api/v3/work_packages/{id}", None),
        ("", None),
        (None, None),
        ("/api/v3/time_entries/activities/3", 3),
        ("/api/v3/projects/-0", "-0"),
        ("/api/v3/projects/2026-roadmap", "2026-roadmap"),
    ],
)
def test_id_from_href_cases(href: str | None, expected: int | str | None) -> None:
    assert id_from_href(href) == expected


@given(st.integers(min_value=0, max_value=10**9))
def test_id_from_href_round_trips_numeric_ids(value: int) -> None:
    assert id_from_href(f"/api/v3/work_packages/{value}") == value


@given(
    st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_"),
        min_size=1,
        max_size=30,
    )
)
def test_id_from_href_preserves_identifiers(identifier: str) -> None:
    parsed = id_from_href(f"/api/v3/projects/{identifier}")
    numeric = identifier.isascii() and identifier.isdigit()
    assert parsed == (int(identifier) if numeric else identifier)


# --- refs -----------------------------------------------------------------


def test_ref_extracts_id_and_name() -> None:
    resolved = ref(WORK_PACKAGE, "status")
    assert resolved is not None
    assert (resolved.id, resolved.name) == (7, "In progress")


def test_ref_returns_none_for_null_href() -> None:
    assert ref(WORK_PACKAGE, "responsible") is None


def test_ref_returns_none_for_missing_link() -> None:
    assert ref(WORK_PACKAGE, "nonexistent") is None
    assert ref(None, "status") is None
    assert ref({}, "status") is None


def test_ref_falls_back_to_embedded_resource() -> None:
    payload = {
        "_links": {"project": {"href": "/api/v3/projects/7"}},
        "_embedded": {
            "project": {"_links": {"self": {"href": "/api/v3/projects/7"}}, "name": "Demo"}
        },
    }
    resolved = ref(payload, "project")
    assert resolved is not None
    assert (resolved.id, resolved.name) == (7, "Demo")


def test_refs_extracts_link_arrays() -> None:
    payload = {
        "_links": {
            "children": [
                {"href": "/api/v3/work_packages/2", "title": "Child A"},
                {"href": "/api/v3/work_packages/3", "title": "Child B"},
            ]
        }
    }
    assert [(item.id, item.name) for item in refs(payload, "children")] == [
        (2, "Child A"),
        (3, "Child B"),
    ]


def test_self_id_prefers_self_link_then_id_field() -> None:
    assert self_id(WORK_PACKAGE) == 1234
    assert self_id({"id": 99}) == 99
    assert self_id({}) is None


# --- collections ----------------------------------------------------------


def test_collection_unwraps_golden_payload() -> None:
    unwrapped = collection(WORK_PACKAGE_COLLECTION)
    assert unwrapped.total == 137
    assert unwrapped.count == 2
    assert unwrapped.page_size == 20
    assert unwrapped.offset == 2
    assert len(unwrapped) == 2
    assert unwrapped.total_sums["storyPoints"] == 41
    assert unwrapped.groups[0]["value"] == "In progress"


def test_collection_handles_empty_and_missing_shapes() -> None:
    assert collection(None).elements == []
    assert collection({}).total == 0
    assert collection({"_embedded": {"elements": []}}).total == 0


def test_collection_accepts_bare_list_payload() -> None:
    unwrapped = collection([{"id": 1}, {"id": 2}])  # type: ignore[arg-type]
    assert unwrapped.total == 2
    assert len(unwrapped) == 2


@given(
    elements=st.lists(st.fixed_dictionaries({"id": st.integers()}), max_size=25),
    total=st.integers(min_value=0, max_value=10**6),
    page_size=st.integers(min_value=1, max_value=100),
    offset=st.integers(min_value=1, max_value=50),
)
def test_collection_unwrap_is_total_and_lossless(
    elements: list[dict[str, int]], total: int, page_size: int, offset: int
) -> None:
    payload = {
        "_type": "Collection",
        "total": total,
        "count": len(elements),
        "pageSize": page_size,
        "offset": offset,
        "_embedded": {"elements": elements},
    }
    unwrapped = collection(payload)
    assert unwrapped.elements == elements
    assert unwrapped.total == total
    assert unwrapped.count == len(elements)
    assert unwrapped.page_size == page_size
    assert unwrapped.offset == offset


@given(st.lists(st.fixed_dictionaries({"id": st.integers()}), max_size=10))
def test_collection_total_defaults_to_element_count(elements: list[dict[str, int]]) -> None:
    unwrapped = collection({"_embedded": {"elements": elements}})
    assert unwrapped.total == len(elements)


# --- formattable & durations ----------------------------------------------


def test_formattable_surfaces_raw_and_drops_html() -> None:
    assert formattable(WORK_PACKAGE["description"]) == "Pooled httpx client with retries."


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("plain text", "plain text"),
        ({"format": "markdown", "raw": None, "html": ""}, None),
        ({"format": "markdown", "raw": "# Title"}, "# Title"),
        (42, None),
    ],
)
def test_formattable_cases(value: object, expected: str | None) -> None:
    assert formattable(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT7H30M", 7.5),
        ("PT1H", 1.0),
        ("PT90M", 1.5),
        ("P1DT2H", 26.0),
        ("PT30S", pytest.approx(1 / 120)),
        (3.25, 3.25),
        (None, None),
        ("garbage", None),
    ],
)
def test_duration_hours(value: object, expected: float | None) -> None:
    assert duration_hours(value) == expected
