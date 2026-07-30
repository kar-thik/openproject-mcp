"""Protocol tests for the MCP prompts and resource templates (SPEC §10).

Both surfaces are exercised through the in-memory FastMCP client against a
respx-mocked instance, because the decision they encode is only true in-process:
a prompt handler reaches the lifespan tool context, so ``prompts/get`` returns a
*rendered* document built from live data rather than instructions telling the
model to go and call tools. The rendered text is asserted against fixture values
— the counts, the hours, the ticket numbers — not merely for absence of error.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from mcp.shared.exceptions import McpError

from openproject_mcp import resources
from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import TEST_URL
from tests.fixtures.reporting_payloads import (
    ATTACHMENT_BYTES,
    ATTACHMENT_ID,
    ATTACHMENT_METADATA,
    BLOCKED_RELATION_COLLECTION,
    EMPTY_RELATION_COLLECTION,
    FROM_DATE,
    MEMBERSHIP_COLLECTION,
    NOT_FOUND_ERROR,
    NOTIFICATION_COLLECTION,
    PROJECT,
    PROJECT_ID,
    QUARANTINED_ATTACHMENT,
    STATUS_COLLECTION,
    TIME_ENTRY_COLLECTION,
    TO_DATE,
    WORK_PACKAGE_DETAIL,
    WORK_PACKAGE_ID,
    backlog_collection,
    hal_collection,
    work_package_response,
)

WP_PATH = f"projects/{PROJECT_ID}/work_packages"
ATTACHMENT_URI = f"openproject://attachment/{ATTACHMENT_ID}"


def _settings(**overrides: Any) -> Settings:
    """Settings with a knob turned, for the cases the shared fixture cannot reach."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", **overrides
    )


def mock_report_api(mock_api: respx.MockRouter) -> dict[str, respx.Route]:
    """Every endpoint a rendered report reads, including the relation probes."""
    mock_api.get(f"projects/{PROJECT_ID}").mock(return_value=httpx.Response(200, json=PROJECT))
    mock_api.get("statuses").mock(return_value=httpx.Response(200, json=STATUS_COLLECTION))
    mock_api.get("time_entries").mock(return_value=httpx.Response(200, json=TIME_ENTRY_COLLECTION))
    mock_api.get("memberships").mock(return_value=httpx.Response(200, json=MEMBERSHIP_COLLECTION))
    for work_package_id in (1235, 1236, 1237, 1238):
        mock_api.get(f"work_packages/{work_package_id}/relations").mock(
            return_value=httpx.Response(200, json=EMPTY_RELATION_COLLECTION)
        )
    return {
        "work_packages": mock_api.get(WP_PATH).mock(side_effect=work_package_response),
        "relations": mock_api.get("work_packages/1234/relations").mock(
            return_value=httpx.Response(200, json=BLOCKED_RELATION_COLLECTION)
        ),
    }


async def rendered(client: Client[Any], name: str, arguments: dict[str, Any]) -> str:
    """The text of the single message a prompt renders."""
    result = await client.get_prompt(name, arguments)
    assert len(result.messages) == 1
    content = result.messages[0].content
    assert content.type == "text"
    return content.text


# --- registration ---------------------------------------------------------


async def test_the_four_prompts_are_advertised(mcp_client: Client[Any]) -> None:
    prompts = {prompt.name: prompt for prompt in await mcp_client.list_prompts()}
    assert set(prompts) == {"weekly_report", "daily_standup", "triage_inbox", "groom_backlog"}

    weekly = prompts["weekly_report"]
    arguments = {argument.name: argument for argument in weekly.arguments or []}
    assert set(arguments) == {"project", "from_date", "to_date", "locale", "team_name"}
    assert arguments["project"].required is True
    assert arguments["from_date"].required is False
    assert arguments["locale"].required is False
    assert "isClosed" in (weekly.description or "")

    assert [argument.name for argument in prompts["daily_standup"].arguments or []] == ["project"]
    assert prompts["triage_inbox"].arguments in (None, [])
    assert [argument.name for argument in prompts["groom_backlog"].arguments or []] == ["project"]


