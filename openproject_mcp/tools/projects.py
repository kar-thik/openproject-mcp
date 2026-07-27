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

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    NotFoundError,
    ValidationFailedError,
)
from openproject_mcp.client.filters import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Filter,
    FilterType,
    Op,
    make_filter,
    query_params,
    register_filter_type,
)
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools._shared import (
    GROUP_PROJECTS,
    READ,
    envelope_from_collection,
    get_tool_context,
    read_annotations,
    tool_errors,
    tool_tags,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["ProjectDetail", "ProjectRow", "register"]

#: Resource key for the filter registry — ``/projects`` has its own strategies.
PROJECTS_RESOURCE = "projects"

#: Sort keys accepted by ``list_projects``; mapped to camelCase centrally.
PROJECT_SORT_KEYS: frozenset[str] = frozenset(
    {"id", "name", "identifier", "created_at", "updated_at", "active", "public"}
)

#: The closed project-status enum (SPEC §6.6), surfaced as ``status_code``.
PROJECT_STATUS_CODES: tuple[str, ...] = (
    "on_track",
    "at_risk",
    "off_track",
    "not_started",
    "finished",
    "discontinued",
)

_FAVORED_HINT = (
    "This OpenProject instance rejected the 'favored' filter, which means it predates "
    "project favorites. Call list_projects again without favorites_only."
)


class ProjectRow(BaseModel):
    """Compact project row for list results (SPEC §5.2, §6.6)."""

    id: int | str | None = Field(
        default=None, description="Numeric project id; accepted by every project_id parameter."
    )
    identifier: str | None = Field(
        default=None,
        description="URL slug from /projects/<identifier>; also accepted wherever an id is.",
    )
    name: str | None = Field(default=None, description="Display name.")
    active: bool | None = Field(
        default=None, description="False for archived projects (read-only in the UI)."
    )
    public: bool | None = Field(
        default=None, description="True when visible to users without a membership."
    )
    parent: Ref | None = Field(
        default=None, description="Parent project, when this is a subproject."
    )
    status_code: str | None = Field(
        default=None,
        description=(
            f"Project status code, one of: {', '.join(PROJECT_STATUS_CODES)}. A code, never a "
            "translated label; null means no status has been set."
        ),
    )


class ProjectDetail(ProjectRow):
    """Full project detail (SPEC §6.6): the row plus text fields and timestamps."""

    description: str | None = Field(
        default=None, description="Description as markdown (raw); html is dropped."
    )
    status_explanation: str | None = Field(
        default=None, description="Free-text explanation of status_code, markdown (raw)."
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


def _status_code(payload: Mapping[str, Any]) -> str | None:
    """Read the status code from ``_links.status`` (or the pre-13 ``status`` object)."""
    linked = hal.ref(payload, "status")
    if linked is not None and linked.id is not None:
        return str(linked.id)
    raw = payload.get("status")
    if isinstance(raw, Mapping):
        code = raw.get("code")
        return code if isinstance(code, str) else None
    return raw if isinstance(raw, str) else None


def _project_row(payload: Mapping[str, Any]) -> ProjectRow:
    identifier = payload.get("identifier")
    return ProjectRow(
        id=hal.self_id(payload),
        identifier=identifier if isinstance(identifier, str) else None,
        name=payload.get("name"),
        active=payload.get("active"),
        public=payload.get("public"),
        parent=Ref.from_hal(payload, "parent"),
        status_code=_status_code(payload),
    )


def _project_detail(payload: Mapping[str, Any]) -> ProjectDetail:
    row = _project_row(payload)
    return ProjectDetail(
        **row.model_dump(),
        description=hal.formattable(payload.get("description")),
        status_explanation=hal.formattable(payload.get("statusExplanation")),
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
    )


def _not_found_hint(key: str) -> str:
    if key.isdigit():
        return (
            f"No project with numeric id {key}. Ids come from list_projects. If {key} was meant "
            "as the URL identifier, pass it as a string identifier instead."
        )
    return (
        f"No project with identifier {key!r}. The identifier is the URL slug "
        "(/projects/<identifier>), not the display name — find it with "
        "list_projects(search=...), or pass the numeric id."
    )


def _register_filters() -> None:
    """Teach the filter validator the project-only filter names we send."""
    register_filter_type("favored", FilterType.BOOLEAN, PROJECTS_RESOURCE)
    register_filter_type("name_and_identifier", FilterType.SEARCH, PROJECTS_RESOURCE)
    register_filter_type("parent_id", FilterType.RELATION, PROJECTS_RESOURCE)
    register_filter_type("active", FilterType.BOOLEAN, PROJECTS_RESOURCE)


def register(mcp: FastMCP) -> None:
    """Register the project tools. Phase 1 fills in list/get."""
    _register_filters()

    @mcp.tool(
        name="list_projects",
        tags=tool_tags(GROUP_PROJECTS, READ),
        annotations=read_annotations(title="List projects"),
    )
    @tool_errors
    async def list_projects(
        search: Annotated[
            str | None,
            Field(
                description=(
                    "Case-insensitive substring matched against the project name AND its "
                    "identifier. Descriptions are not searched. Omit to list everything the "
                    "filters allow."
                )
            ),
        ] = None,
        active: Annotated[
            bool | None,
            Field(
                description=(
                    "true (default) lists active projects, false lists only archived ones, "
                    "null lists both. Archived projects are read-only in OpenProject."
                )
            ),
        ] = True,
        parent_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric id or identifier of a parent project; returns its DIRECT children "
                    "only, not the whole subtree. Ids come from a previous list_projects call."
                )
            ),
        ] = None,
        favorites_only: Annotated[
            bool,
            Field(
                description=(
                    "Restrict to projects the authenticated user has favorited. Instances that "
                    "predate project favorites reject this filter with a 400."
                )
            ),
        ] = False,
        sort_by: Annotated[
            list[list[str]] | None,
            Field(
                description=(
                    'Server-side sort, e.g. [["name", "asc"], ["created_at", "desc"]]. Allowed '
                    "keys: active, created_at, id, identifier, name, public, updated_at. "
                    "Unknown keys are rejected with the allowed set listed."
                )
            ),
        ] = None,
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_PAGE_SIZE,
                description=(
                    f"Records per page (max {MAX_PAGE_SIZE}); the instance may clamp it lower "
                    "and the returned pagination reports what actually came back."
                ),
            ),
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[ProjectRow]:
        """List projects, filtered server-side, one page at a time.

        Use this to turn a project name into the id or identifier that every other tool
        consumes, to enumerate the sub-projects of a parent, or to review which projects are
        off track. It is the id-producing path for every ``project_id`` parameter in this
        server.

        Returns the standard list envelope: ``items`` of
        ``{id, identifier, name, active, public, parent, status_code}`` plus ``pagination``
        with ``total``/``page``/``page_size``/``has_more``. Nothing is truncated silently —
        page explicitly until ``has_more`` is false.

        Pitfalls: ``search`` matches name and identifier only (not descriptions);
        ``parent_id`` returns direct children, so a deep hierarchy needs one call per level;
        ``status_code`` is a code such as ``on_track``, never a translated label.

        For a single project's description and status explanation use ``get_project``. For
        the types, versions, categories and time-entry activities valid inside a project use
        ``get_project_metadata``. To list a project's work packages use
        ``list_work_packages(project=...)``.
        """
        ctx = get_tool_context()

        filters: list[Filter] = []
        if search and search.strip():
            filters.append(
                make_filter(
                    "name_and_identifier",
                    Op.CONTAINS,
                    [search.strip()],
                    resource=PROJECTS_RESOURCE,
                )
            )
        if active is not None:
            filters.append(make_filter("active", Op.EQ, [active], resource=PROJECTS_RESOURCE))
        if parent_id is not None:
            filters.append(make_filter("parent_id", Op.EQ, [parent_id], resource=PROJECTS_RESOURCE))
        if favorites_only:
            filters.append(make_filter("favored", Op.EQ, [True], resource=PROJECTS_RESOURCE))

        params = query_params(
            filters=filters,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_keys=PROJECT_SORT_KEYS,
        )
        try:
            payload = await ctx.client.get_json("projects", params=params)
        except ValidationFailedError as exc:
            if not favorites_only:
                raise
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=_FAVORED_HINT,
            ) from exc

        unwrapped = hal.collection(payload)
        rows = [_project_row(element) for element in unwrapped]
        return envelope_from_collection(unwrapped, rows, page=page, page_size=page_size)

    @mcp.tool(
        name="get_project",
        tags=tool_tags(GROUP_PROJECTS, READ),
        annotations=read_annotations(title="Get project"),
    )
    @tool_errors
    async def get_project(
        id_or_identifier: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric project id (e.g. 7) or the URL identifier (e.g. 'demo-project'). "
                    "Both are accepted; identifiers come from list_projects, and are the slug "
                    "in /projects/<identifier>, not the display name."
                )
            ),
        ],
    ) -> ProjectDetail:
        """Read one project in full.

        Use it after ``list_projects`` when you need the description, the status explanation
        or the parent of a specific project — or to verify that an id or identifier a user
        gave you actually resolves.

        Returns ``{id, identifier, name, active, public, parent, status_code, description,
        status_explanation, created_at, updated_at}``. Rich text comes back as markdown
        ``raw``; html is dropped.

        Pitfalls: ``status_code`` is one of on_track, at_risk, off_track, not_started,
        finished, discontinued — an empty ``status_code`` means the project has no status
        set, not "on track". A 404 here means the id/identifier is wrong or the project is
        archived and invisible to this user; the error hint says which spelling to try next.

        For the ids valid inside this project (types, versions, categories, time-entry
        activities) call ``get_project_metadata(project_id=...)``; for its work packages call
        ``list_work_packages(project=...)``.
        """
        ctx = get_tool_context()
        key = str(id_or_identifier).strip()
        if not key:
            raise InputValidationError(
                "id_or_identifier must not be empty.",
                hint=(
                    "Pass a numeric project id or the URL identifier; find both with list_projects."
                ),
            )
        try:
            payload = await ctx.client.get_json(f"projects/{quote(key, safe='')}")
        except NotFoundError as exc:
            raise NotFoundError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=_not_found_hint(key),
            ) from exc
        return _project_detail(payload)
