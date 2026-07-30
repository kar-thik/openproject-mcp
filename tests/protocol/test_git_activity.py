"""Protocol tests for the git / development-activity tools (SPEC §6.5, §8, G5).

Every call goes through the in-memory FastMCP client against a respx-mocked
instance, so these assert what a model actually receives: the availability
verdict derived from the work package's links, the per-source degradation notes
that keep one dead module from failing the whole call, the internal-id /
provider-number distinction, and the structured error envelopes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.git_queries_payloads import (
    FORBIDDEN_ERROR,
    GITLAB_ISSUE_COLLECTION,
    MERGE_REQUEST_COLLECTION,
    NOT_FOUND_ERROR,
    PULL_REQUEST_COLLECTION,
    PULL_REQUEST_DETAIL,
    PULL_REQUEST_ID,
    REVISION_COLLECTION,
    WORK_PACKAGE_ALL_SOURCES,
    WORK_PACKAGE_GITHUB_ONLY,
    WORK_PACKAGE_ID,
    WORK_PACKAGE_NO_SOURCES,
    hal_collection,
)

WP_PATH = f"work_packages/{WORK_PACKAGE_ID}"
REVISIONS_PATH = f"{WP_PATH}/revisions"
PULLS_PATH = f"{WP_PATH}/github_pull_requests"
MERGES_PATH = f"{WP_PATH}/gitlab_merge_requests"
ISSUES_PATH = f"{WP_PATH}/gitlab_issues"


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def route_all(mock_api: respx.MockRouter, work_package: dict[str, Any]) -> dict[str, Any]:
    """Route the work package and all four development sources."""
    return {
        "work_package": mock_api.get(WP_PATH).mock(
            return_value=httpx.Response(200, json=work_package)
        ),
        "revisions": mock_api.get(REVISIONS_PATH).mock(
            return_value=httpx.Response(200, json=REVISION_COLLECTION)
        ),
        "github": mock_api.get(PULLS_PATH).mock(
            return_value=httpx.Response(200, json=PULL_REQUEST_COLLECTION)
        ),
        "merges": mock_api.get(MERGES_PATH).mock(
            return_value=httpx.Response(200, json=MERGE_REQUEST_COLLECTION)
        ),
        "issues": mock_api.get(ISSUES_PATH).mock(
            return_value=httpx.Response(200, json=GITLAB_ISSUE_COLLECTION)
        ),
    }


# --- registration ---------------------------------------------------------


async def test_both_tools_are_registered_with_honest_annotations(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    activity = tools["get_work_package_git_activity"]
    assert activity.outputSchema is not None
    assert activity.annotations is not None
    assert activity.annotations.readOnlyHint is True
    assert activity.annotations.destructiveHint is False
    assert set(activity.inputSchema["properties"]) == {"work_package_id", "include"}
    assert activity.inputSchema["properties"]["include"]["description"]

    detail = tools["get_github_pull_request"]
    assert detail.outputSchema is not None
    assert detail.annotations is not None
    assert detail.annotations.readOnlyHint is True
    assert set(detail.inputSchema["properties"]) == {"github_pull_request_id"}


async def test_descriptions_state_the_linking_magic_words(mcp_client: Client[Any]) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    activity = tools["get_work_package_git_activity"].description or ""
    assert "refs #123" in activity
    assert "OP#123" in activity
    assert "get_github_pull_request" in activity

    detail = tools["get_github_pull_request"]
    assert "OP#123" in (detail.description or "")
    # The id-producing path is named where the id is consumed (SPEC §5.10).
    assert "get_work_package_git_activity" in (detail.description or "")
    id_description = detail.inputSchema["properties"]["github_pull_request_id"]["description"]
    assert "OpenProject-internal" in id_description
    assert "not the github pr number" in id_description.lower()


# --- the happy path: everything available ---------------------------------


async def test_all_sources_are_fetched_and_projected(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = route_all(mock_api, WORK_PACKAGE_ALL_SOURCES)

    result = await mcp_client.call_tool(
        "get_work_package_git_activity", {"work_package_id": WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    activity = result.structured_content

    assert activity["available"] == {"revisions": True, "github": True, "gitlab": True}
    assert activity["notes"] == []
    assert activity["work_package"] == {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"}
    assert all(route.call_count == 1 for route in routes.values())

    revision = activity["revisions"][0]
    assert revision == {
        "id": 91,
        "identifier": "0f2e1c9a6b8d4f31a7c5e0b9d8f7a6c5e4d3b2a1",
        "formatted_identifier": "0f2e1c9",
        "author_name": "Grace Hopper",
        "message": "refs #1234 pool the httpx client\n\nAdds retries and backoff.",
        "committed_at": "2026-07-03T08:12:44Z",
        "show_url": "/projects/platform/repository/revision/0f2e1c9a6b8d4f31a7c5e0b9d8f7a6c5",
    }

    merged, draft = activity["github_pull_requests"]
    assert merged["id"] == PULL_REQUEST_ID
    assert merged["number"] == 481
    assert merged["state"] == "closed"
    assert merged["merged"] is True
    assert merged["merged_at"] == "2026-07-05T10:02:31Z"
    assert merged["draft"] is False
    assert merged["repository"] == "acme/platform"
    assert merged["labels"] == ["backend", "needs-review"]
    assert merged["author"] == {"id": 3, "name": "ghopper"}
    assert merged["html_url"] == "https://github.com/acme/platform/pull/481"
    assert merged["check_runs"] == [
        {
            "id": 55,
            "name": "ci/test",
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/acme/platform/runs/55",
            "completed_at": "2026-07-03T09:19:00Z",
        },
        {
            "id": 56,
            "name": "ci/lint",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/acme/platform/runs/56",
            "completed_at": "2026-07-03T09:11:00Z",
        },
    ]

    # Check runs also arrive wrapped in a HAL collection on some versions.
    assert draft["draft"] is True
    assert draft["merged"] is False
    assert draft["labels"] == ["chore"]
    assert [run["status"] for run in draft["check_runs"]] == ["in_progress"]

    merge_request = activity["gitlab_merge_requests"][0]
    assert merge_request["id"] == 44
    assert merge_request["number"] == 12
    # GitLab reports the merge as a state, not a boolean; it is normalized.
    assert merge_request["state"] == "merged"
    assert merge_request["merged"] is True
    assert merge_request["pipelines"] == [
        {
            "id": 71,
            "name": "build",
            "status": "success",
            "commit_id": "9a8b7c6",
            "html_url": "https://gitlab.com/acme/platform/-/pipelines/71",
            "started_at": "2026-07-06T11:30:00Z",
            "completed_at": "2026-07-06T11:48:00Z",
        }
    ]

    issue = activity["gitlab_issues"][0]
    assert issue["id"] == 66
    assert issue["number"] == 3
    assert issue["state"] == "opened"
    assert issue["labels"] == ["flaky"]


async def test_sources_are_fetched_concurrently(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """Each handler blocks until the other has started; sequential I/O would time out."""
    pulls_started = asyncio.Event()
    merges_started = asyncio.Event()

    async def pulls(request: httpx.Request) -> httpx.Response:
        pulls_started.set()
        await asyncio.wait_for(merges_started.wait(), timeout=5)
        return httpx.Response(200, json=PULL_REQUEST_COLLECTION)

    async def merges(request: httpx.Request) -> httpx.Response:
        merges_started.set()
        await asyncio.wait_for(pulls_started.wait(), timeout=5)
        return httpx.Response(200, json=MERGE_REQUEST_COLLECTION)

    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_ALL_SOURCES))
    mock_api.get(REVISIONS_PATH).mock(return_value=httpx.Response(200, json=REVISION_COLLECTION))
    mock_api.get(ISSUES_PATH).mock(return_value=httpx.Response(200, json=GITLAB_ISSUE_COLLECTION))
    mock_api.get(PULLS_PATH).mock(side_effect=pulls)
    mock_api.get(MERGES_PATH).mock(side_effect=merges)

    result = await mcp_client.call_tool(
        "get_work_package_git_activity", {"work_package_id": WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    assert len(result.structured_content["github_pull_requests"]) == 2
    assert len(result.structured_content["gitlab_merge_requests"]) == 1
    assert result.structured_content["notes"] == []


async def test_include_narrows_the_fan_out_and_says_what_was_skipped(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = route_all(mock_api, WORK_PACKAGE_ALL_SOURCES)

    result = await mcp_client.call_tool(
        "get_work_package_git_activity",
        {"work_package_id": WORK_PACKAGE_ID, "include": ["github"]},
    )
    assert result.structured_content is not None
    activity = result.structured_content

    assert routes["github"].call_count == 1
    assert routes["revisions"].call_count == 0
    assert routes["merges"].call_count == 0
    assert routes["issues"].call_count == 0

    # Availability is reported for every source, fetched or not.
    assert activity["available"] == {"revisions": True, "github": True, "gitlab": True}
    assert activity["revisions"] == []
    assert activity["gitlab_merge_requests"] == []
    assert len(activity["github_pull_requests"]) == 2
    assert any("revisions: available but not fetched" in note for note in activity["notes"])
    assert any("gitlab: available but not fetched" in note for note in activity["notes"])


async def test_empty_include_is_rejected_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = route_all(mock_api, WORK_PACKAGE_ALL_SOURCES)
    result = await mcp_client.call_tool(
        "get_work_package_git_activity",
        {"work_package_id": WORK_PACKAGE_ID, "include": []},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "include" in error["message"]
    assert routes["work_package"].call_count == 0


# --- availability detection (SPEC §8) -------------------------------------


async def test_plugin_links_alone_do_not_grant_availability(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """The corrected rule: collection links render unconditionally, tabs do not."""
    routes = route_all(mock_api, WORK_PACKAGE_GITHUB_ONLY)

    result = await mcp_client.call_tool(
        "get_work_package_git_activity", {"work_package_id": WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    activity = result.structured_content

    assert activity["available"] == {"revisions": False, "github": True, "gitlab": False}
    assert routes["github"].call_count == 1
    assert routes["merges"].call_count == 0, "no gitlab tab link means no permission to read it"
    assert routes["issues"].call_count == 0
    assert routes["revisions"].call_count == 0

    notes = activity["notes"]
    assert any("GitLab module is installed" in note and "no permission" in note for note in notes)
    assert any("revisions" in note and "view changesets" in note for note in notes)
    assert len(activity["github_pull_requests"]) == 2


async def test_no_dev_links_reports_absent_modules_without_calling_them(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = route_all(mock_api, WORK_PACKAGE_NO_SOURCES)

    result = await mcp_client.call_tool(
        "get_work_package_git_activity", {"work_package_id": WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    activity = result.structured_content

    assert activity["available"] == {"revisions": False, "github": False, "gitlab": False}
    assert activity["github_pull_requests"] == []
    for key in ("revisions", "github", "merges", "issues"):
        assert routes[key].call_count == 0
    notes = activity["notes"]
    assert any(note == "github: not available on this instance (module absent)" for note in notes)
    assert any(note == "gitlab: not available on this instance (module absent)" for note in notes)


# --- per-source degradation (G5) ------------------------------------------


async def test_a_forbidden_source_becomes_a_note_not_a_failure(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_ALL_SOURCES))
    mock_api.get(REVISIONS_PATH).mock(return_value=httpx.Response(403, json=FORBIDDEN_ERROR))
    mock_api.get(PULLS_PATH).mock(return_value=httpx.Response(200, json=PULL_REQUEST_COLLECTION))
    mock_api.get(MERGES_PATH).mock(return_value=httpx.Response(200, json=MERGE_REQUEST_COLLECTION))
    mock_api.get(ISSUES_PATH).mock(return_value=httpx.Response(200, json=GITLAB_ISSUE_COLLECTION))

    result = await mcp_client.call_tool(
        "get_work_package_git_activity", {"work_package_id": WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    activity = result.structured_content

    assert result.is_error is False
    assert activity["available"]["revisions"] is False, "the endpoint overrules the link"
    assert activity["available"]["github"] is True
    assert activity["revisions"] == []
    assert len(activity["github_pull_requests"]) == 2
    assert any("revisions: no permission (403)" in note for note in activity["notes"])


async def test_a_missing_module_becomes_a_note_per_sub_source(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_ALL_SOURCES))
    mock_api.get(REVISIONS_PATH).mock(return_value=httpx.Response(200, json=REVISION_COLLECTION))
    mock_api.get(PULLS_PATH).mock(return_value=httpx.Response(200, json=PULL_REQUEST_COLLECTION))
    mock_api.get(MERGES_PATH).mock(return_value=httpx.Response(200, json=MERGE_REQUEST_COLLECTION))
    mock_api.get(ISSUES_PATH).mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    result = await mcp_client.call_tool(
        "get_work_package_git_activity", {"work_package_id": WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    activity = result.structured_content

    assert activity["gitlab_issues"] == []
    assert len(activity["gitlab_merge_requests"]) == 1, "the sibling source still answered"
    assert activity["available"]["gitlab"] is False
    assert any(
        "gitlab issues: module not installed on this instance (404)" in note
        for note in activity["notes"]
    )


async def test_empty_sources_return_empty_lists_not_notes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(200, json=WORK_PACKAGE_ALL_SOURCES))
    for path in (REVISIONS_PATH, PULLS_PATH, MERGES_PATH, ISSUES_PATH):
        mock_api.get(path).mock(return_value=httpx.Response(200, json=hal_collection([])))

    result = await mcp_client.call_tool(
        "get_work_package_git_activity", {"work_package_id": WORK_PACKAGE_ID}
    )
    assert result.structured_content is not None
    assert result.structured_content["notes"] == []
    assert result.structured_content["available"]["gitlab"] is True
    assert result.structured_content["github_pull_requests"] == []


async def test_unknown_work_package_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(WP_PATH).mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))
    sources = mock_api.get(PULLS_PATH).mock(
        return_value=httpx.Response(200, json=PULL_REQUEST_COLLECTION)
    )

    result = await mcp_client.call_tool(
        "get_work_package_git_activity", {"work_package_id": WORK_PACKAGE_ID}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "ids come from" in error["hint"]
    assert sources.call_count == 0, "no fan-out when the work package itself is unreadable"


# --- get_github_pull_request ----------------------------------------------


async def test_pull_request_detail_carries_body_diff_counts_and_links(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get(f"github_pull_requests/{PULL_REQUEST_ID}").mock(
        return_value=httpx.Response(200, json=PULL_REQUEST_DETAIL)
    )

    result = await mcp_client.call_tool(
        "get_github_pull_request", {"github_pull_request_id": PULL_REQUEST_ID}
    )
    assert result.structured_content is not None
    pull_request = result.structured_content

    assert route.call_count == 1
    assert pull_request["id"] == PULL_REQUEST_ID
    assert pull_request["number"] == 481
    assert pull_request["body"] == "Fixes OP#1234.\n\nPools the client and adds retries."
    assert pull_request["additions"] == 210
    assert pull_request["deletions"] == 38
    assert pull_request["changed_files"] == 7
    assert pull_request["comments_count"] == 4
    assert pull_request["review_comments_count"] == 2
    assert pull_request["merged_by"] == {"id": 1, "name": "alovelace"}
    assert pull_request["work_packages"] == [
        {"id": WORK_PACKAGE_ID, "name": "Ship the client layer"}
    ]
    assert [run["conclusion"] for run in pull_request["check_runs"]] == ["success", "failure"]


async def test_unknown_pull_request_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("github_pull_requests/999").mock(
        return_value=httpx.Response(404, json=NOT_FOUND_ERROR)
    )
    result = await mcp_client.call_tool(
        "get_github_pull_request", {"github_pull_request_id": 999}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["hint"]


async def test_rejected_pull_request_request_surfaces_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    """A 422 has no write path here, but its envelope is still product surface."""
    mock_api.get(f"github_pull_requests/{PULL_REQUEST_ID}").mock(
        return_value=httpx.Response(
            422,
            json={
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Id is not a valid pull request id.",
                "_embedded": {"details": {"attribute": "id"}},
            },
        )
    )
    result = await mcp_client.call_tool(
        "get_github_pull_request",
        {"github_pull_request_id": PULL_REQUEST_ID},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "id", "message": "Id is not a valid pull request id."}
    ]
    assert "violations" in error["hint"]
