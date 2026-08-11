"""Protocol tests for the collaboration-module tools (SPEC §6.13, §4.7, §9.3, G5).

Every call goes through the in-memory FastMCP client against a respx-mocked
instance, so these assert what a model actually receives. Two themes run through
the file: the projected *contents* (ids, titles, hours, outcomes, the wire keys a
write puts on the line) and the module gating — a 404 and a 403 mean different
things, and neither is ever allowed to look like "there are none". The gating has
two shapes and the tests pin both: a **list** tool degrades to an empty envelope
plus the reason in ``notes`` (SPEC §4.7, G5), while a detail read or a write, which
has no ``notes`` to carry it, raises the typed error with the same explanation.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.modules_collab_payloads import (
    AGENDA_ITEM_COLLECTION,
    AGENDA_ITEM_REJECTED,
    AGENDA_ITEM_SIMPLE,
    BUDGET_COLLECTION,
    CREATED_AGENDA_ITEM,
    CREATED_AGENDA_ITEM_ID,
    CREATED_MEETING,
    CREATED_MEETING_ID,
    CREATED_OUTCOME,
    CREATED_OUTCOME_ID,
    DOCUMENT,
    DOCUMENT_COLLECTION,
    DOCUMENT_ID,
    FRESH_MEETING_AFTER_CONFLICT,
    INVALID_SORT_ERROR,
    INVALID_TIME_FILTER_ERROR,
    MEETING,
    MEETING_COLLECTION,
    MEETING_EMBEDDED_PARTICIPANTS_ONLY,
    MEETING_FORM,
    MEETING_FORM_BLANK_TITLE,
    MEETING_ID,
    MEETING_NOT_EDITABLE,
    MODULE_FORBIDDEN,
    MODULE_NOT_FOUND,
    OUTCOME_ID,
    OUTCOME_NOT_EDITABLE,
    PAST_MEETING_ID,
    PROJECT_ID,
    PROJECT_IDENTIFIER,
    STALE_LOCK_CONFLICT,
    UPDATED_AGENDA_ITEM,
    UPDATED_MEETING,
    UPDATED_OUTCOME,
    WIKI_PAGE,
    WIKI_PAGE_ID,
    WORK_PACKAGE_ID,
    hal_collection,
)

MEETING_PATH = f"meetings/{MEETING_ID}"
AGENDA_PATH = f"{MEETING_PATH}/agenda_items"
BUDGETS_PATH = f"projects/{PROJECT_ID}/budgets"
AGENDA_ITEM_PATH = "meeting_agenda_items/91"
OUTCOME_PATH = f"meeting_outcomes/{OUTCOME_ID}"

TOOL_NAMES = (
    "list_meetings",
    "get_meeting",
    "create_meeting",
    "update_meeting",
    "delete_meeting",
    "add_meeting_agenda_item",
    "update_meeting_agenda_item",
    "delete_meeting_agenda_item",
    "add_meeting_outcome",
    "update_meeting_outcome",
    "delete_meeting_outcome",
    "get_wiki_page",
    "list_documents",
    "get_document",
    "list_budgets",
)


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def body_of(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


def filters_of(route: respx.Route, index: int = 0) -> list[dict[str, Any]]:
    raw = route.calls[index].request.url.params.get("filters")
    return json.loads(raw) if raw else []


# --- registration ---------------------------------------------------------


async def test_all_fifteen_tools_are_registered_with_honest_annotations(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    for name in TOOL_NAMES:
        assert name in tools, name
        assert tools[name].outputSchema is not None
        assert tools[name].annotations is not None

    listing = tools["list_meetings"]
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert listing.annotations.destructiveHint is False
    assert set(listing.inputSchema["properties"]) == {
        "project_id",
        "upcoming_only",
        "page",
        "page_size",
    }
    assert set((listing.meta or {})["fastmcp"]["tags"]) == {"meetings", "read"}

    creating = tools["create_meeting"]
    assert creating.annotations is not None
    assert creating.annotations.readOnlyHint is False
    assert creating.annotations.destructiveHint is False
    assert set(creating.inputSchema["properties"]) == {
        "project_id",
        "title",
        "start_time",
        "duration_minutes",
        "participants",
    }
    assert set((creating.meta or {})["fastmcp"]["tags"]) == {"meetings", "write"}

    agenda = tools["add_meeting_agenda_item"]
    assert set(agenda.inputSchema["properties"]) == {
        "meeting_id",
        "title",
        "notes",
        "duration_minutes",
        "work_package_id",
    }

    updating = tools["update_meeting"]
    assert updating.annotations is not None
    assert updating.annotations.readOnlyHint is False
    assert updating.annotations.destructiveHint is False
    assert set(updating.inputSchema["properties"]) == {
        "meeting_id",
        "title",
        "location",
        "start_time",
        "duration_minutes",
        "state",
        "participants",
        "lock_version",
    }
    assert set((updating.meta or {})["fastmcp"]["tags"]) == {"meetings", "write"}

    assert set(tools["update_meeting_agenda_item"].inputSchema["properties"]) == {
        "agenda_item_id",
        "title",
        "notes",
        "duration_minutes",
        "position",
        "presenter_id",
        "work_package_id",
        "lock_version",
    }
    assert set(tools["add_meeting_outcome"].inputSchema["properties"]) == {
        "agenda_item_id",
        "kind",
        "notes",
        "work_package_id",
    }
    assert set(tools["update_meeting_outcome"].inputSchema["properties"]) == {
        "outcome_id",
        "kind",
        "notes",
        "work_package_id",
    }

    # The three deletes carry the destructive contract: annotations, tags,
    # and (asserted separately below) the confirm=false refusal.
    for name, id_field in (
        ("delete_meeting", "meeting_id"),
        ("delete_meeting_agenda_item", "agenda_item_id"),
        ("delete_meeting_outcome", "outcome_id"),
    ):
        deleting = tools[name]
        assert deleting.annotations is not None, name
        assert deleting.annotations.destructiveHint is True, name
        assert set(deleting.inputSchema["properties"]) == {id_field, "confirm"}, name
        assert set((deleting.meta or {})["fastmcp"]["tags"]) == {
            "meetings",
            "write",
            "destructive",
        }, name

    assert set(tools["get_wiki_page"].inputSchema["properties"]) == {"wiki_page_id"}
    assert set(tools["list_documents"].inputSchema["properties"]) == {"page", "page_size"}
    assert set(tools["get_document"].inputSchema["properties"]) == {"document_id"}
    assert set(tools["list_budgets"].inputSchema["properties"]) == {"project_id"}
    assert set((tools["get_wiki_page"].meta or {})["fastmcp"]["tags"]) == {"wiki", "read"}
    assert set((tools["list_documents"].meta or {})["fastmcp"]["tags"]) == {"documents", "read"}
    assert set((tools["list_budgets"].meta or {})["fastmcp"]["tags"]) == {"budgets", "read"}


async def test_descriptions_state_the_api_limits_a_model_must_pass_on(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    wiki = tools["get_wiki_page"].description or ""
    assert "wiki index and no wiki search" in wiki
    assert "CONTENT is not exposed" in wiki
    assert "list_attachments(container_type='wiki_page'" in wiki
    id_description = tools["get_wiki_page"].inputSchema["properties"]["wiki_page_id"]["description"]
    assert "URL" in id_description

    budgets = tools["list_budgets"].description or ""
    assert "no amounts" in budgets

    meetings = tools["list_meetings"].description or ""
    assert "get_meeting" in meetings
    assert "404" in meetings
    # The Ⓜ list contract is taught in-band: absence lands in notes, not in an error.
    assert "notes" in meetings

    # The id-consuming tools name their id-producing path (SPEC §5.10).
    assert "list_meetings" in (tools["get_meeting"].description or "")
    assert "list_meetings" in (tools["add_meeting_agenda_item"].description or "")
    assert "list_documents" in (tools["get_document"].description or "")


# --- list_meetings --------------------------------------------------------


async def test_list_meetings_filters_by_project_and_time_and_sorts_by_start(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("meetings").mock(return_value=httpx.Response(200, json=MEETING_COLLECTION))

    result = await mcp_client.call_tool("list_meetings", {"project_id": PROJECT_ID})

    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["offset"] == "1"
    assert params["pageSize"] == "20"
    assert params["sortBy"] == '[["startTime","asc"]]'
    assert filters_of(route) == [
        {"project": {"operator": "=", "values": [str(PROJECT_ID)]}},
        {"time": {"operator": "upcoming", "values": []}},
    ]

    assert result.structured_content is not None
    content = result.structured_content
    assert content["pagination"] == {"total": 37, "page": 1, "page_size": 20, "has_more": True}
    assert content["notes"] is None
    assert content["items"][0] == {
        "id": MEETING_ID,
        "title": "Sprint 12 planning",
        "project": {"id": PROJECT_ID, "name": "Apollo migration"},
        "start_time": "2026-08-03T14:00:00Z",
        "end_time": "2026-08-03T15:30:00Z",
        "duration_hours": 1.5,
        "location": "Room 2.14",
        "state": "open",
    }
    past = content["items"][1]
    assert past["id"] == PAST_MEETING_ID
    assert past["duration_hours"] == 1.0
    assert past["state"] == "closed"


async def test_list_meetings_without_upcoming_only_drops_the_time_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("meetings").mock(return_value=httpx.Response(200, json=MEETING_COLLECTION))

    await mcp_client.call_tool("list_meetings", {"upcoming_only": False, "page": 2})

    assert filters_of(route) == []
    params = route.calls[0].request.url.params
    assert params["sortBy"] == '[["startTime","desc"]]'
    assert params["offset"] == "2"


async def test_list_meetings_rejects_a_project_identifier_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("meetings").mock(return_value=httpx.Response(200, json=MEETING_COLLECTION))

    result = await mcp_client.call_tool(
        "list_meetings", {"project_id": PROJECT_IDENTIFIER}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "numeric project id" in error["message"]
    assert "list_projects" in error["hint"]
    assert route.call_count == 0


async def test_an_instance_that_refuses_the_sort_still_answers_and_says_so(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """G5: an unsorted page beats a failed call, but the model is told it is unsorted."""

    def refuse_sort(request: httpx.Request) -> httpx.Response:
        if "sortBy" in request.url.params:
            return httpx.Response(400, json=INVALID_SORT_ERROR)
        return httpx.Response(200, json=MEETING_COLLECTION)

    route = mock_api.get("meetings").mock(side_effect=refuse_sort)

    result = await mcp_client.call_tool("list_meetings", {})

    # Ladder: operator+sort (400) → values+sort (400) → operator, unsorted (200).
    assert route.call_count == 3
    assert "sortBy" in route.calls[0].request.url.params
    assert "sortBy" in route.calls[1].request.url.params
    assert "sortBy" not in route.calls[2].request.url.params
    assert filters_of(route, 2) == [{"time": {"operator": "upcoming", "values": []}}]
    assert result.structured_content is not None
    assert len(result.structured_content["items"]) == 2
    assert any("unsorted" in note for note in result.structured_content["notes"])


async def test_a_pre_17_6_instance_gets_the_legacy_time_values_and_it_is_remembered(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """SPEC §4.7: 16.6 rejects the value-less `upcoming` operator (live-confirmed);
    the tool falls back to `=` + 'future', caches the dialect, and later calls
    skip the failing attempt entirely."""

    def refuse_operator_dialect(request: httpx.Request) -> httpx.Response:
        if "upcoming" in request.url.params.get("filters", ""):
            return httpx.Response(400, json=INVALID_TIME_FILTER_ERROR)
        return httpx.Response(200, json=MEETING_COLLECTION)

    route = mock_api.get("meetings").mock(side_effect=refuse_operator_dialect)

    result = await mcp_client.call_tool("list_meetings", {})

    assert route.call_count == 2
    assert filters_of(route, 1) == [{"time": {"operator": "=", "values": ["future"]}}]
    # The sort survives the dialect fallback, and the result carries no
    # degradation note — both dialects mean exactly the same thing.
    assert route.calls[1].request.url.params["sortBy"] == '[["startTime","asc"]]'
    assert result.structured_content is not None
    assert len(result.structured_content["items"]) == 2
    assert result.structured_content["notes"] is None

    await mcp_client.call_tool("list_meetings", {})

    # The dialect was cached: the second tool call goes straight to legacy.
    assert route.call_count == 3
    assert filters_of(route, 2) == [{"time": {"operator": "=", "values": ["future"]}}]


async def test_a_genuinely_invalid_meetings_query_still_surfaces_the_error(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """When every rung of the dialect/sort ladder is rejected, the last 400 is
    the answer — the fallback never turns a real validation error into silence."""
    route = mock_api.get("meetings").mock(
        return_value=httpx.Response(400, json=INVALID_TIME_FILTER_ERROR)
    )

    result = await mcp_client.call_tool("list_meetings", {}, raise_on_error=False)

    assert route.call_count == 4  # operator+sort, values+sort, operator, values
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 400


async def test_a_missing_meetings_module_degrades_to_a_note_not_a_silent_empty_list(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """SPEC §4.7/G5: absence is in-band, and it must not read as 'no meetings'."""
    mock_api.get("meetings").mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool("list_meetings", {})

    assert result.is_error is False
    assert result.structured_content is not None
    content = result.structured_content
    assert content["items"] == []
    assert content["pagination"]["total"] == 0
    assert content["pagination"]["has_more"] is False
    assert len(content["notes"]) == 1
    note = content["notes"][0]
    assert note.startswith("unavailable (module not installed)")
    assert "Meetings module is not installed" in note
    assert "do not report that there are none" in note


async def test_a_forbidden_meetings_module_names_the_missing_permission_in_notes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("meetings").mock(return_value=httpx.Response(403, json=MODULE_FORBIDDEN))

    result = await mcp_client.call_tool("list_meetings", {})

    assert result.is_error is False
    assert result.structured_content is not None
    content = result.structured_content
    assert content["items"] == []
    note = content["notes"][0]
    assert note.startswith("no permission (403)")
    assert "'view meetings'" in note
    assert "list_permissions" in note


async def test_an_unsorted_page_from_a_module_free_instance_carries_both_notes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """The sort degradation and the module degradation compose rather than replace."""

    def refuse_sort_then_vanish(request: httpx.Request) -> httpx.Response:
        if "sortBy" in request.url.params:
            return httpx.Response(400, json=INVALID_SORT_ERROR)
        return httpx.Response(404, json=MODULE_NOT_FOUND)

    route = mock_api.get("meetings").mock(side_effect=refuse_sort_then_vanish)

    result = await mcp_client.call_tool("list_meetings", {})

    assert route.call_count == 3  # operator+sort, values+sort, operator unsorted → 404
    assert result.is_error is False
    assert result.structured_content is not None
    notes = result.structured_content["notes"]
    assert any("unsorted" in note for note in notes)
    assert any("unavailable (module not installed)" in note for note in notes)


# --- get_meeting ----------------------------------------------------------


async def test_get_meeting_returns_participants_agenda_and_outcomes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    meeting = mock_api.get(MEETING_PATH).mock(return_value=httpx.Response(200, json=MEETING))
    agenda = mock_api.get(AGENDA_PATH).mock(
        return_value=httpx.Response(200, json=AGENDA_ITEM_COLLECTION)
    )

    result = await mcp_client.call_tool("get_meeting", {"meeting_id": MEETING_ID})

    assert meeting.call_count == 1
    assert agenda.call_count == 1
    assert result.structured_content is not None
    detail = result.structured_content

    assert detail["id"] == MEETING_ID
    assert detail["title"] == "Sprint 12 planning"
    assert detail["duration_hours"] == 1.5
    # The lock version is surfaced so update_meeting can echo it (SPEC §4.4).
    assert detail["lock_version"] == 3
    assert detail["author"] == {"id": 3, "name": "Grace Hopper"}
    assert detail["participants"] == [
        {"id": 3, "name": "Grace Hopper"},
        {"id": 5, "name": "Alan Turing"},
    ]
    assert detail["created_at"] == "2026-07-20T09:00:00Z"

    simple, linked, undisclosed = detail["agenda_items"]
    assert simple == {
        "id": 91,
        "title": "Capacity check",
        "notes": "Two people on vacation in week 33.",
        "duration_minutes": 15,
        "position": 1,
        "item_type": "simple",
        "presenter": {"id": 5, "name": "Alan Turing"},
        "work_package": None,
        "section": {"id": 6, "name": "Agenda"},
        "outcomes": [],
        "lock_version": 0,
    }
    assert linked["item_type"] == "work_package"
    assert linked["work_package"] == {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"}
    assert linked["duration_minutes"] == 30
    assert linked["outcomes"] == [
        {
            "id": 12,
            "kind": "decision",
            "notes": "Ship on Friday, feature flag stays off.",
            "author": {"id": 3, "name": "Grace Hopper"},
            "work_package": {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"},
        }
    ]

    # The undisclosed URN must never become an id (G2/G3) — it becomes a note.
    assert undisclosed["work_package"] is None
    assert any("93" in note and "may not view" in note for note in detail["notes"])


async def test_get_meeting_falls_back_to_embedded_participants(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(MEETING_PATH).mock(
        return_value=httpx.Response(200, json=MEETING_EMBEDDED_PARTICIPANTS_ONLY)
    )
    mock_api.get(AGENDA_PATH).mock(return_value=httpx.Response(200, json=hal_collection([])))

    result = await mcp_client.call_tool("get_meeting", {"meeting_id": MEETING_ID})

    assert result.structured_content is not None
    assert result.structured_content["participants"] == [
        {"id": 3, "name": "Grace Hopper"},
        {"id": 5, "name": "Alan Turing"},
    ]
    assert result.structured_content["agenda_items"] == []
    assert result.structured_content["notes"] == []


async def test_an_unreadable_agenda_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(MEETING_PATH).mock(return_value=httpx.Response(200, json=MEETING))
    mock_api.get(AGENDA_PATH).mock(return_value=httpx.Response(403, json=MODULE_FORBIDDEN))

    result = await mcp_client.call_tool("get_meeting", {"meeting_id": MEETING_ID})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["title"] == "Sprint 12 planning"
    assert result.structured_content["agenda_items"] == []
    assert any("no permission (403)" in note for note in result.structured_content["notes"])


async def test_a_missing_agenda_endpoint_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(MEETING_PATH).mock(return_value=httpx.Response(200, json=MEETING))
    mock_api.get(AGENDA_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool("get_meeting", {"meeting_id": MEETING_ID})

    assert result.structured_content is not None
    assert result.structured_content["agenda_items"] == []
    assert any(
        "agenda_items" in note and "404" in note for note in result.structured_content["notes"]
    )


async def test_an_unknown_meeting_fails_before_the_agenda_is_fetched(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(MEETING_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))
    agenda = mock_api.get(AGENDA_PATH).mock(
        return_value=httpx.Response(200, json=AGENDA_ITEM_COLLECTION)
    )

    result = await mcp_client.call_tool(
        "get_meeting", {"meeting_id": MEETING_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert f"meeting {MEETING_ID} does not exist" in error["hint"]
    assert "Meetings module is not installed" in error["hint"]
    assert agenda.call_count == 0


# --- create_meeting -------------------------------------------------------


async def test_create_meeting_goes_through_the_form_and_sends_the_wire_keys(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("meetings/form").mock(return_value=httpx.Response(200, json=MEETING_FORM))
    create = mock_api.post("meetings").mock(return_value=httpx.Response(201, json=CREATED_MEETING))

    result = await mcp_client.call_tool(
        "create_meeting",
        {
            "project_id": PROJECT_ID,
            "title": "Design review",
            "start_time": "2026-09-01T09:00:00Z",
            "duration_minutes": 45,
            "participants": [3, 5],
        },
    )

    assert form.call_count == 1
    sent = body_of(form)
    assert sent["title"] == "Design review"
    assert sent["startTime"] == "2026-09-01T09:00:00Z"
    # Minutes in, ISO duration on the wire (SPEC §5.8).
    assert sent["duration"] == "PT45M"
    assert sent["_links"] == {
        "project": {"href": f"/api/v3/projects/{PROJECT_ID}"},
        "participants": [
            {"href": "/api/v3/users/3"},
            {"href": "/api/v3/users/5"},
        ],
    }

    committed = body_of(create)
    assert committed["duration"] == "PT45M"
    # The form's defaults survive; our own attributes win over its echo.
    assert committed["state"] == "draft"
    assert committed["_links"]["participants"] == sent["_links"]["participants"]
    assert "self" not in committed["_links"]

    assert result.structured_content is not None
    detail = result.structured_content
    assert detail["id"] == CREATED_MEETING_ID
    assert detail["state"] == "draft"
    assert detail["duration_hours"] == 0.75
    assert detail["agenda_items"] == []
    assert detail["participants"] == [
        {"id": 3, "name": "Grace Hopper"},
        {"id": 5, "name": "Alan Turing"},
    ]


async def test_create_meeting_sends_an_hour_and_a_half_as_pt1h30m(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("meetings/form").mock(return_value=httpx.Response(200, json=MEETING_FORM))
    mock_api.post("meetings").mock(return_value=httpx.Response(201, json=CREATED_MEETING))

    await mcp_client.call_tool(
        "create_meeting",
        {
            "project_id": PROJECT_IDENTIFIER,
            "title": "Design review",
            "start_time": "2026-09-01T11:00:00+02:00",
            "duration_minutes": 90,
        },
    )

    sent = body_of(form)
    assert sent["duration"] == "PT1H30M"
    assert sent["startTime"] == "2026-09-01T11:00:00+02:00"
    # The create path takes an identifier, unlike the meetings *filter*.
    assert sent["_links"] == {"project": {"href": f"/api/v3/projects/{PROJECT_IDENTIFIER}"}}
    assert "participants" not in sent["_links"]


async def test_create_meeting_rejects_a_start_time_without_an_offset(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("meetings/form").mock(return_value=httpx.Response(200, json=MEETING_FORM))

    result = await mcp_client.call_tool(
        "create_meeting",
        {
            "project_id": PROJECT_ID,
            "title": "Design review",
            "start_time": "2026-09-01T09:00:00",
            "duration_minutes": 45,
        },
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "no UTC offset" in error["message"]
    assert "2026-08-03T14:00:00Z" in error["hint"]
    assert form.call_count == 0


async def test_create_meeting_surfaces_form_violations_and_never_commits(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("meetings/form").mock(
        return_value=httpx.Response(200, json=MEETING_FORM_BLANK_TITLE)
    )
    create = mock_api.post("meetings").mock(return_value=httpx.Response(201, json=CREATED_MEETING))

    result = await mcp_client.call_tool(
        "create_meeting",
        {
            "project_id": PROJECT_ID,
            "title": "-",
            "start_time": "2026-09-01T09:00:00Z",
            "duration_minutes": 45,
        },
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [{"attribute": "title", "message": "Title can't be blank."}]
    assert "non-empty title" in error["hint"]
    assert create.call_count == 0


async def test_create_meeting_reports_a_missing_module_on_the_form_call(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("meetings/form").mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))
    create = mock_api.post("meetings").mock(return_value=httpx.Response(201, json=CREATED_MEETING))

    result = await mcp_client.call_tool(
        "create_meeting",
        {
            "project_id": PROJECT_ID,
            "title": "Design review",
            "start_time": "2026-09-01T09:00:00Z",
            "duration_minutes": 45,
        },
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "Meetings module is not installed" in error["hint"]
    assert create.call_count == 0


# --- add_meeting_agenda_item ---------------------------------------------


async def test_add_agenda_item_links_a_work_package_as_a_work_package_item(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("meeting_agenda_items").mock(
        return_value=httpx.Response(201, json=CREATED_AGENDA_ITEM)
    )

    result = await mcp_client.call_tool(
        "add_meeting_agenda_item",
        {
            "meeting_id": MEETING_ID,
            "title": "Release readiness",
            "notes": "Go / no-go for 2.1.",
            "duration_minutes": 15,
            "work_package_id": WORK_PACKAGE_ID,
        },
    )

    sent = body_of(route)
    assert sent["title"] == "Release readiness"
    assert sent["notes"] == {"format": "markdown", "raw": "Go / no-go for 2.1."}
    assert sent["durationInMinutes"] == 15
    assert sent["itemType"] == "work_package"
    assert sent["_links"] == {
        "meeting": {"href": f"/api/v3/meetings/{MEETING_ID}"},
        "workPackage": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}"},
    }

    assert result.structured_content is not None
    item = result.structured_content
    assert item["id"] == CREATED_AGENDA_ITEM_ID
    assert item["position"] == 4
    assert item["duration_minutes"] == 15
    assert item["notes"] == "Go / no-go for 2.1."
    assert item["work_package"] == {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"}
    assert item["meeting"] == {"id": MEETING_ID, "name": "Sprint 12 planning"}
    assert item["created_at"] == "2026-07-26T12:05:00Z"
    assert item["outcomes"] == []


async def test_add_agenda_item_without_a_work_package_stays_a_simple_item(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("meeting_agenda_items").mock(
        return_value=httpx.Response(201, json=CREATED_AGENDA_ITEM)
    )

    await mcp_client.call_tool(
        "add_meeting_agenda_item", {"meeting_id": MEETING_ID, "title": "Capacity check"}
    )

    sent = body_of(route)
    assert "itemType" not in sent
    assert "notes" not in sent
    assert "durationInMinutes" not in sent
    assert sent["_links"] == {"meeting": {"href": f"/api/v3/meetings/{MEETING_ID}"}}


async def test_a_rejected_agenda_item_explains_the_usual_causes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("meeting_agenda_items").mock(
        return_value=httpx.Response(422, json=AGENDA_ITEM_REJECTED)
    )

    result = await mcp_client.call_tool(
        "add_meeting_agenda_item",
        {"meeting_id": MEETING_ID, "title": "Release readiness", "work_package_id": 999},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "workPackage", "message": "Work package is invalid."}
    ]
    assert "not visible to this account" in error["hint"]


async def test_add_agenda_item_reports_a_missing_module(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("meeting_agenda_items").mock(
        return_value=httpx.Response(404, json=MODULE_NOT_FOUND)
    )

    result = await mcp_client.call_tool(
        "add_meeting_agenda_item",
        {"meeting_id": MEETING_ID, "title": "Release readiness"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "Meetings module is not installed" in error["hint"]


async def test_add_agenda_item_rejects_an_empty_title_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("meeting_agenda_items").mock(
        return_value=httpx.Response(201, json=CREATED_AGENDA_ITEM)
    )

    result = await mcp_client.call_tool(
        "add_meeting_agenda_item",
        {"meeting_id": MEETING_ID, "title": "   "},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert route.call_count == 0


# --- update_meeting -------------------------------------------------------


async def test_update_meeting_patches_with_the_supplied_lock_version(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """The caller's lock_version is echoed as-is: no pre-read, one PATCH."""
    patch = mock_api.patch(MEETING_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_MEETING)
    )
    mock_api.get(AGENDA_PATH).mock(return_value=httpx.Response(200, json=hal_collection([])))

    result = await mcp_client.call_tool(
        "update_meeting",
        {
            "meeting_id": MEETING_ID,
            "title": "Sprint 12 planning (moved)",
            "start_time": "2026-08-04T14:00:00Z",
            "duration_minutes": 60,
            "state": "open",
            "participants": [3, 5],
            "lock_version": 3,
        },
    )

    assert patch.call_count == 1
    sent = body_of(patch)
    assert sent == {
        "title": "Sprint 12 planning (moved)",
        "startTime": "2026-08-04T14:00:00Z",
        "duration": "PT1H",
        "state": "open",
        "lockVersion": 3,
        "_links": {"participants": [{"href": "/api/v3/users/3"}, {"href": "/api/v3/users/5"}]},
    }

    assert result.structured_content is not None
    detail = result.structured_content
    assert detail["id"] == MEETING_ID
    assert detail["title"] == "Sprint 12 planning (moved)"
    assert detail["duration_hours"] == 1.0
    assert detail["lock_version"] == 4
    assert detail["agenda_items"] == []
    assert detail["notes"] == []


