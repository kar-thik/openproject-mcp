"""SPEC section 13.5 doc-sync gate: docs and registered tools may never drift.

These tests parse the machine-readable tool tables in README.md and SPEC.md
section 6 and assert set equality with what ``build_server()`` actually
registers (admin tools included), plus the release-critical README invariants:
the Configuration section documents exactly the frozen env surface, the
registry name marker is present, and every markdown link survives being read
off-GitHub (PyPI, registries). Everything runs over the in-memory FastMCP
client — no network.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastmcp import Client

from openproject_mcp.config import Settings
from openproject_mcp.server import build_server
from tests.conftest import TEST_URL
from tests.unit.test_config import ENV_SURFACE

REPO = Path(__file__).resolve().parents[2]

#: SPEC section 6 header: "Count: 85 tools — Ph1: 16 · Ph2: 33 · Ph3: 36."
EXPECTED_TOOL_COUNT = 85

#: The MCP registry name marker README.md must carry verbatim.
MCP_NAME_MARKER = "<!-- mcp-name: io.github.kar-thik/openproject-mcp-server -->"

#: First backticked lowercase identifier on a README table row = the tool name.
#: ``[^|`]*`` forbids crossing a cell boundary, so backticks in later cells
#: (defaults, troubleshooting commands) never match; env-variable rows are
#: uppercase and never match either.
_TOOL_ROW = re.compile(r"^\|[^|`]*`([a-z0-9_]+)`", re.MULTILINE)

#: A backticked tool name inside one SPEC table cell.
_BACKTICKED_NAME = re.compile(r"`([a-z0-9_]+)`")

#: A backticked env-variable mention; tolerates `NAME=value` example forms.
_ENV_TOKEN = re.compile(r"`(OPENPROJECT[A-Z0-9_]*)")

#: A markdown link target: everything between "](" and ")".
_LINK_TARGET = re.compile(r"\]\(([^)]+)\)")


def _readme() -> str:
    return (REPO / "README.md").read_text(encoding="utf-8")


def _readme_tools() -> set[str]:
    """Tool names in the README ``## Tools`` section tables.

    Scoped to the Tools section (mirroring the Configuration-section scoping
    below) so a future table elsewhere in the README whose first cell holds a
    backticked lowercase token can never spuriously fail the sync gate.
    """
    parts = _readme().split("\n## Tools\n", 1)
    assert len(parts) == 2, "README.md must contain the '## Tools' H2"
    section = parts[1].split("\n## ", 1)[0]
    names: list[str] = _TOOL_ROW.findall(section)
    return set(names)


def _spec_catalog_tools() -> set[str]:
    """All tool names in the SPEC section 6 tables.

    Scans every backticked name in the first cell of each table row, because
    section 6.13 documents two tools on one shared row
    (``list_documents`` / ``get_document``).
    """
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    section6 = spec.split("## 6. Tool catalog", 1)[1].split("\n## 7.", 1)[0]
    tools: set[str] = set()
    for line in section6.splitlines():
        if line.startswith("|"):
            first_cell = line.split("|")[1]
            tools.update(_BACKTICKED_NAME.findall(first_cell))
    return tools


async def _registered_tools() -> set[str]:
    """Every registered tool name, admin tools included (docs list all 85)."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        url=TEST_URL,
        api_key="test-token",
        admin_tools=True,
    )
    async with Client(build_server(settings)) as client:
        return {tool.name for tool in await client.list_tools()}


def _sync_diff(documented: set[str], registered: set[str]) -> str:
    return (
        f"missing from docs: {sorted(registered - documented)}; "
        f"stale in docs: {sorted(documented - registered)}"
    )


async def test_readme_tool_table_matches_registered_tools() -> None:
    documented = _readme_tools()
    registered = await _registered_tools()
    assert documented == registered, f"README.md out of sync — {_sync_diff(documented, registered)}"


async def test_spec_catalog_matches_registered_tools() -> None:
    catalog = _spec_catalog_tools()
    registered = await _registered_tools()
    assert catalog == registered, (
        f"SPEC.md section 6 out of sync — {_sync_diff(catalog, registered)}"
    )


async def test_registered_tool_count_matches_spec_declared_count() -> None:
    assert len(await _registered_tools()) == EXPECTED_TOOL_COUNT


def test_readme_has_configuration_heading_and_registry_marker() -> None:
    readme = _readme()
    assert "## Configuration" in readme.splitlines(), (
        "README.md must contain an H2 line reading exactly '## Configuration'"
    )
    assert MCP_NAME_MARKER in readme, f"README.md must contain the marker {MCP_NAME_MARKER!r}"


def test_readme_configuration_section_documents_exact_env_surface() -> None:
    parts = _readme().split("\n## Configuration\n", 1)
    assert len(parts) == 2, "README.md must contain the '## Configuration' H2"
    section = parts[1].split("\n## ", 1)[0]
    documented: set[str] = set(_ENV_TOKEN.findall(section))
    assert documented == set(ENV_SURFACE), (
        "README Configuration section out of sync with the frozen env surface — "
        f"undocumented: {sorted(ENV_SURFACE - documented)}; "
        f"unknown in docs: {sorted(documented - ENV_SURFACE)}"
    )


def test_readme_links_are_absolute_or_anchors() -> None:
    """Relative links break off-GitHub (PyPI project page, MCP registries)."""
    targets: list[str] = _LINK_TARGET.findall(_readme())
    assert targets, "README.md should contain markdown links"
    bad = [t for t in targets if not t.startswith(("http://", "https://", "#"))]
    assert not bad, f"relative markdown link targets in README.md: {bad}"
