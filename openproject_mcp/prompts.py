"""MCP prompts: report and workflow templates (SPEC §10).

Prompts surface as slash commands in Claude Code. They render structured data
(from the reporting tools) into ready-to-send documents, parameterized by
``locale`` so localized templates are configuration, not forks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register the prompt templates (Phase 3 stub)."""
