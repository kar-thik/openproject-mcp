"""Work-package core tools (SPEC §6.2).

Lands here:

===============================  ======  =============================================
Tool                             Phase   Endpoint(s)
===============================  ======  =============================================
🔍 ``search_work_packages``      1       ``GET /work_packages`` + ``typeahead``/``search``
🔍 ``list_work_packages``        1       ``GET /work_packages`` or ``/projects/{id}/work_packages``
🔍 ``get_work_package``          1       ``GET /work_packages/{id}`` (+ sub-resources)
✏️ ``create_work_package``       1       ``POST /work_packages/form`` → ``POST /work_packages``
✏️ ``update_work_package``       1       form → ``PATCH /work_packages/{id}``
🗑 ``delete_work_package``       1       ``DELETE /work_packages/{id}``
===============================  ======  =============================================

Non-negotiables for this module:

* Always send an explicit status filter derived from ``status_scope``
  (``search`` defaults to ``all``, ``list`` to ``open``); never rely on the
  server's implicit open-only default. ``status_ids`` overrides ``status_scope``.
* Writes go through the form endpoint first so validation errors carry allowed
  values, and ``lock_version`` handling goes through
  :func:`openproject_mcp.client.locking.patch_with_lock`.
* ``assignee=None`` clears via ``{"href": null}`` — use
  :func:`openproject_mcp.client.payloads.links_payload`.
* Every include in ``get_work_package`` is capped at 20 items and reports
  ``{"truncated": true, "total": N}`` with a pointer to the full-listing tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(mcp: FastMCP) -> None:
    """Register the work-package core tools. Phase 1 fills this in."""
