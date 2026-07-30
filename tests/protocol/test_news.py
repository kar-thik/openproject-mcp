"""Protocol tests for the news tools (SPEC §6.13, §9.3, G2/G4/G5).

Everything runs through the in-memory FastMCP client against a respx-mocked
instance. What is asserted here is what a live instance would otherwise have to
prove: the snake_case ``project_id`` filter the news endpoint really wants, the
identifier→id resolution in front of it, the §9.3 envelope with an honest note
when a project's news module is off, the exact create/patch bodies (no
``lockVersion``, KEEP semantics, formattable description), and the structured
error envelopes for 404 / 403 / 422 / unconfirmed deletion.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from tests.fixtures.news_payloads import (
    CREATED_NEWS,
    EMPTY_COLLECTION,
    NEWS,
    NEWS_COLLECTION,
    NEWS_FORBIDDEN,
    NEWS_ID,
    NEWS_NOT_FOUND,
    NEWS_TITLE_BLANK,
    OTHER_NEWS_ID,
    PROJECT,
    PROJECT_ID,
    PROJECT_IDENTIFIER,
    PROJECT_NOT_FOUND,
    UPDATED_NEWS,
    hal_collection,
)

NEWS_PATH = f"news/{NEWS_ID}"


def error_of(result: Any) -> dict[str, Any]:
    """The `{"error": {...}}` body a failed tool call carries as text content."""
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


def body_of(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content)


# --- registration ---------------------------------------------------------


async def test_all_five_news_tools_are_registered_with_honest_annotations(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_news"]
    assert listing.outputSchema is not None
    assert listing.annotations is not None
    assert listing.annotations.readOnlyHint is True
    assert set(listing.inputSchema["properties"]) == {"project_id", "page", "page_size"}
    assert set((listing.meta or {})["fastmcp"]["tags"]) == {"news", "read"}

    reading = tools["get_news"]
    assert reading.annotations is not None
    assert reading.annotations.readOnlyHint is True
    assert set(reading.inputSchema["properties"]) == {"news_id"}

    creating = tools["create_news"]
    assert creating.annotations is not None
    assert creating.annotations.readOnlyHint is False
    assert creating.annotations.destructiveHint is False
    assert set(creating.inputSchema["properties"]) == {
        "project_id",
        "title",
        "summary",
        "description",
    }
    assert set((creating.meta or {})["fastmcp"]["tags"]) == {"news", "write"}

    updating = tools["update_news"]
    assert set(updating.inputSchema["properties"]) == {
        "news_id",
        "title",
        "summary",
        "description",
    }

    deleting = tools["delete_news"]
    assert deleting.annotations is not None
    assert deleting.annotations.destructiveHint is True
    assert deleting.annotations.model_extra is not None
    assert deleting.annotations.model_extra["anthropic/requiresUserInteraction"] is True
    assert set((deleting.meta or {})["fastmcp"]["tags"]) == {"news", "write", "destructive"}


async def test_descriptions_name_the_module_gate_and_the_id_source(
    mcp_client: Client[Any],
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    listing = tools["list_news"].description or ""
    assert "news module" in listing
    assert "get_news" in listing
    assert "newest first" in listing

    assert "manage news" in (tools["create_news"].description or "")
    assert "REPLACE the stored text" in (tools["update_news"].description or "")
    assert "lockVersion" in (tools["update_news"].description or "")
    assert "no undo" in (tools["delete_news"].description or "")


# --- list_news ------------------------------------------------------------


async def test_news_is_listed_newest_first_with_the_standard_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("news").mock(return_value=httpx.Response(200, json=NEWS_COLLECTION))

    result = await mcp_client.call_tool("list_news", {})
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["pagination"] == {"total": 37, "page": 1, "page_size": 20, "has_more": True}
    assert payload["items"][0] == {
        "id": NEWS_ID,
        "title": "Release 2.1 is live",
        "summary": "Payments hardening shipped to production.",
        "project": {"id": PROJECT_ID, "name": "Apollo migration"},
        "author": {"id": 12, "name": "Grace Hopper"},
        "created_at": "2026-07-20T08:00:00Z",
        "can_manage": True,
    }
    # The second entry renders no update/delete link: this account may not edit it.
    assert payload["items"][1]["id"] == OTHER_NEWS_ID
    assert payload["items"][1]["can_manage"] is False
    assert payload["items"][1]["project"] == {"id": 3, "name": "Customer work"}
    assert payload["notes"] is None
    # The body stays out of a listing; get_news fetches it.
    assert "description" not in payload["items"][0]

    params = route.calls[0].request.url.params
    assert params["offset"] == "1"
    assert params["pageSize"] == "20"
    assert json.loads(params["sortBy"]) == [["created_at", "desc"]]
    assert "filters" not in params


async def test_project_scope_sends_the_snake_case_project_id_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("news").mock(
        return_value=httpx.Response(200, json=hal_collection([NEWS], total=1))
    )

    result = await mcp_client.call_tool(
        "list_news", {"project_id": PROJECT_ID, "page": 2, "page_size": 50}
    )
    assert result.structured_content is not None
    assert result.structured_content["items"][0]["id"] == NEWS_ID

    params = route.calls[0].request.url.params
    # The news endpoint keeps this filter snake_case; 'projectId' is not its name.
    assert json.loads(params["filters"]) == [
        {"project_id": {"operator": "=", "values": [str(PROJECT_ID)]}}
    ]
    assert params["offset"] == "2"
    assert params["pageSize"] == "50"


async def test_a_project_identifier_is_resolved_to_the_numeric_id_first(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    project = mock_api.get(f"projects/{PROJECT_IDENTIFIER}").mock(
        return_value=httpx.Response(200, json=PROJECT)
    )
    news = mock_api.get("news").mock(
        return_value=httpx.Response(200, json=hal_collection([NEWS], total=1))
    )

    result = await mcp_client.call_tool("list_news", {"project_id": PROJECT_IDENTIFIER})
    assert result.structured_content is not None

    assert project.call_count == 1
    assert json.loads(news.calls[0].request.url.params["filters"]) == [
        {"project_id": {"operator": "=", "values": [str(PROJECT_ID)]}}
    ]


async def test_an_unknown_project_identifier_fails_before_the_news_call(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects/nope").mock(return_value=httpx.Response(404, json=PROJECT_NOT_FOUND))
    news = mock_api.get("news").mock(return_value=httpx.Response(200, json=NEWS_COLLECTION))

    result = await mcp_client.call_tool("list_news", {"project_id": "nope"}, raise_on_error=False)
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert news.call_count == 0


async def test_an_empty_project_page_says_the_module_may_be_off(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("news").mock(return_value=httpx.Response(200, json=EMPTY_COLLECTION))

    result = await mcp_client.call_tool("list_news", {"project_id": PROJECT_ID})
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload["items"] == []
    assert payload["pagination"] == {"total": 0, "page": 1, "page_size": 20, "has_more": False}
    note = payload["notes"][0]
    assert f"project {PROJECT_ID}" in note
    assert "news module" in note
    assert "view news" in note


async def test_an_empty_instance_wide_page_is_explained_too(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("news").mock(return_value=httpx.Response(200, json=EMPTY_COLLECTION))

    result = await mcp_client.call_tool("list_news", {})
    assert result.structured_content is not None
    assert any("anywhere on the instance" in note for note in result.structured_content["notes"])


# --- get_news -------------------------------------------------------------


async def test_get_news_returns_the_full_markdown_body(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(NEWS_PATH).mock(return_value=httpx.Response(200, json=NEWS))

    result = await mcp_client.call_tool("get_news", {"news_id": NEWS_ID})
    assert result.structured_content is not None
    payload = result.structured_content

    assert payload == {
        "id": NEWS_ID,
        "title": "Release 2.1 is live",
        "summary": "Payments hardening shipped to production.",
        "project": {"id": PROJECT_ID, "name": "Apollo migration"},
        "author": {"id": 12, "name": "Grace Hopper"},
        "created_at": "2026-07-20T08:00:00Z",
        "can_manage": True,
        "description": "# Release 2.1\n\n- Payments hardening\n- Faster search",
        "updated_at": "2026-07-20T08:00:00Z",
    }


async def test_unknown_news_id_returns_the_not_found_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("news/999").mock(return_value=httpx.Response(404, json=NEWS_NOT_FOUND))

    result = await mcp_client.call_tool("get_news", {"news_id": 999}, raise_on_error=False)
    error = error_of(result)
    assert error["type"] == "not_found"
    assert error["http_status"] == 404
    assert error["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert "list_news" in error["hint"]
    assert "news module" in error["hint"]


# --- create_news ----------------------------------------------------------


async def test_create_news_posts_title_summary_and_a_formattable_description(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("news").mock(return_value=httpx.Response(201, json=CREATED_NEWS))

    result = await mcp_client.call_tool(
        "create_news",
        {
            "project_id": PROJECT_ID,
            "title": "  Weekly report — week 30  ",
            "summary": "Two features closed, one blocked.",
            "description": "## Highlights\n\n- Feature X closed",
        },
    )

    assert body_of(route) == {
        "title": "Weekly report — week 30",
        "summary": "Two features closed, one blocked.",
        "description": {"format": "markdown", "raw": "## Highlights\n\n- Feature X closed"},
        "_links": {"project": {"href": f"/api/v3/projects/{PROJECT_ID}"}},
    }

    assert result.structured_content is not None
    assert result.structured_content["id"] == 61
    assert result.structured_content["title"] == "Weekly report — week 30"
    assert result.structured_content["project"] == {"id": PROJECT_ID, "name": "Apollo migration"}
    assert result.structured_content["author"] == {"id": 12, "name": "Grace Hopper"}
    assert result.structured_content["created_at"] == "2026-07-26T09:10:00Z"
    assert result.structured_content["can_manage"] is True


async def test_create_news_omits_the_fields_that_were_not_passed(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("news").mock(return_value=httpx.Response(201, json=CREATED_NEWS))

    await mcp_client.call_tool("create_news", {"project_id": PROJECT_ID, "title": "Headline only"})
    sent = body_of(route)
    assert sent["title"] == "Headline only"
    assert "summary" not in sent
    assert "description" not in sent


async def test_create_news_resolves_a_project_identifier_for_the_link(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"projects/{PROJECT_IDENTIFIER}").mock(
        return_value=httpx.Response(200, json=PROJECT)
    )
    route = mock_api.post("news").mock(return_value=httpx.Response(201, json=CREATED_NEWS))

    await mcp_client.call_tool(
        "create_news", {"project_id": PROJECT_IDENTIFIER, "title": "Headline"}
    )
    # The href always carries the numeric id: an identifier href does not resolve upstream.
    assert body_of(route)["_links"] == {"project": {"href": f"/api/v3/projects/{PROJECT_ID}"}}


async def test_create_news_rejects_a_blank_title_locally(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("news").mock(return_value=httpx.Response(201, json=CREATED_NEWS))

    result = await mcp_client.call_tool(
        "create_news", {"project_id": PROJECT_ID, "title": "   "}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "title" in error["message"]
    assert route.call_count == 0


async def test_create_news_without_permission_explains_the_module_gate(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("news").mock(return_value=httpx.Response(403, json=NEWS_FORBIDDEN))

    result = await mcp_client.call_tool(
        "create_news",
        {"project_id": PROJECT_ID, "title": "Release 2.2 is live"},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "permission_denied"
    assert error["http_status"] == 403
    assert "manage news" in error["hint"]
    assert "news module" in error["hint"]


async def test_create_news_surfaces_a_rejected_title_as_violations(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.post("news").mock(return_value=httpx.Response(422, json=NEWS_TITLE_BLANK))

    result = await mcp_client.call_tool(
        "create_news",
        {"project_id": PROJECT_ID, "title": "x" * 300},
        raise_on_error=False,
    )
    error = error_of(result)
    assert error["type"] == "validation_failed"
    assert error["http_status"] == 422
    assert error["violations"] == [
        {"attribute": "title", "message": "Title is too long (maximum is 256 characters)."}
    ]


# --- update_news ----------------------------------------------------------


async def test_update_news_sends_only_what_was_passed_and_no_lock_version(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    patch = mock_api.patch(NEWS_PATH).mock(return_value=httpx.Response(200, json=UPDATED_NEWS))
    read = mock_api.get(NEWS_PATH).mock(return_value=httpx.Response(200, json=NEWS))

    result = await mcp_client.call_tool(
        "update_news", {"news_id": NEWS_ID, "title": "Release 2.1 is live (hotfixed)"}
    )

    assert body_of(patch) == {"title": "Release 2.1 is live (hotfixed)"}
    assert "lockVersion" not in body_of(patch)
    # No read-before-write: there is no lock version to echo back.
    assert read.call_count == 0

    assert result.structured_content is not None
    assert result.structured_content["title"] == "Release 2.1 is live (hotfixed)"
    assert result.structured_content["updated_at"] == "2026-07-27T11:45:00Z"


async def test_update_news_clears_summary_and_description_on_explicit_null(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    patch = mock_api.patch(NEWS_PATH).mock(return_value=httpx.Response(200, json=UPDATED_NEWS))

    result = await mcp_client.call_tool(
        "update_news", {"news_id": NEWS_ID, "summary": None, "description": None}
    )

    assert body_of(patch) == {
        "summary": "",
        "description": {"format": "markdown", "raw": ""},
    }
    assert result.structured_content is not None
    assert result.structured_content["summary"] == ""


async def test_update_news_with_nothing_to_change_never_calls_the_api(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    patch = mock_api.patch(NEWS_PATH).mock(return_value=httpx.Response(200, json=UPDATED_NEWS))

    result = await mcp_client.call_tool("update_news", {"news_id": NEWS_ID}, raise_on_error=False)
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "nothing to change" in error["message"]
    assert "title, summary or description" in error["hint"]
    assert patch.call_count == 0


async def test_update_news_refuses_to_blank_the_title(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    patch = mock_api.patch(NEWS_PATH).mock(return_value=httpx.Response(200, json=UPDATED_NEWS))

    result = await mcp_client.call_tool(
        "update_news", {"news_id": NEWS_ID, "title": ""}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "invalid_input"
    assert "blank title" in error["hint"]
    assert patch.call_count == 0


async def test_update_news_reports_an_unknown_id_as_not_found(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.patch("news/999").mock(return_value=httpx.Response(404, json=NEWS_NOT_FOUND))

    result = await mcp_client.call_tool(
        "update_news", {"news_id": 999, "title": "Anything"}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "list_news" in error["hint"]


# --- delete_news ----------------------------------------------------------


async def test_delete_news_refuses_without_confirmation(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(NEWS_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool("delete_news", {"news_id": NEWS_ID}, raise_on_error=False)
    error = error_of(result)
    assert error["type"] == "confirmation_required"
    assert "confirm=true" in error["hint"]
    assert "comments" in error["hint"]
    assert route.call_count == 0


async def test_delete_news_confirms_the_deletion(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.delete(NEWS_PATH).mock(return_value=httpx.Response(204))

    result = await mcp_client.call_tool("delete_news", {"news_id": NEWS_ID, "confirm": True})

    assert route.call_count == 1
    assert result.structured_content == {
        "id": NEWS_ID,
        "deleted": True,
        "message": f"News entry {NEWS_ID} was deleted, together with its comments.",
    }


async def test_delete_news_reports_an_unknown_id_as_not_found(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete("news/999").mock(return_value=httpx.Response(404, json=NEWS_NOT_FOUND))

    result = await mcp_client.call_tool(
        "delete_news", {"news_id": 999, "confirm": True}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "not_found"
    assert "list_news" in error["hint"]


async def test_delete_news_without_permission_explains_the_module_gate(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.delete(NEWS_PATH).mock(return_value=httpx.Response(403, json=NEWS_FORBIDDEN))

    result = await mcp_client.call_tool(
        "delete_news", {"news_id": NEWS_ID, "confirm": True}, raise_on_error=False
    )
    error = error_of(result)
    assert error["type"] == "permission_denied"
    assert "manage news" in error["hint"]
