"""Protocol tests for the Phase 3 project/query/file-link tools (SPEC §6.6, §6.7, §6.4).

Everything runs through the in-memory FastMCP client against a respx-mocked
instance, so these assert what a model actually receives: the copy form's
``_meta`` defaults surviving into the commit, a copy that reports a *job* and
never a finished project (G3), the 17.x gate on favorites refusing a
known-too-old instance but not a silent one (G2/G5), the saved query committed
in the shape the form re-rendered, and the module-missing degradation of
``list_file_links`` (G5).

The payloads come from ``tests.fixtures.project_ops_payloads``, which mirrors the
upstream representers rather than the tools, so a key-name typo fails here.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client, FastMCP

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import API_BASE, TEST_URL
from tests.fixtures.hal_payloads import API_ROOT
from tests.fixtures.project_ops_payloads import (
    COPY_ACCEPTED_WITHOUT_JOB,
    COPY_FORM,
    COPY_FORM_NAME_TAKEN,
    COPY_IDENTIFIER,
    COPY_JOB_ACCEPTED,
    COPY_JOB_ID,
    COPY_NAME,
    COPY_PROJECT_NUMERIC_ID,
    CREATED_QUERY,
    CREATED_QUERY_ONE_FILTER,
    FILE_LINK,
    FILE_LINK_NO_PERMISSION,
    FILE_LINK_UNKNOWN_STATUS,
    FILE_LINK_WORK_PACKAGE_ID,
    FORBIDDEN_ERROR,
    JOB_FAILURE,
    JOB_IN_PROCESS,
    JOB_SUCCESS,
    JOB_SUCCESS_URL_ONLY,
    NOT_FOUND_ERROR,
    QUERY_FORM,
    QUERY_FORM_INVALID_FILTER,
    QUERY_ID,
    QUERY_NAME,
    QUERY_STAR_FORBIDDEN,
    SOURCE_PROJECT_ID,
    STARRED_QUERY,
    file_link_collection,
)

COPY_PATH = f"projects/{SOURCE_PROJECT_ID}/copy"
FAVORITE_PATH = f"projects/{SOURCE_PROJECT_ID}/favorite"
JOB_PATH = f"job_statuses/{COPY_JOB_ID}"
FILE_LINKS_PATH = f"work_packages/{FILE_LINK_WORK_PACKAGE_ID}/file_links"

SAVE_FILTERS: list[dict[str, Any]] = [
    {"name": "status", "operator": "o", "values": []},
    {"name": "assignee", "operator": "=", "values": ["12"]},
]


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def body_of(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


def route_probe(mock_api: respx.MockRouter, core_version: str | None = "17.7.1") -> None:
    """Route the two calls ``ctx.probe()`` makes (SPEC §4.7).

    ``core_version=None`` models an instance that reports no version at all —
    the API root simply omits the key.

    The API root has to be routed by full URL: with a router base_url, a
    relative pattern of ``""`` matches every path.
    """
    root = {key: value for key, value in API_ROOT.items() if key != "coreVersion"}
    if core_version is not None:
        root["coreVersion"] = core_version
    mock_api.get(url=f"{API_BASE}/").mock(return_value=httpx.Response(200, json=root))
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json={"total": 0}))


def server_for(core_version: str | None, mock_api: respx.MockRouter) -> FastMCP:
    """A server whose probe reports ``core_version`` (fresh cache per server)."""
    route_probe(mock_api, core_version)
    return build_server(
        Settings(_env_file=None, url=TEST_URL, api_key="test-token")  # type: ignore[call-arg]
    )


# --- registration ---------------------------------------------------------


async def test_the_five_phase_three_tools_are_registered_with_honest_annotations(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    copying = tools["copy_project"]
    assert copying.outputSchema is not None
    assert copying.annotations is not None
    assert copying.annotations.readOnlyHint is False
    assert copying.annotations.destructiveHint is False
    assert set(copying.inputSchema["properties"]) == {
        "id_or_identifier",
        "new_name",
        "include_work_packages",
        "notify",
    }
    assert set((copying.meta or {})["fastmcp"]["tags"]) == {"projects", "write"}

    job = tools["get_job_status"]
    assert job.annotations is not None
    assert job.annotations.readOnlyHint is True
    assert set(job.inputSchema["properties"]) == {"job_id"}
    assert set((job.meta or {})["fastmcp"]["tags"]) == {"projects", "read"}

    favorite = tools["set_project_favorite"]
    assert favorite.annotations is not None
    assert favorite.annotations.readOnlyHint is False
    assert favorite.annotations.idempotentHint is True
    assert set(favorite.inputSchema["properties"]) == {"id_or_identifier", "favorite"}
    assert set(favorite.inputSchema["required"]) == {"id_or_identifier", "favorite"}

    saving = tools["save_query"]
    assert saving.annotations is not None
    assert saving.annotations.readOnlyHint is False
    assert set(saving.inputSchema["properties"]) == {
        "name",
        "filters",
        "project_id",
        "public",
        "star",
        "sort_by",
        "group_by",
    }
    assert set(saving.inputSchema["required"]) == {"name", "filters"}
    assert set((saving.meta or {})["fastmcp"]["tags"]) == {"queries", "write"}

    links = tools["list_file_links"]
    assert links.outputSchema is not None
    assert links.annotations is not None
    assert links.annotations.readOnlyHint is True
    assert set(links.inputSchema["properties"]) == {"work_package_id"}
    assert set((links.meta or {})["fastmcp"]["tags"]) == {"attachments", "read"}


async def test_descriptions_carry_the_phase_three_warnings(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    copying = tools["copy_project"].description or ""
    assert "ASYNCHRONOUS" in copying
    assert "get_job_status" in copying
    assert "NEVER" in copying

    favorite = tools["set_project_favorite"].description or ""
    assert "OpenProject 17" in favorite
    assert "per user" in favorite

    saving = tools["save_query"].description or ""
    assert "run_query" in saving
    assert "OpenProject UI" in saving

    links = tools["list_file_links"].description or ""
    assert "list_attachments" in links
    assert "notes" in links


# --- copy_project ---------------------------------------------------------


async def test_copy_asks_the_form_first_and_keeps_the_instance_copy_flags(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{COPY_PATH}/form").mock(return_value=httpx.Response(200, json=COPY_FORM))
    copy = mock_api.post(COPY_PATH).mock(return_value=httpx.Response(201, json=COPY_JOB_ACCEPTED))

    result = await mcp_client.call_tool(
        "copy_project",
        {
            "id_or_identifier": SOURCE_PROJECT_ID,
            "new_name": COPY_NAME,
            "include_work_packages": False,
        },
    )

    assert form.call_count == 1
    assert copy.call_count == 1

    sent = body_of(form)
    assert sent["name"] == COPY_NAME
    assert sent["_meta"] == {"copyWorkPackages": False, "sendNotifications": False}

    committed = body_of(copy)
    # The identifier the form derived is carried into the commit…
    assert committed["identifier"] == COPY_IDENTIFIER
    # …and so is every copy flag the instance defaulted, with ours on top.
    assert committed["_meta"] == {
        "copyMembers": True,
        "copyVersions": True,
        "copyWiki": True,
        "copyWorkPackages": False,
        "sendNotifications": False,
    }

    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["scheduled"] is True
    assert payload["job_id"] == COPY_JOB_ID
    assert payload["status"] == "in_queue"
    assert payload["message"] == "Project copy scheduled."
    assert payload["source"] == SOURCE_PROJECT_ID
    assert payload["new_name"] == COPY_NAME
    # G3: nothing pretends the copy exists yet.
    assert payload["project"] is None
    assert payload["notes"] == [
        "The copy runs as a background job: this is the job's INITIAL state, not a finished "
        "copy. Poll get_job_status(job_id=...) until status is 'success' or 'failure' before "
        "telling the user the new project exists."
    ]


async def test_copy_defaults_to_including_work_packages_and_no_notifications(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{COPY_PATH}/form").mock(return_value=httpx.Response(200, json=COPY_FORM))
    mock_api.post(COPY_PATH).mock(return_value=httpx.Response(201, json=COPY_JOB_ACCEPTED))

    await mcp_client.call_tool(
        "copy_project", {"id_or_identifier": SOURCE_PROJECT_ID, "new_name": COPY_NAME}
    )
    assert body_of(form)["_meta"] == {"copyWorkPackages": True, "sendNotifications": False}


async def test_a_copy_without_a_job_id_says_how_to_check_instead(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(f"{COPY_PATH}/form").mock(return_value=httpx.Response(200, json=COPY_FORM))
    mock_api.post(COPY_PATH).mock(return_value=httpx.Response(201, json=COPY_ACCEPTED_WITHOUT_JOB))

    result = await mcp_client.call_tool(
        "copy_project", {"id_or_identifier": SOURCE_PROJECT_ID, "new_name": COPY_NAME}
    )
    assert result.structured_content is not None
    assert result.structured_content["job_id"] is None
    assert any("list_projects" in note for note in result.structured_content["notes"])


async def test_copy_surfaces_form_validation_errors_and_never_commits(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post(f"{COPY_PATH}/form").mock(
        return_value=httpx.Response(200, json=COPY_FORM_NAME_TAKEN)
    )
    copy = mock_api.post(COPY_PATH).mock(return_value=httpx.Response(201, json=COPY_JOB_ACCEPTED))

    result = await mcp_client.call_tool(
        "copy_project",
        {"id_or_identifier": SOURCE_PROJECT_ID, "new_name": COPY_NAME},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "identifier", "message": "Identifier has already been taken."}
    ]
    assert "unique across the instance" in error["hint"]
    assert copy.call_count == 0


async def test_copying_an_unknown_project_reports_not_found(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("projects/ghost-project/copy/form").mock(
        return_value=httpx.Response(404, json=NOT_FOUND_ERROR)
    )
    result = await mcp_client.call_tool(
        "copy_project",
        {"id_or_identifier": "ghost-project", "new_name": COPY_NAME},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "URL slug" in error["hint"]


async def test_an_empty_copy_name_never_reaches_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post(f"{COPY_PATH}/form").mock(return_value=httpx.Response(200, json=COPY_FORM))
    result = await mcp_client.call_tool(
        "copy_project",
        {"id_or_identifier": SOURCE_PROJECT_ID, "new_name": "   "},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "new_name" in error["message"]
    assert form.call_count == 0


# --- get_job_status -------------------------------------------------------


async def test_a_running_job_is_never_reported_as_finished(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(JOB_PATH).mock(return_value=httpx.Response(200, json=JOB_IN_PROCESS))

    result = await mcp_client.call_tool("get_job_status", {"job_id": COPY_JOB_ID})
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["id"] == COPY_JOB_ID
    assert payload["status"] == "in_process"
    assert payload["finished"] is False
    assert payload["successful"] is None
    assert payload["message"] == "Copying work packages (140/900)."
    assert payload["project"] is None
    assert any("still in_process" in note for note in payload["notes"])


async def test_a_finished_copy_job_hands_back_the_new_project(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(JOB_PATH).mock(return_value=httpx.Response(200, json=JOB_SUCCESS))

    result = await mcp_client.call_tool("get_job_status", {"job_id": COPY_JOB_ID})
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["status"] == "success"
    assert payload["finished"] is True
    assert payload["successful"] is True
    # The link nested in the job's payload wins: numeric id and the real name,
    # not the slug the redirect URL happens to carry.
    assert payload["project"] == {"id": COPY_PROJECT_NUMERIC_ID, "name": COPY_NAME}
    assert payload["result_url"] == f"https://openproject.test/projects/{COPY_IDENTIFIER}"
    assert payload["notes"] == []


async def test_a_job_with_only_a_redirect_derives_the_project_and_says_so(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(JOB_PATH).mock(return_value=httpx.Response(200, json=JOB_SUCCESS_URL_ONLY))

    result = await mcp_client.call_tool("get_job_status", {"job_id": COPY_JOB_ID})
    assert result.structured_content is not None
    payload = result.structured_content

    # No link anywhere, so the identifier comes out of the URL — and no name is
    # invented for it.
    assert payload["project"] == {"id": COPY_IDENTIFIER, "name": None}
    assert any("derived from the URL" in note for note in payload["notes"])


async def test_a_failed_job_is_reported_as_failed_with_its_message(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(JOB_PATH).mock(return_value=httpx.Response(200, json=JOB_FAILURE))

    result = await mcp_client.call_tool("get_job_status", {"job_id": COPY_JOB_ID})
    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["finished"] is True
    assert payload["successful"] is False
    assert payload["message"] == "Copying failed: Identifier has already been taken."


async def test_an_unknown_job_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("job_statuses/nope").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    result = await mcp_client.call_tool("get_job_status", {"job_id": "nope"}, raise_on_error=False)
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "copy_project" in error["hint"]
    assert "discards" in error["hint"]


# --- set_project_favorite -------------------------------------------------


async def test_favorites_are_refused_below_openproject_17(mock_api: respx.MockRouter) -> None:
    server = server_for("16.6.1", mock_api)
    route = mock_api.post(FAVORITE_PATH).mock(return_value=httpx.Response(204))

    async with Client(server) as client:
        result = await client.call_tool(
            "set_project_favorite",
            {"id_or_identifier": SOURCE_PROJECT_ID, "favorite": True},
            raise_on_error=False,
        )

    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "16.6.1" in error["message"]
    assert "OpenProject 17" in error["message"]
    assert "web UI" in error["hint"]
    assert route.call_count == 0, "nothing may be sent to an instance without the endpoint"


async def test_a_version_silent_instance_is_still_asked(mock_api: respx.MockRouter) -> None:
    """An unreported version is not evidence the endpoint is missing (G5)."""
    server = server_for(None, mock_api)
    route = mock_api.post(FAVORITE_PATH).mock(return_value=httpx.Response(204))

    async with Client(server) as client:
        result = await client.call_tool(
            "set_project_favorite",
            {"id_or_identifier": SOURCE_PROJECT_ID, "favorite": True},
        )

    assert route.call_count == 1, "a silent instance must be asked, not assumed to be old"
    assert result.structured_content is not None
    assert result.structured_content["favorite"] is True


async def test_favoriting_posts_and_unfavoriting_deletes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_probe(mock_api)
    post = mock_api.post(FAVORITE_PATH).mock(return_value=httpx.Response(204))
    delete = mock_api.delete(FAVORITE_PATH).mock(return_value=httpx.Response(204))

    added = await mcp_client.call_tool(
        "set_project_favorite", {"id_or_identifier": SOURCE_PROJECT_ID, "favorite": True}
    )
    assert added.structured_content is not None
    assert added.structured_content == {
        "id": SOURCE_PROJECT_ID,
        "favorite": True,
        "message": f"Project '{SOURCE_PROJECT_ID}' is now a favorite of the authenticated user.",
    }
    assert post.call_count == 1
    # json={} keeps the Content-Type header that OpenProject requires on POST
    # (406 "Missing content-type header" otherwise); DELETE stays bodyless.
    assert post.calls[0].request.headers["content-type"] == "application/json"
    assert post.calls[0].request.content == b"{}"

    removed = await mcp_client.call_tool(
        "set_project_favorite", {"id_or_identifier": SOURCE_PROJECT_ID, "favorite": False}
    )
    assert removed.structured_content is not None
    assert removed.structured_content["favorite"] is False
    assert "no longer a favorite" in removed.structured_content["message"]
    assert delete.call_count == 1
    assert delete.calls[0].request.content == b"", "the unfavorite DELETE stays bodyless"


async def test_a_missing_favorite_endpoint_is_reported_as_unavailable(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route_probe(mock_api)
    mock_api.post(FAVORITE_PATH).mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    result = await mcp_client.call_tool(
        "set_project_favorite",
        {"id_or_identifier": SOURCE_PROJECT_ID, "favorite": True},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert "does not expose" in error["hint"]
    assert "17.7.1" in error["hint"]
    assert "Nothing was changed" in error["hint"]


# --- save_query -----------------------------------------------------------


async def test_save_query_asks_the_form_then_commits_the_forms_own_filters(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("queries/form").mock(return_value=httpx.Response(200, json=QUERY_FORM))
    create = mock_api.post("queries").mock(return_value=httpx.Response(201, json=CREATED_QUERY))

    result = await mcp_client.call_tool(
        "save_query",
        {
            "name": QUERY_NAME,
            "filters": SAVE_FILTERS,
            "project_id": 5,
            "sort_by": [["due_date", "asc"]],
            "group_by": "status",
        },
    )

    assert form.call_count == 1
    sent = body_of(form)
    assert sent["name"] == QUERY_NAME
    assert sent["public"] is False
    # Filters are written as links to the filter and the operator; the operator
    # is percent-encoded because '=' is part of a URL path here. status and
    # assignee are object-backed filters, so their values are hrefs inside
    # _links — a plain array is a parse failure upstream, not a 422.
    assert sent["filters"] == [
        {
            "_links": {
                "filter": {"href": "/api/v3/queries/filters/status"},
                "operator": {"href": "/api/v3/queries/operators/o"},
                "values": [],
            }
        },
        {
            "_links": {
                "filter": {"href": "/api/v3/queries/filters/assignee"},
                "operator": {"href": "/api/v3/queries/operators/%3D"},
                "values": [{"href": "/api/v3/principals/12"}],
            }
        },
    ]
    assert "values" not in sent["filters"][1], "an object-backed filter sends no plain values"
    assert sent["_links"]["project"] == {"href": "/api/v3/projects/5"}
    assert sent["_links"]["groupBy"] == {"href": "/api/v3/queries/group_bys/status"}
    assert sent["_links"]["sortBy"] == [{"href": "/api/v3/queries/sort_bys/dueDate-asc"}]

    committed = body_of(create)
    # The form re-rendered the filters (assignee values as hrefs); that spelling
    # is what gets committed, not ours.
    assert committed["filters"] == QUERY_FORM["_embedded"]["payload"]["filters"]
    assert committed["name"] == QUERY_NAME
    assert "self" not in committed["_links"]

    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["id"] == QUERY_ID
    assert payload["name"] == QUERY_NAME
    assert payload["project"] == {"id": 5, "name": "Platform"}
    assert payload["public"] is False
    assert payload["starred"] is False
    assert payload["filters"] == ["Status open", "Assignee is (OR) Grace Hopper"]
    assert payload["group_by"] == "Status"
    assert payload["sort_by"] == ["Finish date asc"]
    assert payload["notes"] == []


async def test_a_non_resource_filter_keeps_its_plain_values(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """dueDate is not object-backed, so its values stay a plain array."""
    form = mock_api.post("queries/form").mock(return_value=httpx.Response(200, json=QUERY_FORM))
    mock_api.post("queries").mock(return_value=httpx.Response(201, json=CREATED_QUERY))

    await mcp_client.call_tool(
        "save_query",
        {
            "name": QUERY_NAME,
            "filters": [{"name": "dueDate", "operator": "<>d", "values": ["2026-01-01", ""]}],
        },
    )
    assert body_of(form)["filters"] == [
        {
            "_links": {
                "filter": {"href": "/api/v3/queries/filters/dueDate"},
                "operator": {"href": "/api/v3/queries/operators/%3C%3Ed"},
            },
            "values": ["2026-01-01", ""],
        }
    ]


async def test_save_query_stars_the_view_in_a_second_call(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("queries/form").mock(return_value=httpx.Response(200, json=QUERY_FORM))
    mock_api.post("queries").mock(return_value=httpx.Response(201, json=CREATED_QUERY))
    # Starring is routed as PATCH upstream; a POST would not reach the endpoint.
    star = mock_api.patch(f"queries/{QUERY_ID}/star").mock(
        return_value=httpx.Response(200, json=STARRED_QUERY)
    )

    result = await mcp_client.call_tool(
        "save_query", {"name": QUERY_NAME, "filters": SAVE_FILTERS, "star": True}
    )
    assert star.call_count == 1
    # The empty JSON body is what makes httpx send a Content-Type header;
    # without one OpenProject answers 406 before the star endpoint runs.
    star_request = star.calls[0].request
    assert star_request.headers["content-type"] == "application/json"
    assert star_request.content == b"{}"
    assert result.structured_content is not None
    assert result.structured_content["starred"] is True
    assert result.structured_content["notes"] == []


async def test_a_failed_star_keeps_the_saved_query_and_explains_itself(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("queries/form").mock(return_value=httpx.Response(200, json=QUERY_FORM))
    mock_api.post("queries").mock(return_value=httpx.Response(201, json=CREATED_QUERY))
    mock_api.patch(f"queries/{QUERY_ID}/star").mock(
        return_value=httpx.Response(403, json=QUERY_STAR_FORBIDDEN)
    )

    result = await mcp_client.call_tool(
        "save_query", {"name": QUERY_NAME, "filters": SAVE_FILTERS, "star": True}
    )
    assert result.structured_content is not None
    payload = result.structured_content
    # The query exists: losing its id because of the star would be worse.
    assert payload["id"] == QUERY_ID
    assert payload["starred"] is False
    assert any("starring it failed" in note for note in payload["notes"])


async def test_filters_the_instance_did_not_keep_are_reported(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("queries/form").mock(return_value=httpx.Response(200, json=QUERY_FORM))
    mock_api.post("queries").mock(return_value=httpx.Response(201, json=CREATED_QUERY_ONE_FILTER))

    result = await mcp_client.call_tool("save_query", {"name": QUERY_NAME, "filters": SAVE_FILTERS})
    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["filters"] == ["Status open"]
    assert any("2 filters were sent" in note for note in payload["notes"])


async def test_an_impossible_filter_operator_never_reaches_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("queries/form").mock(return_value=httpx.Response(200, json=QUERY_FORM))
    result = await mcp_client.call_tool(
        "save_query",
        {"name": QUERY_NAME, "filters": [{"name": "subject", "operator": "o", "values": []}]},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "subject" in error["message"]
    assert "allowed operators" in error["hint"].lower()
    assert form.call_count == 0


async def test_an_unknown_sort_key_is_rejected_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    form = mock_api.post("queries/form").mock(return_value=httpx.Response(200, json=QUERY_FORM))
    result = await mcp_client.call_tool(
        "save_query",
        {"name": QUERY_NAME, "filters": [], "sort_by": [["deadline", "asc"]]},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "deadline" in error["message"]
    assert "due_date" in error["hint"]
    assert form.call_count == 0


async def test_save_query_surfaces_form_validation_errors_and_never_commits(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("queries/form").mock(
        return_value=httpx.Response(200, json=QUERY_FORM_INVALID_FILTER)
    )
    create = mock_api.post("queries").mock(return_value=httpx.Response(201, json=CREATED_QUERY))

    result = await mcp_client.call_tool(
        "save_query",
        {"name": QUERY_NAME, "filters": [{"name": "version", "operator": "=", "values": ["3"]}]},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "filters", "message": "Version filter does not exist."}
    ]
    assert "customField" in error["hint"]
    assert create.call_count == 0


# --- list_file_links ------------------------------------------------------


async def test_file_links_are_listed_with_absolute_openproject_urls(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(FILE_LINKS_PATH).mock(
        return_value=httpx.Response(
            200,
            json=file_link_collection(
                [FILE_LINK, FILE_LINK_NO_PERMISSION, FILE_LINK_UNKNOWN_STATUS]
            ),
        )
    )

    result = await mcp_client.call_tool(
        "list_file_links", {"work_package_id": FILE_LINK_WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["pagination"] == {"total": 3, "page": 1, "page_size": 3, "has_more": False}
    # The static hrefs are instance-relative API paths upstream; what a user can
    # click is the absolutized OpenProject endpoint, which then 303s to the
    # storage. The access state comes from the 'status' link, verbatim.
    assert payload["items"][0] == {
        "id": 601,
        "file_name": "architecture-review.pdf",
        "storage": {"id": 3, "name": "Acme Nextcloud"},
        "origin_id": "5503",
        "mime_type": "application/pdf",
        "open_url": f"{TEST_URL}/api/v3/file_links/601/open",
        "download_url": f"{TEST_URL}/api/v3/file_links/601/download",
        "permission": "View allowed",
        "creator": {"id": 12, "name": "Grace Hopper"},
        "created_at": "2026-07-20T08:15:00Z",
    }
    second = payload["items"][1]
    assert second["file_name"] == "budget.xlsx"
    assert second["permission"] == "View not allowed"
    assert second["open_url"] == f"{TEST_URL}/api/v3/file_links/602/open"
    assert second["download_url"] is None
    # No status link at all: null means "the storage did not say", and nothing
    # may fill that in.
    assert payload["items"][2]["permission"] is None
    assert payload["notes"] is None


async def test_a_missing_storages_module_degrades_to_a_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(FILE_LINKS_PATH).mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    result = await mcp_client.call_tool(
        "list_file_links", {"work_package_id": FILE_LINK_WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    payload = result.structured_content

    assert result.is_error is False
    assert payload["items"] == []
    assert payload["pagination"]["total"] == 0
    note = payload["notes"][0]
    assert "404" in note
    assert "storages module" in note
    assert "NOT proof" in note


async def test_forbidden_file_links_degrade_to_a_permission_note(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(FILE_LINKS_PATH).mock(return_value=httpx.Response(403, json=FORBIDDEN_ERROR))

    result = await mcp_client.call_tool(
        "list_file_links", {"work_package_id": FILE_LINK_WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    note = result.structured_content["notes"][0]
    assert "403" in note
    assert "permission" in note
    assert "NOT proof" in note


async def test_an_empty_file_link_list_never_claims_there_are_none(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """A caller without 'view file links' gets this same empty 200 (G5)."""
    mock_api.get(FILE_LINKS_PATH).mock(
        return_value=httpx.Response(200, json=file_link_collection([]))
    )

    result = await mcp_client.call_tool(
        "list_file_links", {"work_package_id": FILE_LINK_WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["items"] == []
    note = payload["notes"][0]
    assert "list_attachments" in note
    assert "not proof" in note
    assert "view file links" in note
    assert "no external files are linked" not in note
