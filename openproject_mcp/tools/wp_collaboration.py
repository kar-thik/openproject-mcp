"""Work-package collaboration tools (SPEC §6.3).

Lands here:

=====================================  ======  =======================================
Tool                                   Phase   Endpoint(s)
=====================================  ======  =======================================
🔍 ``list_work_package_comments``      1       ``GET /work_packages/{id}/activities``
✏️ ``add_work_package_comment``        1       ``POST /work_packages/{id}/activities``
✏️ ``edit_work_package_comment``       2       ``PATCH /activities/{id}``
✏️ ``add_work_package_watcher``        2       ``POST /work_packages/{id}/watchers``
✏️ ``remove_work_package_watcher``     2       ``DELETE /work_packages/{id}/watchers/{uid}``
✏️ ``create_work_package_relation``    2       ``POST /work_packages/{id}/relations``
✏️ ``update_work_package_relation``    2       ``PATCH /relations/{id}``
🗑 ``delete_work_package_relation``    2       ``DELETE /relations/{id}``
✏️Ⓜ ``toggle_comment_reaction``        3       ``PATCH /activities/{id}/emoji_reactions``
✏️ ``set_work_package_reminder``       3       ``/work_packages/{id}/reminders``
🔍 ``list_reminders``                  3       ``GET /reminders``
✏️ ``execute_custom_action``           3       ``POST /custom_actions/{id}/execute``
=====================================  ======  =======================================

Non-negotiables for this module:

* The activities endpoint is **unpaginated upstream** — fetch the full journal
  and page client-side, and say so in the description rather than pretending.
* Comment text is capped by ``max_comment_chars`` with a per-item ``truncated``
  marker (G1); ``activity_id`` fetches one entry uncapped.
* ``internal=true`` requires OpenProject ≥ 16: check
  ``(await ctx.probe()).supports_internal_comments`` and hard-error otherwise —
  older servers silently ignore the flag (G2).
* Relation ``type`` is a closed enum and ``lag`` is only valid on
  ``follows``/``precedes``; validate locally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(mcp: FastMCP) -> None:
    """Register the collaboration tools. Phase 1 fills in the two comment tools."""
