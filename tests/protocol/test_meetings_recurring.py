"""Protocol tests for the recurring-meeting tools (SPEC §6.13, §4.7, G2, G5).

Every call goes through the in-memory FastMCP client against a respx-mocked
instance. Three themes run through the file: the wire shapes (a plain number of
hours for ``duration``, the exact UTC ``Z`` occurrence token in the path, the
corrective ``timeZone`` PATCH after a create), the local validation of the
frequency/end_after matrix — rejected BEFORE the wire, with the allowed
combinations spelled out — and the version fencing: the whole family is 17.4+,
so a 404 anywhere must surface that third reading rather than "does not exist".
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.meetings_recurring_payloads import (
    CANCEL_CONFLICT,
    CREATED_SERIES,
    CREATED_SERIES_ID,
    CREATED_SERIES_UTC,
    CREATED_SERIES_ZONE_FIXED,
    CREATED_TEMPLATE_MEETING_ID,
    MODULE_FORBIDDEN,
    MODULE_NOT_FOUND,
    OCCURRENCE_MEETING,
    OCCURRENCE_MEETING_ID,
    PROJECT_ID,
    RECURRING_COLLECTION,
    RECURRING_MEETING,
    RECURRING_MEETING_ID,
    SERIES_START_REJECTED,
    TEMPLATE_MEETING_ID,
    UPCOMING_OCCURRENCES,
    hal_collection,
)

SERIES_PATH = f"recurring_meetings/{RECURRING_MEETING_ID}"
UPCOMING_PATH = f"{SERIES_PATH}/occurrences/upcoming"
CREATED_UPCOMING_PATH = f"recurring_meetings/{CREATED_SERIES_ID}/occurrences/upcoming"

#: The canonical UTC token for the instantiated occurrence — what init/cancel
#: must put in the path, colons unencoded, never a '+' offset.
OCCURRENCE_TOKEN = "2026-08-17T07:00:00Z"
OCCURRENCE_PATH = f"{SERIES_PATH}/occurrences/{OCCURRENCE_TOKEN}"

TOOL_NAMES = (
    "list_recurring_meetings",
    "get_recurring_meeting",
    "create_recurring_meeting",
    "delete_recurring_meeting",
    "init_recurring_meeting_occurrence",
    "cancel_recurring_meeting_occurrence",
)

#: A valid create call, mutated per test.
CREATE_ARGS: dict[str, Any] = {
    "project_id": PROJECT_ID,
    "title": "Design review",
    "start_time": "2026-09-01T09:00:00+02:00",
    "duration_minutes": 45,
    "time_zone": "Europe/Berlin",
    "frequency": "weekly",
    "interval": 2,
    "end_after": "specific_date",
    "end_date": "2026-12-31",
    "location": "Room 2.14",
}


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def body_of(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


# --- registration ---------------------------------------------------------


async def test_all_six_tools_are_registered_with_honest_annotations(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    for name in TOOL_NAMES:
        assert name in tools, name
        assert tools[name].outputSchema is not None
        assert tools[name].annotations is not None

    listing = tools["list_recurring_meetings"]
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert set(listing.inputSchema["properties"]) == {"page", "page_size"}
    assert set((listing.meta or {})["fastmcp"]["tags"]) == {"meetings", "read"}

    reading = tools["get_recurring_meeting"]
    assert reading.annotations is not None
    assert reading.annotations.readOnlyHint is True
    assert set(reading.inputSchema["properties"]) == {"recurring_meeting_id"}
    assert set((reading.meta or {})["fastmcp"]["tags"]) == {"meetings", "read"}

    creating = tools["create_recurring_meeting"]
    assert creating.annotations is not None
    assert creating.annotations.readOnlyHint is False
    assert creating.annotations.destructiveHint is False
    assert set(creating.inputSchema["properties"]) == {
        "project_id",
        "title",
        "start_time",
        "duration_minutes",
        "time_zone",
        "frequency",
        "interval",
        "monthly_day",
        "monthly_ordinal",
        "monthly_weekday",
        "end_after",
        "end_date",
        "iterations",
        "location",
        "notify",
    }
    assert set((creating.meta or {})["fastmcp"]["tags"]) == {"meetings", "write"}

    initing = tools["init_recurring_meeting_occurrence"]
    assert initing.annotations is not None
    assert initing.annotations.destructiveHint is False
    assert set(initing.inputSchema["properties"]) == {"recurring_meeting_id", "start_time"}
    assert set((initing.meta or {})["fastmcp"]["tags"]) == {"meetings", "write"}

    # The two destructive tools carry the full contract: annotations, tags, and
    # (asserted separately below) the confirm=false refusal.
    for name, properties in (
        ("delete_recurring_meeting", {"recurring_meeting_id", "confirm"}),
        (
            "cancel_recurring_meeting_occurrence",
            {"recurring_meeting_id", "start_time", "confirm"},
        ),
    ):
        destroying = tools[name]
        assert destroying.annotations is not None, name
        assert destroying.annotations.destructiveHint is True, name
        assert set(destroying.inputSchema["properties"]) == properties, name
        assert set((destroying.meta or {})["fastmcp"]["tags"]) == {
            "meetings",
            "write",
            "destructive",
        }, name


async def test_descriptions_state_the_traps_a_model_must_pass_on(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_recurring_meetings"].description or ""
    assert "instance-wide" in listing
    assert "17.4" in listing
    assert "notes" in listing

    # The id-consuming tools name their id-producing path (SPEC §5.10).
    getting = tools["get_recurring_meeting"].description or ""
    assert "list_recurring_meetings" in getting
    assert "'planned'" in getting

    creating = tools["create_recurring_meeting"].description or ""
    assert "DRAFT" in creating
    assert "state='open'" in creating

    initing = tools["init_recurring_meeting_occurrence"].description or ""
    assert "off-schedule" in initing
    assert "get_recurring_meeting" in initing

    cancelling = tools["cancel_recurring_meeting_occurrence"].description or ""
    assert "phantom" in cancelling
    assert "delete_meeting" in cancelling


# --- list_recurring_meetings ----------------------------------------------


async def test_list_recurring_meetings_pages_and_projects_the_schedule_fields(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("recurring_meetings").mock(
        return_value=httpx.Response(200, json=RECURRING_COLLECTION)
    )

    result = await mcp_client.call_tool("list_recurring_meetings", {})

    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["offset"] == "1"
    assert params["pageSize"] == "20"

    assert result.structured_content is not None
    content = result.structured_content
    assert content["pagination"] == {"total": 5, "page": 1, "page_size": 20, "has_more": True}
    assert content["items"][0] == {
        "id": RECURRING_MEETING_ID,
        "title": "Weekly team sync",
        "project": {"id": PROJECT_ID, "name": "Apollo migration"},
        "start_time": "2026-08-17T07:00:00Z",
        "time_zone": "Europe/Berlin",
        "frequency": "weekly",
        "interval": 1,
        "monthly_day": None,
        "monthly_ordinal": None,
        "monthly_weekday": None,
        "end_after": "never",
        "end_date": None,
        "iterations": None,
        "duration_hours": 1.5,
        "location": "Room 2.14",
    }
    monthly = content["items"][1]
    assert monthly["frequency"] == "monthly_nth_weekday"
    assert monthly["monthly_ordinal"] == 1
    assert monthly["monthly_weekday"] == "friday"
    assert monthly["end_after"] == "iterations"
    assert monthly["iterations"] == 12
    assert monthly["duration_hours"] == 1.0


async def test_a_pre_17_4_instance_degrades_the_listing_to_a_version_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """SPEC §4.7/G5: absence is in-band, and the note names the 17.4 floor."""
    mock_api.get("recurring_meetings").mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool("list_recurring_meetings", {})

    assert result.is_error is False
    assert result.structured_content is not None
    content = result.structured_content
    assert content["items"] == []
    assert content["pagination"]["total"] == 0
    note = content["notes"][0]
    assert note.startswith("unavailable (module not installed)")
    assert "17.4" in note
    assert "do not report that there are none" in note


async def test_a_forbidden_listing_names_the_view_meetings_permission(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("recurring_meetings").mock(return_value=httpx.Response(403, json=MODULE_FORBIDDEN))

    result = await mcp_client.call_tool("list_recurring_meetings", {})

    assert result.is_error is False
    assert result.structured_content is not None
    note = result.structured_content["notes"][0]
    assert note.startswith("no permission (403)")
    assert "'view meetings'" in note


# --- get_recurring_meeting ------------------------------------------------


async def test_get_recurring_meeting_returns_the_schedule_and_its_occurrences(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    series = mock_api.get(SERIES_PATH).mock(
        return_value=httpx.Response(200, json=RECURRING_MEETING)
    )
    upcoming = mock_api.get(UPCOMING_PATH).mock(
        return_value=httpx.Response(200, json=UPCOMING_OCCURRENCES)
    )

    result = await mcp_client.call_tool(
        "get_recurring_meeting", {"recurring_meeting_id": RECURRING_MEETING_ID}
    )

    assert series.call_count == 1
    assert upcoming.call_count == 1
    assert upcoming.calls[0].request.url.params["limit"] == "10"

    assert result.structured_content is not None
    detail = result.structured_content
    assert detail["id"] == RECURRING_MEETING_ID
    assert detail["title"] == "Weekly team sync"
    assert detail["time_zone"] == "Europe/Berlin"
    assert detail["author"] == {"id": 3, "name": "Grace Hopper"}
    assert detail["template_meeting_id"] == TEMPLATE_MEETING_ID
    assert detail["notes"] == []

    instantiated, planned = detail["occurrences"]
    # An instantiated slot carries the backing meeting's state and id...
    assert instantiated == {
        "start_time": OCCURRENCE_TOKEN,
        "state": "open",
        "meeting_id": OCCURRENCE_MEETING_ID,
    }
    # ...a schedule-only slot falls back to the synthetic 'planned' state, and
    # meeting_id stays null until somebody initialises it.
    assert planned == {
        "start_time": "2026-08-24T07:00:00Z",
        "state": "planned",
        "meeting_id": None,
    }


async def test_an_unreadable_occurrence_schedule_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(SERIES_PATH).mock(return_value=httpx.Response(200, json=RECURRING_MEETING))
    mock_api.get(UPCOMING_PATH).mock(return_value=httpx.Response(403, json=MODULE_FORBIDDEN))

    result = await mcp_client.call_tool(
        "get_recurring_meeting", {"recurring_meeting_id": RECURRING_MEETING_ID}
    )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["occurrences"] == []
    assert any("no permission (403)" in note for note in result.structured_content["notes"])


async def test_a_full_occurrence_page_is_marked_as_a_cap_not_the_end(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """The upcoming sub-collection does not paginate: a full page is a cap (G1)."""
    slots = [
        {
            "_type": "MeetingOccurrence",
            "startTime": f"2026-08-{17 + day:02d}T07:00:00Z",
            "state": "planned",
            "_links": {},
        }
        for day in range(10)
    ]
    mock_api.get(SERIES_PATH).mock(return_value=httpx.Response(200, json=RECURRING_MEETING))
    mock_api.get(UPCOMING_PATH).mock(return_value=httpx.Response(200, json=hal_collection(slots)))

    result = await mcp_client.call_tool(
        "get_recurring_meeting", {"recurring_meeting_id": RECURRING_MEETING_ID}
    )

    assert result.structured_content is not None
    assert len(result.structured_content["occurrences"]) == 10
    assert any("cap" in note for note in result.structured_content["notes"])


async def test_an_unknown_series_names_all_three_404_readings(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(SERIES_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))
    upcoming = mock_api.get(UPCOMING_PATH).mock(
        return_value=httpx.Response(200, json=UPCOMING_OCCURRENCES)
    )

    result = await mcp_client.call_tool(
        "get_recurring_meeting",
        {"recurring_meeting_id": RECURRING_MEETING_ID},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert f"recurring meeting {RECURRING_MEETING_ID} does not exist" in error["hint"]
    assert "Meetings module is not installed" in error["hint"]
    assert "17.4" in error["hint"]
    assert upcoming.call_count == 0


# --- create_recurring_meeting ---------------------------------------------


async def test_create_sends_the_wire_keys_and_corrects_the_overwritten_time_zone(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """The create-overwrite trap: OpenProject stores the account's zone on POST,
    so the requested one is applied with a follow-up PATCH (SPEC §6.13)."""
    create = mock_api.post("recurring_meetings").mock(
        return_value=httpx.Response(201, json=CREATED_SERIES)
    )
    fix_zone = mock_api.patch(f"recurring_meetings/{CREATED_SERIES_ID}").mock(
        return_value=httpx.Response(200, json=CREATED_SERIES_ZONE_FIXED)
    )
    mock_api.get(CREATED_UPCOMING_PATH).mock(
        return_value=httpx.Response(200, json=UPCOMING_OCCURRENCES)
    )

    result = await mcp_client.call_tool("create_recurring_meeting", dict(CREATE_ARGS))

    sent = body_of(create)
    assert sent["title"] == "Design review"
    assert sent["startTime"] == "2026-09-01T09:00:00+02:00"
    # A plain number of hours — never the ISO duration the /meetings API takes.
    assert sent["duration"] == 0.75
    assert sent["timeZone"] == "Europe/Berlin"
    assert sent["frequency"] == "weekly"
    assert sent["interval"] == 2
    assert sent["endAfter"] == "specific_date"
    assert sent["endDate"] == "2026-12-31"
    assert sent["location"] == "Room 2.14"
    assert sent["notify"] is False
    assert "monthlyDay" not in sent
    assert "iterations" not in sent
    assert sent["_links"] == {"project": {"href": f"/api/v3/projects/{PROJECT_ID}"}}

    assert fix_zone.call_count == 1
    assert body_of(fix_zone) == {"timeZone": "Europe/Berlin"}

    assert result.structured_content is not None
    detail = result.structured_content
    assert detail["id"] == CREATED_SERIES_ID
    assert detail["time_zone"] == "Europe/Berlin"
    assert detail["duration_hours"] == 0.75
    assert detail["template_meeting_id"] == CREATED_TEMPLATE_MEETING_ID
    assert len(detail["occurrences"]) == 2
    # The draft-template consequence is announced with the concrete fix.
    assert any(
        "DRAFT" in note
        and f"update_meeting(meeting_id={CREATED_TEMPLATE_MEETING_ID}, state='open')" in note
        for note in detail["notes"]
    )


async def test_create_skips_the_zone_fix_up_when_the_stored_zone_already_matches(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("recurring_meetings").mock(
        return_value=httpx.Response(201, json=CREATED_SERIES_UTC)
    )
    fix_zone = mock_api.patch(f"recurring_meetings/{CREATED_SERIES_ID}").mock(
        return_value=httpx.Response(200, json=CREATED_SERIES_UTC)
    )
    mock_api.get(CREATED_UPCOMING_PATH).mock(
        return_value=httpx.Response(200, json=UPCOMING_OCCURRENCES)
    )

    result = await mcp_client.call_tool(
        "create_recurring_meeting", {**CREATE_ARGS, "time_zone": "Etc/UTC"}
    )

    assert fix_zone.call_count == 0
    assert result.structured_content is not None
    assert result.structured_content["time_zone"] == "Etc/UTC"


async def test_a_refused_zone_fix_up_degrades_to_a_note_not_a_failure(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """G5: the series was created; a 403 on the corrective PATCH must not hide it."""
    mock_api.post("recurring_meetings").mock(return_value=httpx.Response(201, json=CREATED_SERIES))
    mock_api.patch(f"recurring_meetings/{CREATED_SERIES_ID}").mock(
        return_value=httpx.Response(403, json=MODULE_FORBIDDEN)
    )
    mock_api.get(CREATED_UPCOMING_PATH).mock(
        return_value=httpx.Response(200, json=UPCOMING_OCCURRENCES)
    )

    result = await mcp_client.call_tool("create_recurring_meeting", dict(CREATE_ARGS))

    assert result.is_error is False
    assert result.structured_content is not None
    detail = result.structured_content
    # The stored zone is reported honestly, and the note explains why.
    assert detail["time_zone"] == "Etc/UTC"
    assert any(
        "'Europe/Berlin' could not be applied" in note and "'edit meetings'" in note
        for note in detail["notes"]
    )


async def test_create_rejects_bad_frequency_combinations_before_the_wire(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("recurring_meetings").mock(
        return_value=httpx.Response(201, json=CREATED_SERIES)
    )

    cases: list[tuple[dict[str, Any], str]] = [
        # Missing companion fields — upstream's "infer from startTime" never
        # applies to API creates, so these must fail here, not there.
        ({"frequency": "monthly_day_of_month"}, "monthly_day is required"),
        ({"frequency": "monthly_nth_weekday"}, "monthly_ordinal AND monthly_weekday"),
        (
            {"frequency": "monthly_nth_weekday", "monthly_ordinal": -1},
            "monthly_ordinal AND monthly_weekday",
        ),
        # Fields for the wrong frequency would be stored misleadingly.
        ({"monthly_day": 15}, "do not apply"),
        (
            {"frequency": "monthly_day_of_month", "monthly_day": 15, "monthly_weekday": "friday"},
            "do not apply",
        ),
        # working_days force-sets interval=1 upstream; another value would be
        # silently ignored, so it is refused.
        ({"frequency": "working_days", "interval": 3}, "every working day"),
    ]
    for overrides, fragment in cases:
        result = await mcp_client.call_tool(
            "create_recurring_meeting",
            {**CREATE_ARGS, "frequency": "weekly", "interval": 1, **overrides},
            raise_on_error=False,
        )
        error = error_of(result)
        assert error["type"] == "invalid_input", overrides
        assert fragment in error["message"], overrides
        # Every rejection teaches the whole matrix.
        assert "Allowed combinations" in error["hint"], overrides
    assert route.call_count == 0


async def test_create_rejects_bad_end_after_combinations_before_the_wire(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("recurring_meetings").mock(
        return_value=httpx.Response(201, json=CREATED_SERIES)
    )

    base = {**CREATE_ARGS, "end_after": "never", "end_date": None}
    cases: list[tuple[dict[str, Any], str]] = [
        ({"end_after": "specific_date"}, "end_date is required"),
        ({"end_after": "iterations"}, "iterations is required"),
        (
            {"end_after": "specific_date", "end_date": "2026-12-31", "iterations": 5},
            "does not apply",
        ),
        (
            {"end_after": "iterations", "iterations": 5, "end_date": "2026-12-31"},
            "does not apply",
        ),
        ({"iterations": 5}, "do not apply"),
        ({"end_after": "specific_date", "end_date": "31.12.2026"}, "not an ISO date"),
    ]
    for overrides, fragment in cases:
        result = await mcp_client.call_tool(
            "create_recurring_meeting", {**base, **overrides}, raise_on_error=False
        )
        error = error_of(result)
        assert error["type"] == "invalid_input", overrides
        assert fragment in error["message"], overrides
    assert route.call_count == 0


async def test_create_rejects_an_unknown_time_zone_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """G2: OpenProject stores 'Mars/Base' without error and silently computes in
    the account's zone — the typo must die here instead."""
    route = mock_api.post("recurring_meetings").mock(
        return_value=httpx.Response(201, json=CREATED_SERIES)
    )

    result = await mcp_client.call_tool(
        "create_recurring_meeting", {**CREATE_ARGS, "time_zone": "Mars/Base"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "not a known IANA time zone" in error["message"]
    assert "Europe/Berlin" in error["hint"]
    assert route.call_count == 0


async def test_create_rejects_a_start_time_without_an_offset(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("recurring_meetings").mock(
        return_value=httpx.Response(201, json=CREATED_SERIES)
    )

    result = await mcp_client.call_tool(
        "create_recurring_meeting",
        {**CREATE_ARGS, "start_time": "2026-09-01T09:00:00"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "no UTC offset" in error["message"]
    assert route.call_count == 0


async def test_a_rejected_series_explains_the_future_start_rule(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("recurring_meetings").mock(
        return_value=httpx.Response(422, json=SERIES_START_REJECTED)
    )

    result = await mcp_client.call_tool(
        "create_recurring_meeting", dict(CREATE_ARGS), raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "startDate", "message": "Start date must be after today."}
    ]
    assert "now or later" in error["hint"]


async def test_create_maps_a_pre_17_4_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("recurring_meetings").mock(
        return_value=httpx.Response(404, json=MODULE_NOT_FOUND)
    )

    result = await mcp_client.call_tool(
        "create_recurring_meeting", dict(CREATE_ARGS), raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "17.4" in error["hint"]
    assert "recurring" in error["hint"]


# --- delete_recurring_meeting ---------------------------------------------


async def test_delete_series_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(SERIES_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_recurring_meeting",
        {"recurring_meeting_id": RECURRING_MEETING_ID},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    # The refusal names the real blast radius: the whole series, not one meeting.
    assert "EVERY instantiated occurrence" in error["hint"]
    assert route.call_count == 0


async def test_delete_series_deletes_and_confirms(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(SERIES_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_recurring_meeting",
        {"recurring_meeting_id": RECURRING_MEETING_ID, "confirm": True},
    )

    assert route.call_count == 1
    assert result.structured_content is not None
    outcome = result.structured_content
    assert outcome["id"] == RECURRING_MEETING_ID
    assert outcome["deleted"] is True
    assert "every instantiated occurrence" in outcome["message"]


async def test_delete_series_maps_a_pre_17_4_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(SERIES_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "delete_recurring_meeting",
        {"recurring_meeting_id": RECURRING_MEETING_ID, "confirm": True},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "17.4" in error["hint"]


# --- init_recurring_meeting_occurrence ------------------------------------


async def test_init_normalizes_the_instant_to_utc_and_returns_the_meeting(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """The path token is the canonical UTC 'Z' form even when the caller passes
    an offset — a literal '+' must never enter the URL path."""
    init = mock_api.post(f"{OCCURRENCE_PATH}/init").mock(
        return_value=httpx.Response(201, json=OCCURRENCE_MEETING)
    )
    agenda = mock_api.get(f"meetings/{OCCURRENCE_MEETING_ID}/agenda_items").mock(
        return_value=httpx.Response(200, json=hal_collection([]))
    )

    result = await mcp_client.call_tool(
        "init_recurring_meeting_occurrence",
        {
            "recurring_meeting_id": RECURRING_MEETING_ID,
            "start_time": "2026-08-17T09:00:00+02:00",
        },
    )

    assert init.call_count == 1
    sent = init.calls[0].request
    assert sent.url.path.endswith(f"/occurrences/{OCCURRENCE_TOKEN}/init")
    # No payload is defined upstream, but a bodyless POST answers 406 — the
    # empty JSON object carries the content-type.
    assert json.loads(sent.content) == {}
    assert "application/json" in sent.headers["content-type"]
    assert agenda.call_count == 1

    assert result.structured_content is not None
    detail = result.structured_content
    # The full get_meeting shape: this is now a normal meeting with its own id.
    assert detail["id"] == OCCURRENCE_MEETING_ID
    assert detail["title"] == "Weekly team sync"
    assert detail["state"] == "open"
    assert detail["participants"] == [
        {"id": 3, "name": "Grace Hopper"},
        {"id": 5, "name": "Alan Turing"},
    ]
    assert detail["agenda_items"] == []


async def test_init_takes_the_canonical_token_verbatim(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    init = mock_api.post(f"{OCCURRENCE_PATH}/init").mock(
        return_value=httpx.Response(201, json=OCCURRENCE_MEETING)
    )
    mock_api.get(f"meetings/{OCCURRENCE_MEETING_ID}/agenda_items").mock(
        return_value=httpx.Response(200, json=hal_collection([]))
    )

    await mcp_client.call_tool(
        "init_recurring_meeting_occurrence",
        {"recurring_meeting_id": RECURRING_MEETING_ID, "start_time": OCCURRENCE_TOKEN},
    )

    assert init.call_count == 1


async def test_init_rejects_an_offsetless_instant_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """G2: matching is exact-instant upstream, so a zone-less time may not be
    guessed into one."""
    result = await mcp_client.call_tool(
        "init_recurring_meeting_occurrence",
        {"recurring_meeting_id": RECURRING_MEETING_ID, "start_time": "2026-08-17T09:00:00"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "no UTC offset" in error["message"]


async def test_init_against_a_draft_template_explains_the_500(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """The upstream failure mode is an unclean 500, not a 422 — the hint must
    carry the fix (publish the template) instead of 'server unhealthy'."""
    mock_api.post(f"{OCCURRENCE_PATH}/init").mock(return_value=httpx.Response(500, text="boom"))

    result = await mcp_client.call_tool(
        "init_recurring_meeting_occurrence",
        {"recurring_meeting_id": RECURRING_MEETING_ID, "start_time": OCCURRENCE_TOKEN},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "upstream_server_error"
    assert error["http_status"] == 500
    assert "DRAFT" in error["hint"]
    assert "state='open'" in error["hint"]
    assert "template_meeting_id" in error["hint"]


async def test_init_maps_a_pre_17_4_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(f"{OCCURRENCE_PATH}/init").mock(
        return_value=httpx.Response(404, json=MODULE_NOT_FOUND)
    )

    result = await mcp_client.call_tool(
        "init_recurring_meeting_occurrence",
        {"recurring_meeting_id": RECURRING_MEETING_ID, "start_time": OCCURRENCE_TOKEN},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "17.4" in error["hint"]


async def test_a_forbidden_init_names_the_create_meetings_permission(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(f"{OCCURRENCE_PATH}/init").mock(
        return_value=httpx.Response(403, json=MODULE_FORBIDDEN)
    )

    result = await mcp_client.call_tool(
        "init_recurring_meeting_occurrence",
        {"recurring_meeting_id": RECURRING_MEETING_ID, "start_time": OCCURRENCE_TOKEN},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "permission_denied"
    assert "'create meetings'" in error["hint"]


# --- cancel_recurring_meeting_occurrence ----------------------------------


async def test_cancel_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(OCCURRENCE_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "cancel_recurring_meeting_occurrence",
        {"recurring_meeting_id": RECURRING_MEETING_ID, "start_time": OCCURRENCE_TOKEN},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert route.call_count == 0


async def test_cancel_normalizes_the_instant_and_confirms(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(OCCURRENCE_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "cancel_recurring_meeting_occurrence",
        {
            "recurring_meeting_id": RECURRING_MEETING_ID,
            "start_time": "2026-08-17T09:00:00+02:00",
            "confirm": True,
        },
    )

    assert route.call_count == 1
    assert route.calls[0].request.url.path.endswith(f"/occurrences/{OCCURRENCE_TOKEN}")
    assert result.structured_content is not None
    outcome = result.structured_content
    assert outcome["recurring_meeting_id"] == RECURRING_MEETING_ID
    # The normalized instant is echoed so the caller knows what was acted on.
    assert outcome["start_time"] == OCCURRENCE_TOKEN
    assert outcome["cancelled"] is True
    assert "init_recurring_meeting_occurrence" in outcome["message"]


async def test_cancelling_an_instantiated_occurrence_points_at_delete_meeting(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """The 409 is not a lock conflict: the occurrence is live, and the honest
    next step is deleting the meeting itself."""
    mock_api.delete(OCCURRENCE_PATH).mock(return_value=httpx.Response(409, json=CANCEL_CONFLICT))

    result = await mcp_client.call_tool(
        "cancel_recurring_meeting_occurrence",
        {
            "recurring_meeting_id": RECURRING_MEETING_ID,
            "start_time": OCCURRENCE_TOKEN,
            "confirm": True,
        },
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    assert "delete_meeting" in error["hint"]
    assert "meeting_id" in error["hint"]


async def test_cancel_maps_a_pre_17_4_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(OCCURRENCE_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "cancel_recurring_meeting_occurrence",
        {
            "recurring_meeting_id": RECURRING_MEETING_ID,
            "start_time": OCCURRENCE_TOKEN,
            "confirm": True,
        },
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "17.4" in error["hint"]