async def test_update_meeting_fetches_the_lock_version_when_none_is_supplied(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """SPEC §4.4: never PATCH blind — the current version is read and echoed."""
    read = mock_api.get(MEETING_PATH).mock(return_value=httpx.Response(200, json=MEETING))
    patch = mock_api.patch(MEETING_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_MEETING)
    )
    mock_api.get(AGENDA_PATH).mock(return_value=httpx.Response(200, json=hal_collection([])))

    await mcp_client.call_tool("update_meeting", {"meeting_id": MEETING_ID, "location": None})

    assert read.call_count == 1
    sent = body_of(patch)
    # MEETING reports lockVersion 3; passing null clears the location.
    assert sent == {"location": "", "lockVersion": 3}


async def test_update_meeting_with_a_stale_lock_version_returns_the_fresh_state(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch(MEETING_PATH).mock(return_value=httpx.Response(409, json=STALE_LOCK_CONFLICT))
    mock_api.get(MEETING_PATH).mock(
        return_value=httpx.Response(200, json=FRESH_MEETING_AFTER_CONFLICT)
    )

    result = await mcp_client.call_tool(
        "update_meeting",
        {"meeting_id": MEETING_ID, "title": "Sprint 12 planning (moved)", "lock_version": 2},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["http_status"] == 409
    # The fresh lockVersion and the differing field travel in the error, so the
    # model can re-read, decide and retry deliberately (SPEC §4.4).
    assert error["lock_version"] == 7
    assert error["conflicting_fields"]["title"]["current"] == "Sprint 12 planning (rescheduled)"


async def test_update_meeting_requires_something_to_change(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    result = await mcp_client.call_tool(
        "update_meeting", {"meeting_id": MEETING_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "nothing to change" in error["message"]


async def test_update_meeting_on_a_closed_meeting_names_the_state_only_rule(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch(MEETING_PATH).mock(return_value=httpx.Response(422, json=MEETING_NOT_EDITABLE))

    result = await mcp_client.call_tool(
        "update_meeting",
        {"meeting_id": MEETING_ID, "title": "Too late", "lock_version": 3},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert "state-only patch" in error["hint"]


async def test_update_meeting_maps_a_pre_17_4_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch(MEETING_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "update_meeting",
        {"meeting_id": MEETING_ID, "title": "Sprint 12 planning (moved)", "lock_version": 3},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "Meetings module is not installed" in error["hint"]
    assert "before 17.4" in error["hint"]
    assert "meetings write API" in error["hint"]


async def test_update_meeting_maps_a_405_onto_the_same_version_reading(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """16.x routes GET /meetings/{id} but not PATCH: Grape answers 405, not 404."""
    mock_api.patch(MEETING_PATH).mock(return_value=httpx.Response(405, text="405 Not Allowed"))

    result = await mcp_client.call_tool(
        "update_meeting",
        {"meeting_id": MEETING_ID, "title": "Sprint 12 planning (moved)", "lock_version": 3},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "unexpected_response"
    assert error["http_status"] == 405
    assert "predates the Meetings write API" in error["hint"]
    assert "before 17.4" in error["hint"]


# --- delete_meeting -------------------------------------------------------


async def test_delete_meeting_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(MEETING_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_meeting", {"meeting_id": MEETING_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    assert route.call_count == 0


async def test_delete_meeting_deletes_and_confirms(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(MEETING_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_meeting", {"meeting_id": MEETING_ID, "confirm": True}
    )

    assert route.call_count == 1
    assert result.structured_content is not None
    outcome = result.structured_content
    assert outcome["id"] == MEETING_ID
    assert outcome["deleted"] is True
    assert "permanently" in outcome["message"]


async def test_delete_meeting_maps_a_pre_17_4_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(MEETING_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "delete_meeting", {"meeting_id": MEETING_ID, "confirm": True}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "meetings write API" in error["hint"]


# --- update_meeting_agenda_item -------------------------------------------


async def test_update_agenda_item_patches_the_wire_keys_but_never_item_type(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    patch = mock_api.patch(AGENDA_ITEM_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_AGENDA_ITEM)
    )

    result = await mcp_client.call_tool(
        "update_meeting_agenda_item",
        {
            "agenda_item_id": 91,
            "title": "Capacity check (final)",
            "notes": "Final numbers.",
            "duration_minutes": 20,
            "position": 2,
            "presenter_id": 3,
            "work_package_id": WORK_PACKAGE_ID,
            "lock_version": 0,
        },
    )

    sent = body_of(patch)
    # itemType is create-only upstream (422 PropertyIsReadOnly), so the full
    # body equality doubles as the "never forwarded" assertion.
    assert sent == {
        "title": "Capacity check (final)",
        "notes": {"format": "markdown", "raw": "Final numbers."},
        "durationInMinutes": 20,
        "position": 2,
        "lockVersion": 0,
        "_links": {
            "presenter": {"href": "/api/v3/users/3"},
            "workPackage": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}"},
        },
    }

    assert result.structured_content is not None
    item = result.structured_content
    assert item["id"] == 91
    assert item["title"] == "Capacity check (final)"
    assert item["duration_minutes"] == 20
    assert item["position"] == 2
    assert item["lock_version"] == 1
    assert item["meeting"] == {"id": MEETING_ID, "name": "Sprint 12 planning"}


async def test_update_agenda_item_with_a_stale_lock_version_conflicts(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch(AGENDA_ITEM_PATH).mock(
        return_value=httpx.Response(409, json=STALE_LOCK_CONFLICT)
    )
    mock_api.get(AGENDA_ITEM_PATH).mock(
        return_value=httpx.Response(200, json={**AGENDA_ITEM_SIMPLE, "lockVersion": 5})
    )

    result = await mcp_client.call_tool(
        "update_meeting_agenda_item",
        {"agenda_item_id": 91, "duration_minutes": 20, "lock_version": 0},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "conflict"
    assert error["lock_version"] == 5


async def test_update_agenda_item_requires_something_to_change(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    result = await mcp_client.call_tool(
        "update_meeting_agenda_item", {"agenda_item_id": 91}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "nothing to change" in error["message"]


async def test_update_agenda_item_on_a_closed_meeting_explains_the_freeze(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch(AGENDA_ITEM_PATH).mock(
        return_value=httpx.Response(422, json=MEETING_NOT_EDITABLE)
    )

    result = await mcp_client.call_tool(
        "update_meeting_agenda_item",
        {"agenda_item_id": 91, "duration_minutes": 20, "lock_version": 0},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert "closed" in error["hint"]


async def test_update_agenda_item_maps_a_pre_17_6_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch(AGENDA_ITEM_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "update_meeting_agenda_item",
        {"agenda_item_id": 91, "duration_minutes": 20, "lock_version": 0},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "Meetings module is not installed" in error["hint"]
    assert "17.6" in error["hint"]


# --- delete_meeting_agenda_item -------------------------------------------


async def test_delete_agenda_item_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(AGENDA_ITEM_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_meeting_agenda_item", {"agenda_item_id": 91}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert route.call_count == 0


async def test_delete_agenda_item_deletes_and_confirms(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(AGENDA_ITEM_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_meeting_agenda_item", {"agenda_item_id": 91, "confirm": True}
    )

    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["id"] == 91
    assert result.structured_content["deleted"] is True


async def test_delete_agenda_item_maps_a_pre_17_6_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(AGENDA_ITEM_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "delete_meeting_agenda_item",
        {"agenda_item_id": 91, "confirm": True},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "17.6" in error["hint"]


# --- add_meeting_outcome --------------------------------------------------


async def test_add_outcome_posts_the_wire_keys_including_the_agenda_item_link(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("meeting_outcomes").mock(
        return_value=httpx.Response(201, json=CREATED_OUTCOME)
    )

    result = await mcp_client.call_tool(
        "add_meeting_outcome",
        {
            "agenda_item_id": 92,
            "kind": "decision",
            "notes": "Ship on Friday.",
            "work_package_id": WORK_PACKAGE_ID,
        },
    )

    sent = body_of(route)
    # The association is spelled "agendaItem" on the wire, and the write route
    # is top-level — not nested under the meeting (SPEC §6.13).
    assert sent == {
        "kind": "decision",
        "notes": {"format": "markdown", "raw": "Ship on Friday."},
        "_links": {
            "agendaItem": {"href": "/api/v3/meeting_agenda_items/92"},
            "workPackage": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}"},
        },
    }

    assert result.structured_content is not None
    outcome = result.structured_content
    assert outcome["id"] == CREATED_OUTCOME_ID
    assert outcome["kind"] == "decision"
    assert outcome["notes"] == "Ship on Friday."
    assert outcome["author"] == {"id": 3, "name": "Grace Hopper"}
    assert outcome["work_package"] == {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"}
    assert outcome["agenda_item"] == {"id": 92, "name": None}


async def test_an_information_outcome_requires_notes_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("meeting_outcomes").mock(
        return_value=httpx.Response(201, json=CREATED_OUTCOME)
    )

    result = await mcp_client.call_tool(
        "add_meeting_outcome", {"agenda_item_id": 92}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "notes is required" in error["message"]
    assert route.call_count == 0


async def test_a_work_package_outcome_requires_the_work_package_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("meeting_outcomes").mock(
        return_value=httpx.Response(201, json=CREATED_OUTCOME)
    )

    result = await mcp_client.call_tool(
        "add_meeting_outcome",
        {"agenda_item_id": 92, "kind": "work_package"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "work_package_id is required" in error["message"]
    assert route.call_count == 0


async def test_an_outcome_against_a_meeting_not_in_progress_explains_the_timing_rule(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("meeting_outcomes").mock(
        return_value=httpx.Response(422, json=OUTCOME_NOT_EDITABLE)
    )

    result = await mcp_client.call_tool(
        "add_meeting_outcome",
        {"agenda_item_id": 92, "kind": "decision", "notes": "Ship on Friday."},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert "'in_progress'" in error["hint"]
    assert "update_meeting" in error["hint"]


async def test_a_forbidden_outcome_write_names_the_manage_outcomes_permission(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("meeting_outcomes").mock(return_value=httpx.Response(403, json=MODULE_FORBIDDEN))

    result = await mcp_client.call_tool(
        "add_meeting_outcome",
        {"agenda_item_id": 92, "kind": "decision", "notes": "Ship on Friday."},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "permission_denied"
    assert "'manage outcomes'" in error["hint"]
    assert "list_permissions" in error["hint"]


async def test_add_outcome_maps_a_pre_17_6_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("meeting_outcomes").mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "add_meeting_outcome",
        {"agenda_item_id": 92, "kind": "decision", "notes": "Ship on Friday."},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "before 17.6 has no outcomes API" in error["hint"]


# --- update_meeting_outcome -----------------------------------------------


async def test_update_outcome_patches_plainly_because_outcomes_have_no_lock_version(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    patch = mock_api.patch(OUTCOME_PATH).mock(
        return_value=httpx.Response(200, json=UPDATED_OUTCOME)
    )

    result = await mcp_client.call_tool(
        "update_meeting_outcome",
        {"outcome_id": OUTCOME_ID, "kind": "information", "notes": "Deferred to next sprint."},
    )

    sent = body_of(patch)
    assert sent == {
        "kind": "information",
        "notes": {"format": "markdown", "raw": "Deferred to next sprint."},
    }
    assert "lockVersion" not in sent

    assert result.structured_content is not None
    outcome = result.structured_content
    assert outcome["id"] == OUTCOME_ID
    assert outcome["kind"] == "information"
    assert outcome["notes"] == "Deferred to next sprint."
    assert outcome["agenda_item"] == {"id": 92, "name": None}


async def test_update_outcome_requires_something_to_change(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    result = await mcp_client.call_tool(
        "update_meeting_outcome", {"outcome_id": OUTCOME_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "nothing to change" in error["message"]


async def test_update_outcome_after_the_meeting_closed_explains_the_timing_rule(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch(OUTCOME_PATH).mock(return_value=httpx.Response(422, json=OUTCOME_NOT_EDITABLE))

    result = await mcp_client.call_tool(
        "update_meeting_outcome",
        {"outcome_id": OUTCOME_ID, "notes": "Too late."},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert "'in_progress'" in error["hint"]


# --- delete_meeting_outcome -----------------------------------------------


async def test_delete_outcome_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(OUTCOME_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_meeting_outcome", {"outcome_id": OUTCOME_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert route.call_count == 0


async def test_delete_outcome_deletes_and_confirms(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(OUTCOME_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool(
        "delete_meeting_outcome", {"outcome_id": OUTCOME_ID, "confirm": True}
    )

    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["id"] == OUTCOME_ID
    assert result.structured_content["deleted"] is True


async def test_delete_outcome_after_the_meeting_closed_explains_the_timing_rule(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(OUTCOME_PATH).mock(return_value=httpx.Response(422, json=OUTCOME_NOT_EDITABLE))

    result = await mcp_client.call_tool(
        "delete_meeting_outcome",
        {"outcome_id": OUTCOME_ID, "confirm": True},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert "'in_progress'" in error["hint"]


async def test_delete_outcome_maps_a_pre_17_6_404_onto_the_version_hint(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(OUTCOME_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "delete_meeting_outcome",
        {"outcome_id": OUTCOME_ID, "confirm": True},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "before 17.6 has no outcomes API" in error["hint"]


# --- get_wiki_page --------------------------------------------------------


async def test_get_wiki_page_returns_identity_and_says_content_is_missing(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(f"wiki_pages/{WIKI_PAGE_ID}").mock(
        return_value=httpx.Response(200, json=WIKI_PAGE)
    )

    result = await mcp_client.call_tool("get_wiki_page", {"wiki_page_id": WIKI_PAGE_ID})

    assert route.call_count == 1
    assert result.structured_content is not None
    page = result.structured_content
    assert page["id"] == WIKI_PAGE_ID
    assert page["title"] == "Deployment runbook"
    assert page["project"] == {"id": PROJECT_ID, "name": "Apollo migration"}
    assert len(page["notes"]) == 1
    assert "content is not available" in page["notes"][0]
    assert "list_attachments(container_type='wiki_page'" in page["notes"][0]


async def test_an_unknown_wiki_page_says_where_ids_come_from(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("wiki_pages/999").mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool(
        "get_wiki_page", {"wiki_page_id": 999}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "Wiki module is not installed" in error["hint"]
    assert "no wiki index or search" in error["hint"]


# --- documents ------------------------------------------------------------


async def test_list_documents_pages_and_projects_the_rows(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("documents").mock(
        return_value=httpx.Response(200, json=DOCUMENT_COLLECTION)
    )

    result = await mcp_client.call_tool("list_documents", {"page": 1, "page_size": 20})

    params = route.calls[0].request.url.params
    assert params["offset"] == "1"
    assert params["pageSize"] == "20"
    assert result.structured_content is not None
    content = result.structured_content
    assert content["pagination"] == {"total": 45, "page": 1, "page_size": 20, "has_more": True}
    assert content["items"][0] == {
        "id": DOCUMENT_ID,
        "title": "Architecture decision record 4",
        "project": {"id": PROJECT_ID, "name": "Apollo migration"},
        "created_at": "2026-05-04T08:00:00Z",
        "updated_at": "2026-06-11T16:20:00Z",
    }
    # The list stays compact: the description belongs to get_document.
    assert "description" not in content["items"][0]
    assert content["items"][1]["project"] == {"id": 3, "name": "Customer work"}


async def test_a_missing_documents_module_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("documents").mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool("list_documents", {})

    assert result.is_error is False
    assert result.structured_content is not None
    content = result.structured_content
    assert content["items"] == []
    assert content["pagination"]["total"] == 0
    note = content["notes"][0]
    assert note.startswith("unavailable (module not installed)")
    assert "Documents module is not installed" in note


async def test_a_forbidden_documents_module_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("documents").mock(return_value=httpx.Response(403, json=MODULE_FORBIDDEN))

    result = await mcp_client.call_tool("list_documents", {})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["items"] == []
    note = result.structured_content["notes"][0]
    assert note.startswith("no permission (403)")
    assert "'view documents'" in note


async def test_get_document_adds_the_description(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"documents/{DOCUMENT_ID}").mock(return_value=httpx.Response(200, json=DOCUMENT))

    result = await mcp_client.call_tool("get_document", {"document_id": DOCUMENT_ID})

    assert result.structured_content is not None
    assert result.structured_content["description"] == "We keep HAL parsing in one module."
    assert result.structured_content["title"] == "Architecture decision record 4"
    assert result.structured_content["project"] == {"id": PROJECT_ID, "name": "Apollo migration"}


async def test_a_forbidden_document_names_the_permission(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"documents/{DOCUMENT_ID}").mock(
        return_value=httpx.Response(403, json=MODULE_FORBIDDEN)
    )

    result = await mcp_client.call_tool(
        "get_document", {"document_id": DOCUMENT_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "permission_denied"
    assert "'view documents'" in error["hint"]


# --- budgets --------------------------------------------------------------


async def test_list_budgets_is_fetched_in_full(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(BUDGETS_PATH).mock(
        return_value=httpx.Response(200, json=BUDGET_COLLECTION)
    )

    result = await mcp_client.call_tool("list_budgets", {"project_id": PROJECT_ID})

    assert route.call_count == 1
    assert result.structured_content is not None
    content = result.structured_content
    assert content["items"] == [
        {"id": 4, "subject": "2026 platform budget"},
        {"id": 5, "subject": "Hardware refresh"},
    ]
    assert content["pagination"] == {"total": 2, "page": 1, "page_size": 2, "has_more": False}


async def test_list_budgets_accepts_a_project_identifier(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(f"projects/{PROJECT_IDENTIFIER}/budgets").mock(
        return_value=httpx.Response(200, json=hal_collection([]))
    )

    result = await mcp_client.call_tool("list_budgets", {"project_id": PROJECT_IDENTIFIER})

    assert route.call_count == 1
    assert result.structured_content is not None
    assert result.structured_content["items"] == []
    assert result.structured_content["pagination"]["has_more"] is False


async def test_a_missing_budgets_module_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(BUDGETS_PATH).mock(return_value=httpx.Response(404, json=MODULE_NOT_FOUND))

    result = await mcp_client.call_tool("list_budgets", {"project_id": PROJECT_ID})

    assert result.is_error is False
    assert result.structured_content is not None
    content = result.structured_content
    assert content["items"] == []
    assert content["pagination"]["total"] == 0
    note = content["notes"][0]
    assert note.startswith("unavailable (module not installed)")
    assert "Budgets module is not installed" in note
    assert f"project {PROJECT_ID} does not exist" in note


async def test_a_forbidden_budget_listing_names_the_permission_in_notes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(BUDGETS_PATH).mock(return_value=httpx.Response(403, json=MODULE_FORBIDDEN))

    result = await mcp_client.call_tool("list_budgets", {"project_id": PROJECT_ID})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["items"] == []
    note = result.structured_content["notes"][0]
    assert note.startswith("no permission (403)")
    assert "'view budgets'" in note
