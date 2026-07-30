"""Collaboration-module tools: meetings, wiki, documents, budgets (SPEC §6.13).

Every tool here is module-dependent (Ⓜ): the backing endpoint 404s when the
module is not installed and 403s without permission — both are surfaced as
honest degradation notes, never empty results (G5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register the meetings/wiki/documents/budgets tools (Phase 3 stub)."""
