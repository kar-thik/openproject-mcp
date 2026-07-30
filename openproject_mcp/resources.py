"""MCP resource templates: ``openproject://…`` URIs (SPEC §10).

Resources give resource-aware clients (``@openproject:`` mentions in Claude
Code) direct reads of work packages, projects and attachment bytes without a
tool call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register the resource templates (Phase 3 stub)."""
