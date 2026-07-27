"""Instance, metadata and schema tools (SPEC §6.1, §6.12).

Lands here:

===============================  ======  ==========================================
Tool                             Phase   Endpoint(s)
===============================  ======  ==========================================
🔍 ``get_instance_info``         1       ``GET /``, ``/configuration``, ``/users/me``
🔍 ``get_project_metadata``      1       types/statuses/priorities/versions/…
🔍 ``get_work_package_schema``   1       ``GET /work_packages/schemas/{p}-{t}``
🔍 ``list_permissions``          2       ``GET /capabilities?filters=…``
===============================  ======  ==========================================

Non-negotiables for this module:

* ``get_instance_info`` doubles as the connection test and reports the feature
  probe (``await ctx.probe()``) so the model can see what this instance
  supports (G5).
* ``get_project_metadata`` **without** ``project_id`` returns the instance-global
  sets, so cross-project filtering never requires picking an arbitrary project.
  It is the one-call answer to "what ids/names do I use here" — no instance
  values are ever hardcoded (G3).
* Everything here is cached through ``ctx.cache`` (TTL from
  ``OPENPROJECT_MCP_CACHE_TTL``, default 300 s) and every tool takes
  ``refresh=false`` to bypass it.
* ``get_work_package_schema`` exposes writable flags, required flags and allowed
  values as ``{id, name}`` — it is what makes custom-field writes resolvable
  (SPEC §6.2.1) and what error hints point at.
* ``list_permissions`` resolves the **numeric** principal id via cached
  ``users/me`` (the capabilities API has no ``"me"``) and uses the probed
  ``p{id}``/``w{id}`` context prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(mcp: FastMCP) -> None:
    """Register the metadata tools. Phase 1 fills in the three read tools."""