async def test_the_three_resource_templates_are_advertised(mcp_client: Client[Any]) -> None:
    templates = {
        template.uriTemplate: template for template in await mcp_client.list_resource_templates()
    }
    assert set(templates) == {
        "openproject://work_package/{id}",
        "openproject://project/{identifier}",
        "openproject://attachment/{id}",
    }
    assert templates["openproject://work_package/{id}"].mimeType == "application/json"
    assert templates["openproject://project/{identifier}"].mimeType == "application/json"
    assert templates["openproject://attachment/{id}"].mimeType == "application/octet-stream"
    assert "search_work_packages" in (
        templates["openproject://work_package/{id}"].description or ""
    )


# --- weekly_report --------------------------------------------------------


async def test_weekly_report_renders_the_eight_sections_from_live_data(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_report_api(mock_api)

    text = await rendered(
        mcp_client,
        "weekly_report",
        {
            "project": str(PROJECT_ID),
            "from_date": FROM_DATE,
            "to_date": TO_DATE,
            "team_name": "Platform Squad",
        },
    )

    # The eight sections of the template this server inherits, in order.
    for heading in (
        "A. GENERAL INFORMATION",
        "B. EXECUTIVE SUMMARY",
        "C. DELIVERY & BACKLOG MOVEMENT",
        "D. RESOURCES & DELIVERY CAPACITY",
        "E. IMPEDIMENTS & DEPENDENCIES",
        "F. QUALITY & SYSTEM STABILITY",
        "G. PLAN FOR NEXT WEEK",
        "H. SPRINT HEALTH & IMPROVEMENTS",
        "APPENDIX: ONE-PAGER FOR LEADERSHIP",
    ):
        assert f"## {heading}" in text
    assert text.index("A. GENERAL") < text.index("H. SPRINT HEALTH")

    assert f"{FROM_DATE} - {TO_DATE}" in text
    assert "Platform Squad" in text
    assert "Platform" in text

    # Done is decided by isClosed: neither 'Shipped' nor 'Rejected' contains a
    # keyword the old classifier looked for, and both land under Done.
    assert "1) Completed (Done) (2)" in text
    assert "#1236" in text
    assert "Pool the httpx client" in text
    assert "#1237" in text
    assert "2) In progress (1)" in text

    # Planned is decided by the row's own timestamps — #1235 was raised inside the
    # window and its last change is still its creation. It cannot be decided by
    # absence from the updatedAt window: a row created inside a window is always
    # changed inside it too, which would leave section G permanently empty.
    assert "3) Raised but not started (Planned) (1)" in text
    assert "1. #1235 Drop sentinel dates (Unassigned - no due date)" in text
    assert "**Planned:** 1" in text

    # Capacity comes from the time entries, per activity and per person.
    assert "7.5 hours" in text
    assert "| Development | 6.0 | 80.0% |" in text
    assert "| Management | 1.5 | 20.0% |" in text
    assert "| Grace Hopper | 4.0 | 1 |" in text
    assert "Team size:** 2 people" in text

    # Impediments come from the blocks/blocked relations that are visible.
    assert "blocked by #1240 Provision the CI runners" in text
    assert "Waiting on the infrastructure ticket." in text
    assert "Off track" in text

    # Server-side open counts, verbatim.
    assert "| In progress | 4 |" in text
    assert "| New | 9 |" in text
    assert "Open work packages now | 13" in text

    # The collector's honesty markers survive into the document.
    assert "### Data notes" in text
    assert "isClosed flag" in text
    # And the model is told not to touch the numbers.
    assert "must not be changed" in text


async def test_weekly_report_renders_the_vietnamese_template(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_report_api(mock_api)

    text = await rendered(
        mcp_client,
        "weekly_report",
        {
            "project": str(PROJECT_ID),
            "from_date": FROM_DATE,
            "to_date": TO_DATE,
            "locale": "vi",
        },
    )

    assert "# BÁO CÁO TUẦN - AGILE SCRUM" in text
    for heading in (
        "A. THÔNG TIN CHUNG",
        "B. TÓM TẮT ĐIỀU HÀNH",
        "C. DELIVERY & BACKLOG MOVEMENT",
        "D. NGUỒN LỰC & NĂNG LỰC THỰC THI",
        "E. TRỞ NGẠI (IMPEDIMENTS) & PHỤ THUỘC",
        "F. CHẤT LƯỢNG & ỔN ĐỊNH HỆ THỐNG",
        "G. KẾ HOẠCH TUẦN TỚI",
        "H. SPRINT HEALTH & CẢI TIẾN",
        "PHỤ LỤC: BẢN SIÊU GỌN CHO LÃNH ĐẠO",
    ):
        assert f"## {heading}" in text
    assert "1) Công việc đã hoàn thành (Done) (2)" in text
    assert "Chậm tiến độ" in text
    # The numbers are the same document, only the labels changed.
    assert "7.5 giờ" in text
    assert "#1236" in text


async def test_an_unknown_locale_falls_back_to_english_and_says_so(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_report_api(mock_api)

    text = await rendered(
        mcp_client,
        "weekly_report",
        {"project": str(PROJECT_ID), "from_date": FROM_DATE, "to_date": TO_DATE, "locale": "fr"},
    )

    assert "# WEEKLY REPORT - AGILE SCRUM" in text
    assert "locale 'fr' has no template" in text
    assert "en, vi" in text


async def test_an_omitted_window_is_derived_and_declared(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)

    text = await rendered(mcp_client, "weekly_report", {"project": str(PROJECT_ID)})

    today = dt.date.today()
    start = (today - dt.timedelta(days=6)).isoformat()
    assert f"{start} - {today.isoformat()}" in text
    assert "no window was given" in text

    created = routes["work_packages"].calls[0].request
    filters = json.loads(created.url.params["filters"])
    assert filters[1] == {"createdAt": {"operator": "<>d", "values": [start, today.isoformat()]}}


async def test_an_inverted_window_is_refused_before_any_request(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)

    text = await rendered(
        mcp_client,
        "weekly_report",
        {"project": str(PROJECT_ID), "from_date": TO_DATE, "to_date": FROM_DATE},
    )

    assert "is after to_date" in text
    assert routes["work_packages"].call_count == 0


async def test_an_upstream_failure_renders_the_structured_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects/999").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    text = await rendered(
        mcp_client,
        "weekly_report",
        {"project": "999", "from_date": FROM_DATE, "to_date": TO_DATE},
    )

    assert "OpenProject could not be read" in text
    envelope = json.loads(text.split("```json")[1].split("```")[0])
    assert envelope["error"]["type"] == "not_found"
    assert envelope["error"]["http_status"] == 404
    assert envelope["error"]["hint"]
    assert "list_projects" in text


# --- daily_standup --------------------------------------------------------


async def test_daily_standup_renders_yesterday_due_today_and_blockers(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    routes = mock_report_api(mock_api)

    text = await rendered(mcp_client, "daily_standup", {"project": str(PROJECT_ID)})

    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    assert f"Yesterday: {yesterday}" in text
    assert "## Completed yesterday (2)" in text
    assert "#1236" in text and "#1237" in text
    assert "## Moved yesterday, still open (1)" in text
    assert "## Due today (1)" in text
    assert "Publish the release notes" in text
    assert "## Blocked (1)" in text
    assert "blocked by #1240" in text
    assert "Time logged yesterday: 7.5 h" in text

    # The window really is yesterday, both bounds.
    created = routes["work_packages"].calls[0].request
    filters = json.loads(created.url.params["filters"])
    assert filters[1]["createdAt"]["values"] == [yesterday, yesterday]


# --- triage_inbox ---------------------------------------------------------


async def test_triage_inbox_groups_unread_notifications_by_reason(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("notifications").mock(
        return_value=httpx.Response(200, json=NOTIFICATION_COLLECTION)
    )

    text = await rendered(mcp_client, "triage_inbox", {})

    assert "3 unread notification(s)" in text
    assert "## mentioned (2)" in text
    assert "## assigned (1)" in text
    assert "#1234 Ship the client layer" in text
    assert "Suggested action:" in text
    assert "mark_notifications(ids=[21, 22], read=true)" in text

    # Only unread notifications are asked for.
    filters = json.loads(route.calls[0].request.url.params["filters"])
    assert filters == [{"readIAN": {"operator": "=", "values": ["f"]}}]


async def test_triage_inbox_on_an_empty_inbox_says_so(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("notifications").mock(
        return_value=httpx.Response(200, json=hal_collection([], total=0))
    )

    text = await rendered(mcp_client, "triage_inbox", {})
    assert "The inbox is empty" in text


# --- groom_backlog --------------------------------------------------------


async def test_groom_backlog_sweeps_unassigned_unestimated_and_stale(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    recent = f"{dt.date.today().isoformat()}T09:00:00Z"
    route = mock_api.get(WP_PATH).mock(
        return_value=httpx.Response(200, json=backlog_collection(recent))
    )

    text = await rendered(mcp_client, "groom_backlog", {"project": str(PROJECT_ID)})

    assert "2 open work package(s); 2 scanned" in text
    assert "## No assignee (1)" in text
    assert "## No estimate (1)" in text
    assert "## Stale (1)" in text
    assert "#1235" in text
    assert "Drop sentinel dates" in text
    assert "update_work_package(id, status=<a closed status>)" in text

    params = route.calls[0].request.url.params
    assert json.loads(params["filters"]) == [{"status": {"operator": "o", "values": []}}]
    # Oldest change first, so the sweep starts where the rot is.
    assert json.loads(params["sortBy"]) == [["updatedAt", "asc"]]


# --- resources ------------------------------------------------------------


async def test_the_work_package_resource_returns_the_detail_projection(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"work_packages/{WORK_PACKAGE_ID}").mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE_DETAIL)
    )

    contents = await mcp_client.read_resource(f"openproject://work_package/{WORK_PACKAGE_ID}")
    assert len(contents) == 1
    assert contents[0].mimeType == "application/json"
    payload = json.loads(contents[0].text)  # type: ignore[union-attr]

    assert payload["id"] == WORK_PACKAGE_ID
    assert payload["subject"] == "Ship the client layer"
    assert payload["status"] == {"id": 7, "name": "In progress"}
    assert payload["assignee"] == {"id": 12, "name": "Grace Hopper"}
    assert payload["version"] == {"id": 3, "name": "Sprint 12"}
    assert payload["description"] == "Pool the httpx client and add retries."
    assert payload["estimated_hours"] == 8.0
    assert payload["spent_hours"] == 6.5
    assert payload["lock_version"] == 9
    assert payload["available"] == {"dev_links": True, "meetings": False, "files": True}
    # What the projection cannot carry, it says (G5).
    assert payload["custom_fields"] == []
    assert any("get_work_package" in note for note in payload["notes"])


async def test_the_project_resource_returns_the_overview(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects/platform").mock(return_value=httpx.Response(200, json=PROJECT))

    contents = await mcp_client.read_resource("openproject://project/platform")
    assert contents[0].mimeType == "application/json"
    payload = json.loads(contents[0].text)  # type: ignore[union-attr]

    assert payload["id"] == PROJECT_ID
    assert payload["identifier"] == "platform"
    assert payload["name"] == "Platform"
    assert payload["active"] is True
    assert payload["public"] is False
    assert payload["parent"] is None
    assert payload["status_code"] == "on_track"
    assert payload["description"] == "The platform team's board."
    assert payload["updated_at"] == "2026-07-06T09:00:00Z"


async def test_the_attachment_resource_returns_bytes_with_the_real_mime_type(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get(f"attachments/{ATTACHMENT_ID}").mock(
        return_value=httpx.Response(200, json=ATTACHMENT_METADATA)
    )
    mock_api.get(f"attachments/{ATTACHMENT_ID}/content").mock(
        return_value=httpx.Response(200, content=ATTACHMENT_BYTES)
    )

    contents = await mcp_client.read_resource(ATTACHMENT_URI)
    assert len(contents) == 1
    # The declared template MIME type is a fallback; the real one wins per read.
    assert contents[0].mimeType == "image/png"
    assert base64.b64decode(contents[0].blob) == ATTACHMENT_BYTES  # type: ignore[union-attr]


async def test_an_attachment_above_the_cap_is_refused_before_the_bytes(
    mock_api: respx.MockRouter,
) -> None:
    declared = 2 * 1024 * 1024
    mock_api.get(f"attachments/{ATTACHMENT_ID}").mock(
        return_value=httpx.Response(200, json={**ATTACHMENT_METADATA, "fileSize": declared})
    )
    content_route = mock_api.get(f"attachments/{ATTACHMENT_ID}/content").mock(
        return_value=httpx.Response(200, content=ATTACHMENT_BYTES)
    )

    async with Client(build_server(_settings(max_download_mb=1))) as client:
        with pytest.raises(McpError) as raised:
            await client.read_resource(ATTACHMENT_URI)

    envelope = json.loads(str(raised.value))
    assert envelope["error"]["type"] == "attachment_too_large"
    assert envelope["error"]["size_bytes"] == declared
    assert "OPENPROJECT_MCP_MAX_DOWNLOAD_MB" in envelope["error"]["hint"]
    # The declared size is enough to refuse: not a byte is fetched.
    assert content_route.call_count == 0


async def test_an_attachment_that_outgrows_the_cap_mid_stream_is_aborted(
    mock_api: respx.MockRouter,
) -> None:
    oversized = b"z" * (1024 * 1024 + 4096)
    # The metadata under-reports the size, so only the streaming guard can catch it.
    mock_api.get(f"attachments/{ATTACHMENT_ID}").mock(
        return_value=httpx.Response(200, json={**ATTACHMENT_METADATA, "fileSize": 10})
    )
    mock_api.get(f"attachments/{ATTACHMENT_ID}/content").mock(
        return_value=httpx.Response(200, content=oversized)
    )

    async with Client(build_server(_settings(max_download_mb=1))) as client:
        with pytest.raises(McpError) as raised:
            await client.read_resource(ATTACHMENT_URI)

    envelope = json.loads(str(raised.value))
    assert envelope["error"]["type"] == "attachment_too_large"
    assert "while streaming" in envelope["error"]["message"]


async def test_a_stalled_attachment_stream_hits_the_wall_clock_deadline(
    mcp_client: Client[Any], mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A streamed body has no read timeout, so this deadline is the only thing
    # that stops a trickling transfer from hanging the session.
    monkeypatch.setattr(resources, "DOWNLOAD_DEADLINE_SECONDS", 0.0)
    mock_api.get(f"attachments/{ATTACHMENT_ID}").mock(
        return_value=httpx.Response(200, json=ATTACHMENT_METADATA)
    )
    mock_api.get(f"attachments/{ATTACHMENT_ID}/content").mock(
        return_value=httpx.Response(200, content=ATTACHMENT_BYTES)
    )

    with pytest.raises(McpError) as raised:
        await mcp_client.read_resource(ATTACHMENT_URI)

    envelope = json.loads(str(raised.value))
    assert envelope["error"]["type"] == "network_error"
    assert "stalled" in envelope["error"]["hint"]


async def test_a_quarantined_attachment_is_refused_before_the_bytes(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("attachments/78").mock(
        return_value=httpx.Response(200, json=QUARANTINED_ATTACHMENT)
    )
    content_route = mock_api.get("attachments/78/content").mock(
        return_value=httpx.Response(200, content=ATTACHMENT_BYTES)
    )

    with pytest.raises(McpError) as raised:
        await mcp_client.read_resource("openproject://attachment/78")

    envelope = json.loads(str(raised.value))
    assert envelope["error"]["type"] == "attachment_quarantined"
    assert "administrator" in envelope["error"]["hint"]
    assert content_route.call_count == 0


async def test_an_unknown_resource_id_raises_the_structured_envelope(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/999").mock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR))

    with pytest.raises(McpError) as raised:
        await mcp_client.read_resource("openproject://work_package/999")

    envelope = json.loads(str(raised.value))
    assert envelope["error"]["type"] == "not_found"
    assert envelope["error"]["http_status"] == 404
    assert envelope["error"]["error_identifier"] == "urn:openproject-org:api:v3:errors:NotFound"
    assert envelope["error"]["hint"]
