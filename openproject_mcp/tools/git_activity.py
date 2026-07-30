"""Git and development activity tools (SPEC §6.5, §8 — Phase 2).

Tools that land here: get_work_package_git_activity, get_github_pull_request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register these tools once the module is implemented."""
