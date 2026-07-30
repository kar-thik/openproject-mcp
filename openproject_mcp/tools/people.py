"""People and access tools (SPEC §6.11 — Phase 2).

Tools that land here: search_principals, get_user, list_memberships,
create_membership, update_membership, delete_membership, list_roles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register these tools once the module is implemented."""
