"""Notification tools (SPEC §6.8 — Phase 2).

Tools that land here: list_notifications, mark_notifications, mark_all_notifications_read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register these tools once the module is implemented."""
