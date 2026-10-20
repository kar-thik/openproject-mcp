#!/usr/bin/env python3
"""Measure the server's payload baselines: tools/list bytes and report cost.

Deterministic and offline. The tool surface is read through an in-memory MCP
client; the report runs against a synthetic busy project served by respx, so
the request counts and result bytes are reproducible. Byte counts are compact
UTF-8 JSON, not tokens: they are stable across tokenizers.

    uv run python scripts/measure_surface.py [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import respx
from fastmcp import Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures.reporting_payloads import (  # noqa: E402
    MEMBERSHIP_COLLECTION,
    OPEN_GROUPED,
    PROJECT,
    PROJECT_ID,
    STATUS_COLLECTION,
    hal_collection,
    time_entry_element,
    work_package_element,
)

from openproject_mcp.config import Settings  # noqa: E402
from openproject_mcp.server import build_server  # noqa: E402

URL = "https://openproject.example.com"
FROM_DATE, TO_DATE = "2026-07-01", "2026-07-07"

#: The synthetic busy week: rows each window matches upstream.
WORKLOAD = {"created": 450, "updated": 900, "closed": 300, "time_entries": 1500}


def size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def _settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, url=URL, api_key="measure", admin_tools=True, **overrides
    )


async def tool_surface(profile: str) -> dict[str, int]:
    async with Client(build_server(_settings(profile=profile))) as client:
        tools = [
            tool.model_dump(mode="json", exclude_none=True, by_alias=True)
            for tool in await client.list_tools()
        ]
    return {
        "tools": len(tools),
        "tools_list_bytes": size(tools),
        "input_schema_bytes": sum(size(tool.get("inputSchema", {})) for tool in tools),
        "output_schema_bytes": sum(size(tool.get("outputSchema", {})) for tool in tools),
        "description_bytes": sum(len(tool.get("description", "").encode()) for tool in tools),
    }


def _page(elements: list[dict[str, Any]], request: httpx.Request) -> httpx.Response:
    offset = int(request.url.params.get("offset", "1"))
    page_size = int(request.url.params.get("pageSize", "20"))
    start = (offset - 1) * page_size
    chunk = elements[start : start + page_size]
    return httpx.Response(
        200, json=hal_collection(chunk, total=len(elements), pageSize=page_size, offset=offset)
    )


def _rows(count: int, first_id: int, status: tuple[int, str]) -> list[dict[str, Any]]:
    return [
        work_package_element(first_id + n, f"Synthetic work package {first_id + n}", status=status)
        for n in range(count)
    ]


def _router(mock: respx.MockRouter, counts: Counter[str]) -> None:
    created = _rows(WORKLOAD["created"], 10_000, (1, "New"))
    updated = _rows(WORKLOAD["updated"], 20_000, (7, "In progress"))
    closed = _rows(WORKLOAD["closed"], 30_000, (9, "Shipped"))
    entries = [
        time_entry_element(
            n, "PT1H", spent_on="2026-07-02", activity=(3, "Development"), user=(12, "Grace")
        )
        for n in range(WORKLOAD["time_entries"])
    ]

    def work_packages(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "groupBy" in params:
            counts["work_packages:grouped"] += 1
            return httpx.Response(200, json=OPEN_GROUPED)
        filters = params.get("filters", "")
        if '"c"' in filters:
            counts["work_packages:closed"] += 1
            return _page(closed, request)
        if "createdAt" in filters:
            counts["work_packages:created"] += 1
            return _page(created, request)
        counts["work_packages:updated"] += 1
        return _page(updated, request)

    def time_entries(request: httpx.Request) -> httpx.Response:
        counts["time_entries"] += 1
        return _page(entries, request)

    def fixed(name: str, payload: dict[str, Any]) -> Any:
        def respond(request: httpx.Request) -> httpx.Response:
            counts[name] += 1
            return httpx.Response(200, json=payload)

        return respond

    api = f"{URL}/api/v3"
    mock.get(f"{api}/projects/{PROJECT_ID}").mock(side_effect=fixed("project", PROJECT))
    mock.get(f"{api}/statuses").mock(side_effect=fixed("statuses", STATUS_COLLECTION))
    mock.get(f"{api}/projects/{PROJECT_ID}/work_packages").mock(side_effect=work_packages)
    mock.get(f"{api}/time_entries").mock(side_effect=time_entries)
    mock.get(f"{api}/memberships").mock(side_effect=fixed("memberships", MEMBERSHIP_COLLECTION))


async def report_cost() -> dict[str, Any]:
    counts: Counter[str] = Counter()
    with respx.mock(assert_all_called=False) as mock:
        _router(mock, counts)
        async with Client(build_server(_settings(profile="full"))) as client:
            result = await client.call_tool(
                "get_project_report_data",
                {"project_id": PROJECT_ID, "from_date": FROM_DATE, "to_date": TO_DATE},
            )
    return {
        "workload": WORKLOAD,
        "upstream_requests": sum(counts.values()),
        "requests_by_endpoint": dict(sorted(counts.items())),
        "result_bytes": size(result.structured_content),
    }


async def measure() -> dict[str, Any]:
    return {
        "surface": {profile: await tool_surface(profile) for profile in ("full", "core")},
        "get_project_report_data": await report_cost(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print one JSON document.")
    args = parser.parse_args()
    results = asyncio.run(measure())
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    for profile, numbers in results["surface"].items():
        print(
            f"tools/list ({profile}, admin on): "
            + ", ".join(f"{k}={v}" for k, v in numbers.items())
        )
    report = results["get_project_report_data"]
    print(
        f"get_project_report_data ({report['workload']}): "
        f"requests={report['upstream_requests']} {report['requests_by_endpoint']}, "
        f"result_bytes={report['result_bytes']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
