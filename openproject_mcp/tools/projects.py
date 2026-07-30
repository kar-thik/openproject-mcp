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

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    NotFoundError,
    ValidationFailedError,
    violations_from_form,
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
from openproject_mcp.client.payloads import build_write_payload, formattable_field, link
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools._shared import (
    DESTRUCTIVE,
    GROUP_PROJECTS,
    READ,
    WRITE,
    destructive_annotations,
    envelope_from_collection,
    get_tool_context,
    read_annotations,
    require_confirmation,
    tool_errors,
    tool_tags,
    write_annotations,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "KEEP",
    "ProjectDeletionResult",
    "ProjectDetail",
    "ProjectRow",
    "register",
]

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


# --- Phase 2: writes (SPEC §6.6, §4.5) ------------------------------------

#: Sentinel for update parameters that can be *cleared* as well as left alone.
#: ``parent_id=KEEP`` leaves the parent untouched, ``parent_id=None`` detaches it.
KEEP = "__unchanged__"

#: The closed status enum as a type — the same values as PROJECT_STATUS_CODES.
ProjectStatusCode = Literal[
    "on_track",
    "at_risk",
    "off_track",
    "not_started",
    "finished",
    "discontinued",
]

#: Project status is a link to its own collection, not to work-package statuses.
PROJECT_STATUS_RESOURCE = "project_statuses"

#: OpenProject identifiers: lowercase, digits, dashes and underscores.
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_STATUS_CODE_HINT = f"status_code must be one of: {', '.join(PROJECT_STATUS_CODES)}."


class ProjectDeletionResult(BaseModel):
    """Outcome of ``delete_project`` — a *scheduled* deletion, never a finished one."""

    id: int | str = Field(description="The project id or identifier the deletion was asked for.")
    scheduled: bool = Field(
        description="True once OpenProject accepted the request. Deletion runs as a background "
        "job, so the project can still be readable for a while afterwards."
    )
    job_id: str | None = Field(
        default=None,
        description="Background job id, when the instance reported one; null otherwise. "
        "OpenProject exposes it at /api/v3/job_statuses/{id}.",
    )
    message: str = Field(description="What was scheduled, in plain language.")


def _form_section(form: Mapping[str, Any], key: str) -> Any:
    inner = form.get("_embedded")
    return inner.get(key) if isinstance(inner, Mapping) else None


def _raise_form_validation_errors(form: Mapping[str, Any]) -> None:
    """Turn a project form's ``validationErrors`` into a typed error (SPEC §4.5).

    The form is asked first precisely so a taken identifier or an unknown parent
    comes back as an attribute-level violation instead of an opaque 422 on the
    commit.
    """
    errors = _form_section(form, "validationErrors")
    if not isinstance(errors, Mapping) or not errors:
        return

    violations = violations_from_form(errors)
    hints: list[str] = []
    if "identifier" in errors:
        hints.append(
            "The identifier must be unique across the instance and may only contain lowercase "
            "letters, digits, '-' and '_'. Pick another one, or omit identifier and let "
            "OpenProject derive it from the name."
        )
    if "parent" in errors:
        hints.append(
            "parent_id must be a project you may add a subproject to; list candidates with "
            "list_projects."
        )
    if "status" in errors:
        hints.append(_STATUS_CODE_HINT)
    if not hints:
        hints.append(
            "Fix the attributes listed in 'violations'. list_projects shows the projects and "
            "identifiers that already exist."
        )

    identifier: str | None = None
    first = next((value for value in errors.values() if isinstance(value, Mapping)), None)
    if first is not None and isinstance(first.get("errorIdentifier"), str):
        identifier = first["errorIdentifier"]

    raise ValidationFailedError(
        violations[0]["message"] if violations else "OpenProject rejected the project.",
        http_status=422,
        error_identifier=identifier,
        hint=" ".join(hints),
        violations=violations,
    )


def _links_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    links = payload.get("_links")
    return dict(links) if isinstance(links, Mapping) else {}


