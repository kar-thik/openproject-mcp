"""Saved-query tools (SPEC §6.7 — Phase 2).

Tools that land here: list_queries, run_query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register these tools once the module is implemented."""
