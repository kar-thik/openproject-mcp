"""Tool modules and their registration order (SPEC §3.2).

Registration is explicit: ``server.py`` calls :func:`register_all`, which calls
each module's ``register(mcp)``. No import side effects, no circular imports —
a tool module never imports ``server``.

Adding a module (a later phase) means adding it to :data:`TOOL_MODULES` here;
filling an existing module means editing only that module.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING

from openproject_mcp.tools import (
    attachments,
    git_activity,
    metadata,
    modules_collab,
    news,
    notifications,
    people,
    projects,
    queries,
    reporting,
    time_entries,
    versions,
    work_packages,
    wp_collaboration,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["TOOL_MODULES", "register_all"]


#: Registration order. Only affects listing order, not behavior.
TOOL_MODULES: tuple[ModuleType, ...] = (
    work_packages,
    wp_collaboration,
    attachments,
    git_activity,
    projects,
    queries,
    notifications,
    time_entries,
    versions,
    people,
    metadata,
    modules_collab,
    news,
    reporting,
)


def register_all(mcp: FastMCP) -> None:
    """Register every tool module on the server."""
    for module in TOOL_MODULES:
        module.register(mcp)
