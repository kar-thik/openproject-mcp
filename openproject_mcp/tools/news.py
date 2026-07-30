"""News tools: list, read and CRUD project news (SPEC §6.13).

News is a core (non-module) resource; the write tools follow the standard
form-validation flow and the delete requires confirmation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register the news tools (Phase 3 stub)."""
