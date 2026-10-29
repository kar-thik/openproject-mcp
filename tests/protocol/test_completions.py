"""``completion/complete`` for prompt arguments and resource-template parameters.

Runs under whichever protocol era ``OPENPROJECT_MCP_TEST_PROTOCOL`` selects;
completion behaves the same on both.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from mcp_types import PromptReference, ResourceTemplateReference

from openproject_mcp.completions import PROJECT_CANDIDATES, PROJECT_PROMPTS
from openproject_mcp.prompts import LOCALES
from openproject_mcp.resources import PROJECT_URI, WORK_PACKAGE_URI


def _projects(*identifiers: str) -> dict[str, Any]:
    elements = [
        {"_type": "Project", "id": index, "identifier": identifier, "name": identifier.title()}
        for index, identifier in enumerate(identifiers, start=1)
    ]
    return {
        "_type": "Collection",
        "total": len(elements),
        "count": len(elements),
        "pageSize": PROJECT_CANDIDATES,
        "offset": 1,
        "_embedded": {"elements": elements},
    }


async def _complete(client: Client[Any], ref: Any, name: str, value: str) -> list[str]:
    completion = await client.complete(ref, {"name": name, "value": value})
    return list(completion.values)


@pytest.mark.parametrize("prompt", sorted(PROJECT_PROMPTS))
async def test_project_prompts_complete_to_matching_active_identifiers(
    mcp_client: Client[Any], mock_api: respx.MockRouter, prompt: str
) -> None:
    route = mock_api.get("projects").mock(
        return_value=httpx.Response(200, json=_projects("website", "web-shop"))
    )

    values = await _complete(mcp_client, PromptReference(name=prompt), "project", " web ")

    assert values == ["website", "web-shop"]
    params = route.calls.last.request.url.params
    assert json.loads(params["filters"]) == [
        {"active": {"operator": "=", "values": ["t"]}},
        {"name_and_identifier": {"operator": "~", "values": ["web"]}},
    ]
    assert params["pageSize"] == str(PROJECT_CANDIDATES)
    assert json.loads(params["sortBy"]) == [["name", "asc"]]


async def test_project_resource_template_completes_its_identifier(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects").mock(return_value=httpx.Response(200, json=_projects("ops")))

    values = await _complete(
        mcp_client, ResourceTemplateReference(uri=PROJECT_URI), "identifier", "o"
    )

    assert values == ["ops"]


async def test_empty_text_lists_active_projects_without_a_name_filter(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("projects").mock(
        return_value=httpx.Response(200, json=_projects("alpha", "beta"))
    )

    values = await _complete(mcp_client, PromptReference(name="daily_standup"), "project", "")

    assert values == ["alpha", "beta"]
    filters = json.loads(route.calls.last.request.url.params["filters"])
    assert filters == [{"active": {"operator": "=", "values": ["t"]}}]


async def test_repeated_keystrokes_reuse_one_lookup(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("projects").mock(return_value=httpx.Response(200, json=_projects("ops")))
    ref = PromptReference(name="groom_backlog")

    await _complete(mcp_client, ref, "project", "Op")
    await _complete(mcp_client, ref, "project", "op")

    assert route.call_count == 1


async def test_an_upstream_failure_yields_no_candidates_not_an_error(
    mcp_client: Client[Any], mock_api: respx.MockRouter
) -> None:
    mock_api.get("projects").mock(return_value=httpx.Response(403, json={"message": "nope"}))

    values = await _complete(mcp_client, PromptReference(name="weekly_report"), "project", "x")

    assert values == []


async def test_weekly_report_locale_completes_from_the_rendered_locales(
    mcp_client: Client[Any],
) -> None:
    ref = PromptReference(name="weekly_report")

    assert await _complete(mcp_client, ref, "locale", "") == list(LOCALES)
    assert await _complete(mcp_client, ref, "locale", "EN") == [
        locale for locale in LOCALES if locale.casefold().startswith("en")
    ]


@pytest.mark.parametrize(
    ("ref", "name"),
    [
        (PromptReference(name="weekly_report"), "team_name"),
        (PromptReference(name="triage_inbox"), "project"),
        (ResourceTemplateReference(uri=WORK_PACKAGE_URI), "id"),
    ],
)
async def test_other_arguments_get_no_candidates_and_no_request(
    mcp_client: Client[Any], mock_api: respx.MockRouter, ref: Any, name: str
) -> None:
    assert await _complete(mcp_client, ref, name, "a") == []
    assert not mock_api.calls


async def test_the_server_advertises_completions(mcp_client: Client[Any]) -> None:
    capabilities = mcp_client.server_capabilities
    assert capabilities is not None
    assert capabilities.completions is not None
