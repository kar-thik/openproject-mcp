"""Time-tracking tools (SPEC §6.9 — Phase 2).

Tools that land here: list_time_entries, log_time, update_time_entry, delete_time_entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register these tools once the module is implemented."""
