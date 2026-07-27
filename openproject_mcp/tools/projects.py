"""Project tools (SPEC §6.6).

Lands here:

============================  ======  ==============================================
Tool                          Phase   Endpoint(s)
============================  ======  ==============================================
🔍 ``list_projects``          1       ``GET /projects`` (paginated)
🔍 ``get_project``            1       ``GET /projects/{id}``
✏️ ``create_project``         2       form → ``POST /projects``
✏️ ``update_project``         2       form → ``PATCH /projects/{id}``
🗑 ``delete_project``         2       ``DELETE /projects/{id}`` (**async** job)
✏️ ``copy_project``           3       ``POST /projects/{id}/copy`` → async job
🔍 ``get_job_status``         3       ``GET /job_statuses/{uuid}``
✏️Ⓜ ``set_project_favorite``  3       ``POST/DELETE /projects/{id}/favorite`` (≥ 17)
============================  ======  ==============================================

Non-negotiables for this module:

* ``list_projects`` is paginated — the old server's unpaginated version silently
  truncated. Use the §9.3 envelope (G1).
* Project parameters accept a numeric id **or** the string identifier; both must
  work everywhere a project is named.
* ``status_code`` is the closed enum ``on_track, at_risk, off_track,
  not_started, finished, discontinued`` written through the ``status`` link, not
  a free string.
* ``delete_project`` is asynchronous upstream: report the scheduled state, never
  claim the project is gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(mcp: FastMCP) -> None:
    """Register the project tools. Phase 1 fills in list/get."""
