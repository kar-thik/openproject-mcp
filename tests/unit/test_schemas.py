"""Output-schema compaction and the wire-size ratchet on ``tools/list``.

The budgets are a ratchet like ``test_description_budget``: they record the
current size, rounded up. Lower them when you shrink the surface; raise them
only deliberately, in the change that adds the bytes, and say why.
``scripts/measure_surface.py`` prints the same numbers.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import Client

from openproject_mcp.config import Settings
from openproject_mcp.schemas import compact_schema
from openproject_mcp.server import build_server
from tests.conftest import TEST_URL

#: Compact JSON bytes of every output schema in ``tools/list`` (full profile, admin on).
#: 0.3.4 shipped 295,527; compaction brought it to 100,846, and the 0.4.0 report section
#: states (states, drill-downs, roster/open objects) added 1,702.
OUTPUT_SCHEMA_BUDGET = 103_000

#: Compact JSON bytes of the whole ``tools/list`` (full profile, admin on). 0.3.4: 520,725;
#: 0.4.0: 328,166.
TOOLS_LIST_BUDGET = 329_000


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def test_compaction_drops_annotations_but_keeps_field_names() -> None:
    schema = {
        "type": "object",
        "description": "A work package.",
        "properties": {
            "description": {"type": "string", "description": "Markdown body."},
            "default": {"type": "integer", "default": 0},
        },
        "required": ["description"],
    }

    assert compact_schema(schema) == {
        "type": "object",
        "properties": {"description": {"type": "string"}, "default": {"type": "integer"}},
        "required": ["description"],
    }


def test_compaction_folds_bare_type_unions_only() -> None:
    nullable = {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "x"}
    nested = {"anyOf": [{"type": "object", "properties": {}}, {"type": "null"}]}

    assert compact_schema(nullable) == {"type": ["string", "null"]}
    assert compact_schema(nested) == nested


def test_compaction_leaves_instance_data_alone() -> None:
    schema = {"enum": [{"description": "kept"}], "const": {"default": 1}}

    assert compact_schema(schema) == schema


async def _listed_tools() -> list[dict[str, Any]]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", admin_tools=True
    )
    async with Client(build_server(settings)) as client:
        return [
            tool.model_dump(mode="json", exclude_none=True, by_alias=True)
            for tool in await client.list_tools()
        ]


async def test_listed_output_schemas_are_compact_and_within_budget() -> None:
    tools = await _listed_tools()
    output = [tool["outputSchema"] for tool in tools if "outputSchema" in tool]

    assert '"description"' not in json.dumps(
        [{k: v for k, v in schema.items() if k != "properties"} for schema in output]
    )
    total = sum(_size(schema) for schema in output)
    assert total <= OUTPUT_SCHEMA_BUDGET, f"output schemas are {total} B"


async def test_input_schemas_keep_their_descriptions() -> None:
    tools = {tool["name"]: tool for tool in await _listed_tools()}

    properties = tools["list_work_packages"]["inputSchema"]["properties"]
    assert properties["status_scope"]["description"]


async def test_tools_list_stays_within_budget() -> None:
    total = _size(await _listed_tools())
    assert total <= TOOLS_LIST_BUDGET, f"tools/list is {total} B"
