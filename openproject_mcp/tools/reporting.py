"""Reporting: structured project-report aggregation (SPEC §6.14).

``get_project_report_data`` powers the report prompts (weekly report, standup)
with windowed work-package and time-entry aggregates. Status bucketing uses the
API's ``isClosed`` flag, never status-name keywords; internal paging caps are
reported in-band (G1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register the reporting tools (Phase 3 stub)."""
