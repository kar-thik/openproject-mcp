"""Sprint tools (SPEC §6.10).

Lands here:

=================  ======  ====================================================
Tool                 Phase   Endpoint(s)
=================  ======  ====================================================
🔍 ``list_sprints``  2       ``GET /sprints`` or ``/projects/{id}/sprints``
🔍 ``get_sprint``    2       ``GET /sprints/{id}``
=================  ======  ====================================================

Non-negotiables for this module:

* Since OpenProject 17.3 sprints are standalone ``Sprint`` objects, no longer
  versions: ``finishDate`` (not ``endDate``), ``definingWorkspace`` (not
  ``definingProject``), and ``status`` as a URN
  (``urn:openproject-org:api:v3:sprints:status:in_planning|active|completed``).
  The protocol tests assert the exact wire mapping on the read path.
* Sprint writes are deferred: the public API documents no ``POST``/``PATCH``/
  ``DELETE /sprints`` and no ``/sprints/form``. If a live ``GET
  /api/v3/spec.json`` ever shows write paths, add ``create/update/delete_sprint``
  mirroring ``versions.py`` (form → ``merge_form_payload`` → commit).
* Assigning work to a sprint is a work-package write, not a sprint write:
  ``create/update_work_package(sprint=...)`` writes ``_links.sprint``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import NotFoundError
from openproject_mcp.client.filters import query_params
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools._shared import (
    GROUP_VERSIONS,
    READ,
    build_envelope,
    envelope_from_collection,
    get_tool_context,
    read_annotations,
    tool_errors,
    tool_tags,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "SprintDetail",
    "SprintRow",
    "register",
]

SprintStatus = Literal["in_planning", "active", "completed"]

SPRINT_STATUSES: tuple[str, ...] = ("in_planning", "active", "completed")


class SprintRow(BaseModel):
    """One sprint as list results return it."""

    id: int | str | None = Field(
        default=None,
        description="Sprint id — what the work-package 'sprint' field consumes.",
    )
    name: str | None = Field(default=None, description="Sprint name, e.g. 'Sprint 11'.")
    description: str | None = Field(
        default=None, description="Description as markdown (raw); html is dropped."
    )
    start_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD).")
    finish_date: str | None = Field(
        default=None,
        description="ISO date (YYYY-MM-DD); the sprint's finish date (wire 'finishDate').",
    )
    workspace: Ref | None = Field(
        default=None,
        description="The project (workspace) that DEFINES the sprint.",
    )
    status: str | None = Field(
        default=None,
        description="in_planning, active or completed (parsed from the status URN).",
    )


class SprintDetail(SprintRow):
    """A single sprint as ``get_sprint`` returns it."""

    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


# --- projections ----------------------------------------------------------


def _sprint_status(payload: Mapping[str, Any]) -> str | None:
    """Parse the status URN tail: ``...:sprints:status:active`` → ``active``."""
    status_ref = hal.ref(payload, "status")
    if status_ref is None or status_ref.href is None:
        return None
    tail = status_ref.href.rsplit(":", 1)[-1]
    return tail if tail in SPRINT_STATUSES else None


def _sprint_row(payload: Mapping[str, Any]) -> SprintRow:
    return SprintRow(
        id=hal.self_id(payload),
        name=payload.get("name") if isinstance(payload.get("name"), str) else None,
        description=hal.formattable(payload.get("description")),
        start_date=payload.get("startDate"),
        # The wire name is finishDate — sprints are not versions (§17 does not apply).
        finish_date=payload.get("finishDate"),
        workspace=Ref.from_hal(payload, "definingWorkspace"),
        status=_sprint_status(payload),
    )


def _sprint_detail(payload: Mapping[str, Any]) -> SprintDetail:
    row = _sprint_row(payload)
    return SprintDetail(
        **row.model_dump(),
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
    )


def _sprint_not_found(exc: NotFoundError, sprint_id: int) -> NotFoundError:
    return NotFoundError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=(
            f"No sprint with id {sprint_id}. Sprint ids come from "
            "list_sprints — not from a version id and not from a sprint number."
        ),
    )


def register(mcp: FastMCP) -> None:
    """Register the sprint tools (SPEC §6.10)."""

    @mcp.tool(
        name="list_sprints",
        tags=tool_tags(GROUP_VERSIONS, READ),
        annotations=read_annotations(title="List sprints"),
    )
    @tool_errors
    async def list_sprints(
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric project id or URL identifier to list the sprints of that "
                    "project (native or shared). Omit it to list every sprint visible "
                    "to you across the instance."
                )
            ),
        ] = None,
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=100, description="Sprints per page (max 100).")
        ] = 20,
    ) -> ListEnvelope[SprintRow]:
        """List sprints (Scrum timeboxes) you can plan work packages into.

        Use it to turn "Sprint 11" into the sprint id that
        ``create_work_package``/``update_work_package`` consume via their
        ``sprint`` parameter, or to review a project's sprint plan with its
        ``in_planning``/``active``/``completed`` states.

        Returns the standard list envelope: ``items`` of ``{id, name,
        description, start_date, finish_date, workspace, status}`` plus
        ``pagination`` and ``notes``. A project-scoped listing is fetched in
        full, so ``has_more`` is false; the instance-wide listing is paginated.

        Pitfalls: sprints are separate objects from versions since OpenProject
        17.3 — ``finish_date`` is the wire ``finishDate`` (not a version's
        ``endDate``), and ``status`` is the sprint lifecycle, not a version's
        open/locked/closed. Assigning work to a sprint is a work-package write
        (``update_work_package(sprint=...)``); there is no sprint write tool
        because the API documents no sprint write endpoint.

        Cross-references: ``get_sprint`` for one sprint in full;
        ``list_versions`` for releases and milestones; ``list_work_packages``
        with ``sprint_ids`` to see what is IN a sprint.
        """
        ctx = get_tool_context()

        if project_id is None:
            payload = await ctx.client.get_json(
                "sprints", params=query_params(page=page, page_size=page_size)
            )
            collection = hal.collection(payload)
            rows = [_sprint_row(element) for element in collection.elements]
            return envelope_from_collection(
                collection, rows, page=page, page_size=page_size
            )

        try:
            payload = await ctx.client.get_json(f"projects/{project_id}/sprints")
        except NotFoundError as exc:
            raise NotFoundError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=(
                    f"No sprints for project {project_id!r}: the project does not exist, "
                    "or it does not expose sprints (the Scrum/Backlogs module is not "
                    "enabled here). list_projects finds the project id."
                ),
            ) from exc
        rows = [_sprint_row(element) for element in hal.collection(payload).elements]
        return build_envelope(
            rows,
            total=len(rows),
            page=1,
            page_size=max(len(rows), 1),
        )

    @mcp.tool(
        name="get_sprint",
        tags=tool_tags(GROUP_VERSIONS, READ),
        annotations=read_annotations(title="Get sprint"),
    )
    @tool_errors
    async def get_sprint(
        sprint_id: Annotated[
            int,
            Field(description="Numeric sprint id from list_sprints."),
        ],
    ) -> SprintDetail:
        """Read one sprint in full: dates, workspace and lifecycle status.

        Use it once ``list_sprints`` has given you an id and the row is not
        enough — this adds ``created_at``/``updated_at`` to the same shape.

        Pitfalls: sprint ids and version ids are different id spaces — a
        version id from ``list_versions`` does not resolve here.

        Cross-references: ``list_sprints`` for the id; ``list_work_packages``
        with ``sprint_ids`` for the work planned in this sprint.
        """
        ctx = get_tool_context()
        try:
            payload = await ctx.client.get_json(f"sprints/{sprint_id}")
        except NotFoundError as exc:
            raise _sprint_not_found(exc, sprint_id) from exc
        return _sprint_detail(payload)