def _merge_form_payload(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the form's defaulted payload with ours; ours wins, ``_links`` merge.

    The form fills in what OpenProject derives (the identifier generated from the
    name, the default status); our payload carries the caller's intent.
    """
    merged: dict[str, Any] = {
        key: value for key, value in base.items() if key not in ("_links", "_type")
    }
    merged.update({key: value for key, value in override.items() if key != "_links"})
    links = _links_of(base)
    links.update(_links_of(override))
    links.pop("self", None)
    if links:
        merged["_links"] = links
    return merged


def _validate_identifier(identifier: str) -> str:
    candidate = identifier.strip()
    if not _IDENTIFIER_RE.match(candidate):
        raise InputValidationError(
            f"Invalid project identifier {identifier!r}.",
            hint=(
                "An identifier is the URL slug: lowercase letters, digits, '-' and '_' only, "
                "starting with a letter or digit (e.g. 'apollo-migration'). Omit it to let "
                "OpenProject derive one from the name."
            ),
        )
    return candidate


def _project_key(id_or_identifier: int | str) -> str:
    key = str(id_or_identifier).strip()
    if not key:
        raise InputValidationError(
            "id_or_identifier must not be empty.",
            hint="Pass a numeric project id or the URL identifier; find both with list_projects.",
        )
    return key


def _with_not_found_hint(exc: NotFoundError, key: str) -> NotFoundError:
    return NotFoundError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=_not_found_hint(key),
    )


def _job_id(payload: Mapping[str, Any]) -> str | None:
    """Read the background-job id out of a 202 body, when the instance sends one."""
    href = hal.self_href(payload)
    if isinstance(href, str) and "job_statuses" in href:
        job = hal.id_from_href(href)
        return str(job) if job is not None else None
    for key in ("jobStatusId", "jobId"):
        value = payload.get(key)
        if isinstance(value, str | int):
            return str(value)
    return None


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

    @mcp.tool(
        name="create_project",
        tags=tool_tags(GROUP_PROJECTS, WRITE),
        annotations=write_annotations(title="Create project"),
    )
    @tool_errors
    async def create_project(
        name: Annotated[
            str,
            Field(
                description=(
                    "Display name of the new project, e.g. 'Apollo migration'. Names need not "
                    "be unique on the instance; the identifier is what must be."
                )
            ),
        ],
        identifier: Annotated[
            str | None,
            Field(
                description=(
                    "URL slug for /projects/<identifier>: lowercase letters, digits, '-' and "
                    "'_' only, unique across the whole instance. Omit it and OpenProject "
                    "derives one from the name — the derived value is in the result. A slug "
                    "that is already taken comes back as a validation violation, not a "
                    "surprise rename."
                )
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Project description in markdown. Omit to leave it empty."),
        ] = None,
        parent_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric id or identifier of the parent project, to create a subproject. "
                    "Ids come from list_projects. Omit for a top-level project. Creating a "
                    "subproject needs the 'add subprojects' permission on the parent."
                )
            ),
        ] = None,
        public: Annotated[
            bool | None,
            Field(
                description=(
                    "True makes the project visible to every logged-in user without a "
                    "membership. Omit to take the instance default (normally private)."
                )
            ),
        ] = None,
        status_code: Annotated[
            ProjectStatusCode | None,
            Field(
                description=(
                    "Initial project status: on_track, at_risk, off_track, not_started, "
                    "finished or discontinued. These codes are the only accepted values — a "
                    "free-text status is rejected rather than silently dropped. Omit for no "
                    "status."
                )
            ),
        ] = None,
    ) -> ProjectDetail:
        """Create a project, validated through OpenProject's own form endpoint first.

        Use it for a new workspace or, with ``parent_id``, for a subproject of an existing
        one. The call runs ``POST /projects/form`` before committing, so a taken identifier,
        an unusable parent or a bad status comes back as ``violations`` naming the attribute
        instead of an opaque failure — and the identifier OpenProject derives from the name
        is used verbatim on the commit.

        Returns the created project: ``{id, identifier, name, active, public, parent,
        status_code, description, status_explanation, created_at, updated_at}``. Keep the
        ``id`` — every other tool's ``project_id`` accepts it, as does the ``identifier``.

        Pitfalls: creating projects usually requires the 'create project' permission or
        admin rights, so a 403 here is about the account, not the payload. The new project
        starts with the instance's default modules and types enabled — check
        ``get_project_metadata(project_id=...)`` before creating work packages in it.
        Members are not copied from the parent; add them with ``create_membership``.

        Cross-references: ``list_projects`` finds the parent id; ``update_project`` changes
        any of these fields afterwards; ``get_project_metadata`` lists the types, versions
        and categories valid inside the result.
        """
        ctx = get_tool_context()
        if not name or not name.strip():
            raise InputValidationError(
                "name is empty.",
                hint="Pass the display name of the project to create.",
            )

        attributes: dict[str, Any] = {"name": name.strip()}
        if identifier is not None:
            attributes["identifier"] = _validate_identifier(identifier)
        if description is not None:
            attributes["description"] = formattable_field(description)
        if public is not None:
            attributes["public"] = public

        links: dict[str, Any] = {}
        if parent_id is not None:
            links["parent"] = link(PROJECTS_RESOURCE, parent_id)
        if status_code is not None:
            links["status"] = link(PROJECT_STATUS_RESOURCE, status_code)

        payload = build_write_payload(attributes, links)
        form = await ctx.client.post_json("projects/form", json=payload)
        _raise_form_validation_errors(form)

        defaults = _form_section(form, "payload")
        body = _merge_form_payload(defaults, payload) if isinstance(defaults, Mapping) else payload
        created = await ctx.client.post_json("projects", json=body)
        return _project_detail(created)

    @mcp.tool(
        name="update_project",
        tags=tool_tags(GROUP_PROJECTS, WRITE),
        annotations=write_annotations(title="Update project"),
    )
    @tool_errors
    async def update_project(
        id_or_identifier: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric project id or URL identifier of the project to change; both are "
                    "accepted and come from list_projects or get_project."
                )
            ),
        ],
        name: Annotated[
            str | None,
            Field(description="New display name. Omit to leave the name alone."),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description=(
                    "New description in markdown; it REPLACES the existing text, so read the "
                    "current one with get_project first if you mean to extend it. Pass an "
                    "empty string to clear it. Omit to leave it alone."
                )
            ),
        ] = None,
        public: Annotated[
            bool | None,
            Field(
                description=(
                    "True publishes the project to every logged-in user, false makes it "
                    "members-only. Omit to leave the visibility alone."
                )
            ),
        ] = None,
        parent_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Move the project under another one: numeric id or identifier of the new "
                    "parent. Pass null to detach it and make it top-level. Omit the parameter "
                    "entirely (the default) to leave the hierarchy untouched."
                )
            ),
        ] = KEEP,
        active: Annotated[
            bool | None,
            Field(
                description=(
                    "false ARCHIVES the project — it disappears from normal listings and "
                    "everything in it becomes read-only, including its subprojects. true "
                    "restores it. Omit to leave it alone. Archiving is usually admin-only."
                )
            ),
        ] = None,
        status_code: Annotated[
            ProjectStatusCode | None,
            Field(
                description=(
                    "New project status: on_track, at_risk, off_track, not_started, finished "
                    "or discontinued. Only these codes are accepted. Omit to leave the "
                    "status alone."
                )
            ),
        ] = None,
        status_explanation: Annotated[
            str | None,
            Field(
                description=(
                    "Markdown note explaining the status, e.g. why the project is at risk. "
                    "Replaces the previous explanation; pass an empty string to clear it."
                )
            ),
        ] = None,
    ) -> ProjectDetail:
        """Change a project's name, description, visibility, parent, status or archived state.

        Use it to record a status change with its explanation ("at_risk because the vendor
        slipped"), to rename or re-parent a project, to publish it, or to archive it with
        ``active=false``. The change is validated through ``POST /projects/{id}/form`` first,
        so rejected values come back as ``violations`` naming the attribute.

        Only the parameters you pass are sent — omitted fields are never rewritten, so two
        agents editing different fields do not clobber each other. Projects carry no
        ``lockVersion`` upstream, so there is no version to echo and no lock parameter here.

        Returns the updated project in the same shape as ``get_project``.

        Pitfalls: ``description`` and ``status_explanation`` REPLACE the stored text rather
        than appending to it. ``active=false`` archives, which is not deletion but does hide
        the project and freeze its work packages. Changing ``identifier`` is deliberately not
        offered — it breaks every existing link to the project.

        Cross-references: ``get_project`` to read the current values first; ``delete_project``
        to remove a project for good; ``list_projects(active=false)`` to find archived ones.
        """
        ctx = get_tool_context()
        key = _project_key(id_or_identifier)

        attributes: dict[str, Any] = {}
        if name is not None:
            if not name.strip():
                raise InputValidationError(
                    "name is empty.",
                    hint="Omit name to leave it unchanged; a project cannot have a blank name.",
                )
            attributes["name"] = name.strip()
        if description is not None:
            attributes["description"] = formattable_field(description)
        if status_explanation is not None:
            attributes["statusExplanation"] = formattable_field(status_explanation)
        if public is not None:
            attributes["public"] = public
        if active is not None:
            attributes["active"] = active

        links: dict[str, Any] = {}
        if parent_id != KEEP:
            links["parent"] = link(PROJECTS_RESOURCE, parent_id)
        if status_code is not None:
            links["status"] = link(PROJECT_STATUS_RESOURCE, status_code)

        if not attributes and not links:
            raise InputValidationError(
                "update_project was called with nothing to change.",
                hint=(
                    "Pass at least one of name, description, public, parent_id, active, "
                    "status_code or status_explanation."
                ),
            )

        payload = build_write_payload(attributes, links)
        path = f"projects/{quote(key, safe='')}"
        try:
            form = await ctx.client.post_json(f"{path}/form", json=payload)
        except NotFoundError as exc:
            raise _with_not_found_hint(exc, key) from exc
        _raise_form_validation_errors(form)

        # The PATCH sends only what the caller asked for, never the form's echoed payload:
        # echoing it back would rewrite fields somebody else just changed.
        updated = await ctx.client.patch_json(path, json=payload)
        return _project_detail(updated)

    @mcp.tool(
        name="delete_project",
        tags=tool_tags(GROUP_PROJECTS, WRITE, DESTRUCTIVE),
        annotations=destructive_annotations(title="Delete project"),
    )
    @tool_errors
    async def delete_project(
        id_or_identifier: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric project id or URL identifier of the project to destroy. Read it "
                    "back with get_project first and show the user the name — an identifier "
                    "typo can point at a different project."
                )
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be true. Ask the user to confirm first: this is irreversible and "
                    "cascades. Calling with confirm=false returns a confirmation_required "
                    "error and deletes nothing."
                )
            ),
        ] = False,
    ) -> ProjectDeletionResult:
        """Schedule the permanent deletion of a project and everything inside it.

        Use only on an explicit, specific instruction. Deletion CASCADES: every subproject,
        work package, comment, attachment, time entry, version, wiki page and membership of
        this project goes with it, and OpenProject offers no API-side undo. If the goal is
        only to get the project out of the way, ``update_project(active=false)`` archives it
        instead — reversible, and it preserves the data.

        Deletion is ASYNCHRONOUS upstream: OpenProject accepts the request and runs it as a
        background job. This tool therefore returns ``{scheduled: true, job_id, message}``,
        never a claim that the project is already gone — for a large project the data
        disappears over minutes and ``get_project`` may still answer during that window.

        Pitfalls: deleting normally requires admin rights (403 otherwise). A 404 means the
        id or identifier is wrong, or the project was already deleted. Because the work runs
        in the background, a later failure inside the job is not visible here; confirm with
        ``get_project`` (it should eventually 404) rather than assuming success.

        Cross-references: ``get_project`` to check what you are about to destroy;
        ``list_projects(parent_id=...)`` to see the subprojects that would go with it;
        ``update_project(active=false)`` for the reversible alternative.
        """
        key = _project_key(id_or_identifier)
        require_confirmation(
            confirm,
            action="delete project",
            target=key,
            consequence=(
                "The project and every subproject, work package, comment, attachment, time "
                "entry and membership in it are removed permanently, and the deletion cannot "
                "be undone through the API."
            ),
        )
        ctx = get_tool_context()
        try:
            response = await ctx.client.delete(f"projects/{quote(key, safe='')}")
        except NotFoundError as exc:
            raise _with_not_found_hint(exc, key) from exc

        return ProjectDeletionResult(
            id=id_or_identifier,
            scheduled=True,
            job_id=_job_id(response),
            message=(
                f"Deletion of project {key!r} was scheduled. OpenProject removes projects in a "
                "background job, so it and its work packages may still be readable for a "
                "while; re-check with get_project."
            ),
        )
