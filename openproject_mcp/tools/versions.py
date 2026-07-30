"""Version and sprint tools (SPEC §6.10 — Phase 2).

Tools that land here: list_versions, create_version, update_version, delete_version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register these tools once the module is implemented."""
