"""A ratchet on how much model-facing description text the tool surface carries.

Every character in a tool's docstring and its parameters' ``Field(description=...)`` text is
tokens the model pays on every call that considers the tool — and, for a client that sends the
full tool list up front, on every turn of the conversation. A tool description that grows
without anyone noticing is a real, recurring cost, not a cosmetic one.

This module is a *ratchet*, not a fixed ceiling: the two budgets below record the current
post-trim state of the surface. When you tighten a tool's wording, lower the constant for what
you touched (or the total) in the same change — don't leave slack for the next person to fill.
When you deliberately add description text (a new tool, a new parameter, a pitfall the model
needs), raise the constant in that same PR, by roughly the amount you added, and say why in the
commit/PR description. Never raise a budget "just in case."

``text_size`` mirrors what a client actually receives: a tool's ``description`` (the docstring)
plus the ``description`` of every *input* property in its JSON schema. Output schemas and other
scaffolding are excluded — they aren't prose the model reads to decide how to call the tool.

To measure by hand::

    uv run pytest tests/unit/test_description_budget.py -q

or, for a one-off look at where the budget goes, adapt the ``text_size`` helper here against
``await build_server(settings).list_tools()`` and sort the results.
"""

from __future__ import annotations

from fastmcp.tools.base import Tool

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import TEST_URL

#: Largest allowed model-facing text (docstring + Σ input-parameter descriptions) for any single
#: tool, in characters. Post-trim maximum (list_work_packages, 2,878 chars as of the 0.3.2
#: description-budget pass), rounded up to the next 100.
PER_TOOL_BUDGET = 2900

#: Largest allowed total model-facing text across every tool, in characters. Post-trim total
#: (148,075 chars as of the 0.3.2 description-budget pass), rounded up to the next 1,000, plus
#: 2,000 characters of documented headroom for one new tool landing in the same release.
TOTAL_BUDGET = 151000


def text_size(tool: Tool) -> int:
    """Model-facing character count: the docstring plus every input parameter's description."""
    properties = tool.parameters.get("properties", {})
    return len(tool.description or "") + sum(
        len(prop.get("description", "")) for prop in properties.values()
    )


async def _admin_tools() -> list[Tool]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, url=TEST_URL, api_key="test-token", admin_tools=True
    )
    mcp = build_server(settings)
    return list(await mcp.list_tools())


async def test_no_tool_exceeds_the_per_tool_description_budget() -> None:
    tools = await _admin_tools()
    oversized = [
        (tool.name, text_size(tool)) for tool in tools if text_size(tool) > PER_TOOL_BUDGET
    ]
    assert not oversized, (
        f"tool(s) exceed the {PER_TOOL_BUDGET}-char per-tool description budget: "
        + ", ".join(f"{name} ({size} chars)" for name, size in oversized)
    )


async def test_total_description_text_stays_within_budget() -> None:
    tools = await _admin_tools()
    total = sum(text_size(tool) for tool in tools)
    assert total <= TOTAL_BUDGET, (
        f"total model-facing description text is {total} chars, over the {TOTAL_BUDGET}-char budget"
    )
