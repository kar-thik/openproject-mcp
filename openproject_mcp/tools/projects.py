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
from openproject_mcp.tools import _forms
from openproject_mcp.tools._shared import (
    DESTRUCTIVE,
    FETCH_ALL_CAP,
    GROUP_PROJECTS,
    READ,
    WRITE,
    collect_all,
    destructive_annotations,
    envelope_from_collection,
    fetch_all_envelope,
    get_tool_context,
    read_annotations,
    require_confirmation,
    require_first_page_for_fetch_all,
    tool_errors,
    tool_tags,
    write_annotations,
)
from openproject_mcp.version_probe import PROJECT_FAVORITES_MIN, parse_version

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "KEEP",
    "JobStatusResult",
    "ProjectCopyResult",
    "ProjectDeletionResult",
    "ProjectDetail",
    "ProjectFavoriteResult",
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
    """Compact project row for list results."""

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
    workspace_type: str | None = Field(
        default=None,
        description=(
            "Workspace kind: 'project', 'program' or 'portfolio'. Pre-17 instances only have "
            "'project'; on 17.x project listings mix all three kinds, so check this before "
            "treating a row as a plain project."
        ),
    )


class ProjectDetail(ProjectRow):
    """Full project detail: the row plus text fields and timestamps."""

    description: str | None = Field(
        default=None, description="Description as markdown (raw); html is dropped."
    )
    status_explanation: str | None = Field(
        default=None, description="Free-text explanation of status_code, markdown (raw)."
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class PhaseDefinitionRow(BaseModel):
    """One instance-global project phase definition, with its gates."""

    id: int | str | None = Field(
        default=None, description="Definition id — the value list_projects' in_phase accepts."
    )
    name: str | None = Field(default=None, description="Phase name, e.g. 'Executing'.")
    start_gate: bool | None = Field(
        default=None, description="True when the phase begins with a gate."
    )
    start_gate_name: str | None = Field(
        default=None, description="Display name of the start gate; null without one."
    )
    finish_gate: bool | None = Field(
        default=None, description="True when the phase ends with a gate."
    )
    finish_gate_name: str | None = Field(
        default=None, description="Display name of the finish gate; null without one."
    )


class ProjectPhaseDetail(BaseModel):
    """One project's instance of a phase definition."""

    id: int | str | None = Field(
        default=None,
        description="Phase id — a per-project record id, not the definition id.",
    )
    name: str | None = Field(default=None, description="Phase name.")
    active: bool | None = Field(
        default=None, description="False when the project switched this phase off."
    )
    definition: Ref | None = Field(
        default=None, description="The instance-global phase definition this instantiates."
    )
    project: Ref | None = Field(default=None, description="Project this phase belongs to.")
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    notes: list[str] | None = Field(default=None, description="Degradation notes for this result.")


def _status_code(payload: Mapping[str, Any]) -> str | None:
    """Read the status code from ``_links.status`` (or the pre-13 ``status`` object)."""
    linked = hal.ref(payload, "status")
    if linked is not None and linked.id is not None:
        return str(linked.id)
    raw = payload.get("status")
    embedded_status = hal.as_object(raw)
    if embedded_status is not None:
        code = embedded_status.get("code")
        return code if isinstance(code, str) else None
    return raw if isinstance(raw, str) else None


def _workspace_type(payload: Mapping[str, Any]) -> str | None:
    """Lowercase the HAL ``_type`` (Project | Program | Portfolio, 17.x)."""
    raw = payload.get("_type")
    return raw.lower() if isinstance(raw, str) and raw else None


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
        workspace_type=_workspace_type(payload),
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


def _phase_definition_row(payload: Mapping[str, Any]) -> PhaseDefinitionRow:
    start_gate_name = payload.get("startGateName")
    finish_gate_name = payload.get("finishGateName")
    return PhaseDefinitionRow(
        id=hal.self_id(payload),
        name=payload.get("name"),
        start_gate=payload.get("startGate") if isinstance(payload.get("startGate"), bool) else None,
        start_gate_name=start_gate_name if isinstance(start_gate_name, str) else None,
        finish_gate=(
            payload.get("finishGate") if isinstance(payload.get("finishGate"), bool) else None
        ),
        finish_gate_name=finish_gate_name if isinstance(finish_gate_name, str) else None,
    )


def _project_phase_detail(payload: Mapping[str, Any]) -> ProjectPhaseDetail:
    return ProjectPhaseDetail(
        id=hal.self_id(payload),
        name=payload.get("name"),
        active=payload.get("active") if isinstance(payload.get("active"), bool) else None,
        definition=Ref.from_hal(payload, "definition"),
        project=Ref.from_hal(payload, "project"),
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
    )


#: The API renders phase names, activity and gates — never the phase dates.
PHASE_DATES_NOTE = (
    "The API does not expose phase dates; only the name, active flag and linked "
    "definition are readable here."
)

_PHASES_UNSUPPORTED_HINT = (
    "This OpenProject instance predates project phases (added in 16.1): the "
    "project_phase_definitions API is absent. No phase data exists to read."
)


def _resolve_phase_definition(
    definitions: list[PhaseDefinitionRow], requested: int | str
) -> int | str:
    """Turn an ``in_phase`` value into a definition id, or fail listing the options."""
    known = ", ".join(f"{row.id}: {row.name}" for row in definitions) or "none defined"
    key = str(requested).strip()
    if key.isdigit():
        if any(str(row.id) == key for row in definitions):
            ids = [row.id for row in definitions if str(row.id) == key]
            return ids[0] if ids[0] is not None else key
        raise InputValidationError(
            f"No phase definition with id {key}.",
            hint=f"This instance defines: {known}.",
        )
    matches = [
        row for row in definitions if (row.name or "").strip().lower() == key.lower() and row.id
    ]
    if len(matches) == 1:
        assert matches[0].id is not None
        return matches[0].id
    problem = "is ambiguous — pass the id" if matches else "matches no phase definition"
    raise InputValidationError(
        f"in_phase {requested!r} {problem}.",
        hint=f"This instance defines: {known}.",
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


def _project_form_hints(form: Mapping[str, Any], errors: Mapping[str, Any]) -> list[str]:
    """The project-specific half of a form rejection (SPEC §4.5).

    The form is asked first precisely so a taken identifier or an unknown parent
    comes back as an attribute-level violation instead of an opaque 422 on the
    commit; these hints say what a valid value would look like.
    """
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
    return hints


def _raise_form_validation_errors(form: Mapping[str, Any]) -> None:
    """Turn a project form's ``validationErrors`` into a typed error (SPEC §4.5)."""
    _forms.raise_validation_errors(
        form,
        subject="project",
        hints=_project_form_hints,
        fallback_hint=(
            "Fix the attributes listed in 'violations'. list_projects shows the projects and "
            "identifiers that already exist."
        ),
    )


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


# --- Phase 3: copy, background jobs, favorites (SPEC §6.6, §4.7) ----------

#: ``_meta`` keys of the copy payload. Only these two are exposed as tool
#: parameters; every other copy flag keeps whatever default the copy form filled
#: in, which is why the form's ``_meta`` is merged instead of replaced.
COPY_WORK_PACKAGES_META = "copyWorkPackages"
COPY_NOTIFICATIONS_META = "sendNotifications"

#: Job states that mean the job is over. Anything else is still running, so
#: ``finished`` is derived from this set rather than assumed from a 200.
JOB_TERMINAL_STATUSES: frozenset[str] = frozenset({"success", "failure", "error", "cancelled"})
#: The terminal states that mean it did not work.
JOB_FAILED_STATUSES: frozenset[str] = frozenset({"failure", "error", "cancelled"})

COPY_POLL_NOTE = (
    "The copy runs as a background job: this is the job's INITIAL state, not a finished copy. "
    "Poll get_job_status(job_id=...) until status is 'success' or 'failure' before telling the "
    "user the new project exists."
)
COPY_NO_JOB_NOTE = (
    "This instance accepted the copy but reported no job id, so it cannot be polled. Watch for "
    "the new project with list_projects(search=<new_name>) instead of assuming it appeared."
)
JOB_DERIVED_PROJECT_NOTE = (
    "'project' was derived from the URL the job stored, not from a link OpenProject rendered — "
    "confirm it with get_project before using the id anywhere else."
)

#: A finished copy/export job stores a UI URL rather than an API link, so the
#: project it produced is read out of that URL when no link names it.
_PROJECT_URL_RE = re.compile(r"/projects/(?P<key>[^/?#]+)")


class ProjectCopyResult(BaseModel):
    """Outcome of ``copy_project`` — a *started* job, never a finished copy."""

    source: int | str = Field(description="The project id or identifier that was copied.")
    new_name: str = Field(description="Name requested for the copy.")
    scheduled: bool = Field(
        description="True once OpenProject accepted the request. The copy itself runs in the "
        "background and is not finished when this is true."
    )
    job_id: str | None = Field(
        default=None,
        description="Background job id — pass it to get_job_status. Null when the instance "
        "reported none; 'notes' then says what to do instead.",
    )
    status: str | None = Field(
        default=None,
        description="Job state at the moment the copy was accepted, normally 'in_queue'. "
        "Null when the instance reported none.",
    )
    message: str | None = Field(
        default=None, description="Message the job reported, when it carried one."
    )
    project: Ref | None = Field(
        default=None,
        description="The copy itself ({id, name}) — only populated on the rare instance that "
        "finishes the job before answering. Normally null: read it from get_job_status.",
    )
    notes: list[str] = Field(
        default_factory=list[str],
        description="What is still outstanding. Always says the copy has to be polled.",
    )


class JobStatusResult(BaseModel):
    """One background job as ``GET /job_statuses/{uuid}`` reports it."""

    id: str | None = Field(default=None, description="Job id (a uuid) this status belongs to.")
    status: str | None = Field(
        default=None,
        description="Job state: 'in_queue' or 'in_process' while it runs, 'success', 'failure', "
        "'error' or 'cancelled' once it is over.",
    )
    finished: bool = Field(
        description="True once status is terminal. False means the job is still running — "
        "poll again rather than reporting a result."
    )
    successful: bool | None = Field(
        default=None,
        description="True when the job finished successfully, false when it failed, null while "
        "it is still running. Never guess from 'finished' alone.",
    )
    message: str | None = Field(
        default=None, description="What the job reported, e.g. why it failed."
    )
    project: Ref | None = Field(
        default=None,
        description="Project the job produced or acted on ({id, name}), when it names one — "
        "this is how a finished copy_project job hands back the new project.",
    )
    result_url: str | None = Field(
        default=None,
        description="Web URL the job stored for its result (the new project, an export "
        "download). A UI URL, not an API endpoint.",
    )
    notes: list[str] = Field(
        default_factory=list[str],
        description="Degradation markers: still running, or a reference that had to be derived.",
    )


class ProjectFavoriteResult(BaseModel):
    """Outcome of ``set_project_favorite`` (OpenProject >= 17)."""

    id: int | str = Field(description="Project id or identifier that was changed.")
    favorite: bool = Field(
        description="The state now in effect: true when the project was favorited, false when "
        "the favorite was removed."
    )
    message: str = Field(description="Human-readable confirmation.")


def _copy_meta(include_work_packages: bool, notify: bool) -> dict[str, Any]:
    """The two ``_meta`` flags ``copy_project`` exposes."""
    return {
        COPY_WORK_PACKAGES_META: include_work_packages,
        COPY_NOTIFICATIONS_META: notify,
    }


def _merged_copy_meta(
    form_payload: Mapping[str, Any] | None, meta: Mapping[str, Any]
) -> dict[str, Any]:
    """Our copy flags on top of the ones the form defaulted.

    ``merge_form_payload`` replaces whole keys, which would drop every copy flag
    the form filled in (members, versions, wiki, …) and silently narrow the copy.
    The ``_meta`` block is therefore merged key by key instead.
    """
    echoed = hal.as_object(form_payload.get("_meta")) if form_payload is not None else None
    merged: dict[str, Any] = dict(echoed) if echoed is not None else {}
    merged.update(meta)
    return merged


def _job_payload(document: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The job's stored payload, which is where a finished job keeps its result."""
    return hal.as_object(document.get("payload"))


def _job_result_url(document: Mapping[str, Any]) -> str | None:
    """The URL a finished job points at, from its payload or its links."""
    payload = _job_payload(document)
    if payload is not None:
        for key in ("redirect", "url", "link", "download"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("redirect", "download"):
        resolved = hal.ref(document, key)
        if resolved is not None and resolved.href:
            return resolved.href
    return None


def _job_project(
    document: Mapping[str, Any], result_url: str | None
) -> tuple[Ref | None, str | None]:
    """The project a job produced: from the link it rendered, else from its URL.

    A finished copy job keeps the new project *inside* its payload
    (``payload._links.project``, carrying the numeric id and the name) next to
    the UI URL it redirects to; the job document itself renders no project link
    at the root. The root is still read as a fallback, and the URL is the last
    resort — it only yields the identifier slug and no name, which is what
    :data:`JOB_DERIVED_PROJECT_NOTE` warns about.
    """
    linked = Ref.from_hal(_job_payload(document), "project") or Ref.from_hal(document, "project")
    if linked is not None:
        return linked, None
    if result_url is None:
        return None, None
    match = _PROJECT_URL_RE.search(result_url)
    if match is None:
        return None, None
    return Ref(id=match.group("key")), JOB_DERIVED_PROJECT_NOTE


def _job_status(payload: Mapping[str, Any], *, requested_id: str | None = None) -> JobStatusResult:
    """Project a JobStatus document, deriving nothing the document does not say."""
    raw_status = payload.get("status")
    status = raw_status if isinstance(raw_status, str) else None
    result_url = _job_result_url(payload)
    project, derived_note = _job_project(payload, result_url)

    finished = status in JOB_TERMINAL_STATUSES if status is not None else False
    successful = None if not finished or status is None else status not in JOB_FAILED_STATUSES

    notes: list[str] = []
    if status is None:
        notes.append(
            "this instance reported no status for the job, so whether it finished is unknown — "
            "verify the result itself (get_project, list_projects) instead"
        )
    elif not finished:
        notes.append(
            f"the job is still {status}: nothing it produces exists yet. Poll get_job_status "
            "again in a few seconds."
        )
    if derived_note:
        notes.append(derived_note)

    return JobStatusResult(
        id=_job_id(payload) or requested_id,
        status=status,
        finished=finished,
        successful=successful,
        message=hal.formattable(payload.get("message")),
        project=project,
        result_url=result_url,
        notes=notes,
    )


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
        in_phase: Annotated[
            int | str | None,
            Field(
                description=(
                    "Restrict to projects whose named phase covers a date (today unless "
                    "phase_on_date says otherwise). Accepts a definition id or name from "
                    "list_project_phase_definitions. Requires OpenProject 16.1+."
                )
            ),
        ] = None,
        phase_on_date: Annotated[
            str | None,
            Field(
                description=(
                    "ISO date (YYYY-MM-DD) the in_phase filter should test instead of today. "
                    "Only valid together with in_phase."
                )
            ),
        ] = None,
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
        fetch_all: Annotated[
            bool,
            Field(
                description=(
                    "Aggregate every page into one result instead of returning page 1. "
                    f"Capped at {FETCH_ALL_CAP} items with a note when the cap bites; "
                    "mutually exclusive with page."
                )
            ),
        ] = False,
    ) -> ListEnvelope[ProjectRow]:
        """List projects, filtered server-side, one page at a time.

        Use this to turn a project name into the id or identifier that every other tool
        consumes, to enumerate the sub-projects of a parent, or to review which projects are
        off track. It is the id-producing path for every ``project_id`` parameter in this
        server.

        Returns the standard list envelope: ``items`` of
        ``{id, identifier, name, active, public, parent, status_code, workspace_type}`` plus
        ``pagination`` with ``total``/``page``/``page_size``/``has_more``. Nothing is
        truncated silently — page explicitly until ``has_more`` is false.

        Pitfalls: ``search`` matches name and identifier only (not descriptions);
        ``parent_id`` returns direct children, so a deep hierarchy needs one call per level;
        ``status_code`` is a code such as ``on_track``, never a translated label. On
        OpenProject 17.x this listing deliberately mixes plain projects with programs and
        portfolios — ``workspace_type`` says which each row is. ``in_phase`` tests phase
        *dates* ("which projects are in Executing today"), so projects whose phases carry
        no dates never match it.

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
        if phase_on_date is not None and in_phase is None:
            raise InputValidationError(
                "phase_on_date is only valid together with in_phase.",
                hint="Pass in_phase (a definition id or name) to say WHICH phase to test.",
            )
        if in_phase is not None:
            # Dynamic wire name (project_phase_<definition id>) — built directly, like the
            # meetings time filter, because the registry only knows static names.
            try:
                definitions_payload = await ctx.client.get_json("project_phase_definitions")
            except NotFoundError as exc:
                raise ValidationFailedError(
                    "This instance has no project-phase API.",
                    http_status=exc.http_status,
                    hint=_PHASES_UNSUPPORTED_HINT,
                ) from exc
            definitions = [
                _phase_definition_row(element) for element in hal.collection(definitions_payload)
            ]
            definition_id = _resolve_phase_definition(definitions, in_phase)
            filters.append(
                Filter(
                    name=f"project_phase_{definition_id}",
                    operator=Op.ON_DATE.value if phase_on_date else Op.TODAY.value,
                    values=[phase_on_date] if phase_on_date else [],
                )
            )

        require_first_page_for_fetch_all(fetch_all, page)

        async def get_page(fetch_page: int, fetch_size: int) -> Mapping[str, Any]:
            return await ctx.client.get_json(
                "projects",
                params=query_params(
                    filters=filters,
                    page=fetch_page,
                    page_size=fetch_size,
                    sort_by=sort_by,
                    sort_keys=PROJECT_SORT_KEYS,
                ),
            )

        try:
            if fetch_all:
                elements, last_chunk, cap_notes = await collect_all(
                    get_page, label="fetching all projects"
                )
                return fetch_all_envelope(
                    [_project_row(element) for element in elements],
                    last_chunk,
                    notes=cap_notes,
                )
            payload = await get_page(page, page_size)
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

        Returns ``{id, identifier, name, active, public, parent, status_code,
        workspace_type, description, status_explanation, created_at, updated_at}``. Rich
        text comes back as markdown ``raw``; html is dropped.

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

        body = _forms.merge_form_payload(_forms.form_payload(form) or {}, payload)
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

    @mcp.tool(
        name="copy_project",
        tags=tool_tags(GROUP_PROJECTS, WRITE),
        annotations=write_annotations(title="Copy project"),
    )
    @tool_errors
    async def copy_project(
        id_or_identifier: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric id or URL identifier of the project to copy (the TEMPLATE, not the "
                    "copy). Both are accepted and come from list_projects or get_project."
                )
            ),
        ],
        new_name: Annotated[
            str,
            Field(
                description=(
                    "Display name of the copy, e.g. 'Apollo migration (2027)'. OpenProject "
                    "derives the copy's URL identifier from it; the derived value is reported by "
                    "get_job_status once the job succeeds."
                )
            ),
        ],
        include_work_packages: Annotated[
            bool,
            Field(
                description=(
                    "Copy the template's work packages too (default). false copies the project "
                    "shell — members, versions, categories, wiki and the other settings the "
                    "instance defaults to — without any tickets."
                )
            ),
        ] = True,
        notify: Annotated[
            bool,
            Field(
                description=(
                    "Send OpenProject notification emails for everything the copy creates. "
                    "false (default) keeps a large copy quiet; the copy itself is identical."
                )
            ),
        ] = False,
    ) -> ProjectCopyResult:
        """Copy a project — its settings, and optionally its work packages — into a new one.

        Use it to spin a new engagement or release off a template project, which is the only
        way to reproduce a project's members, versions, categories and enabled modules in one
        call. The request goes through ``POST /projects/{id}/copy/form`` first, so a name that
        derives a taken identifier comes back as ``violations`` naming the attribute instead of
        a failed background job.

        Copying is ASYNCHRONOUS: OpenProject queues a job and answers immediately. This tool
        therefore returns ``{scheduled: true, job_id, status, message, notes}`` and NEVER
        claims the copy exists — a large project takes minutes. Poll
        ``get_job_status(job_id=...)`` until ``status`` is 'success' (it then reports the new
        project) or 'failure'.

        Pitfalls: only ``include_work_packages`` and ``notify`` are exposed; every other copy
        flag (members, versions, wiki, boards, file links) keeps this instance's own default,
        which the form fills in — so the copy can contain more than the two parameters
        suggest. A 403 means the account may not copy this project (it needs the 'copy project'
        permission on the template plus the right to create projects). Work packages come
        across with their relations, but time entries and comment histories do not.

        Cross-references: ``get_job_status`` follows the job to completion; ``list_projects``
        or ``get_project`` confirms the result; ``create_project`` makes an empty project
        instead; ``update_project`` renames the copy afterwards.
        """
        ctx = get_tool_context()
        key = _project_key(id_or_identifier)
        if not new_name or not new_name.strip():
            raise InputValidationError(
                "new_name is empty.",
                hint="Pass the display name for the copy, e.g. 'Apollo migration (2027)'.",
            )

        meta = _copy_meta(include_work_packages, notify)
        payload = build_write_payload({"name": new_name.strip(), "_meta": meta})

        path = f"projects/{quote(key, safe='')}/copy"
        try:
            form = await ctx.client.post_json(f"{path}/form", json=payload)
        except NotFoundError as exc:
            raise _with_not_found_hint(exc, key) from exc
        _raise_form_validation_errors(form)

        echoed = _forms.form_payload(form)
        body = _forms.merge_form_payload(echoed or {}, payload)
        body["_meta"] = _merged_copy_meta(echoed, meta)

        accepted = await ctx.client.post_json(path, json=body)
        job = _job_status(accepted)

        notes = [COPY_POLL_NOTE]
        if job.id is None:
            notes.append(COPY_NO_JOB_NOTE)
        # The job's own "still queued" note would only repeat COPY_POLL_NOTE; the
        # derivation caveat is the one thing a caller could not infer.
        notes.extend(note for note in job.notes if note == JOB_DERIVED_PROJECT_NOTE)

        return ProjectCopyResult(
            source=id_or_identifier,
            new_name=new_name.strip(),
            scheduled=True,
            job_id=job.id,
            status=job.status,
            message=job.message,
            project=job.project,
            notes=notes,
        )

    @mcp.tool(
        name="get_job_status",
        tags=tool_tags(GROUP_PROJECTS, READ),
        annotations=read_annotations(title="Get background job status"),
    )
    @tool_errors
    async def get_job_status(
        job_id: Annotated[
            str,
            Field(
                description=(
                    "Background job id — a uuid such as "
                    "'9f4c1d5e-0e2a-4f2b-9a11-2f1b3c4d5e6f'. It comes from copy_project or "
                    "delete_project ('job_id' in their results), never from a project id."
                )
            ),
        ],
    ) -> JobStatusResult:
        """Check whether a background job (a project copy, a scheduled deletion) has finished.

        OpenProject runs copies, deletions and exports asynchronously and hands back a job id.
        This is the only way to learn what happened to one: call it after ``copy_project`` or
        ``delete_project`` and wait for a terminal state before reporting an outcome to the
        user.

        Returns ``{id, status, finished, successful, message, project, result_url, notes}``.
        ``status`` is 'in_queue' or 'in_process' while the job runs and 'success', 'failure',
        'error' or 'cancelled' once it is over; ``finished`` and ``successful`` are derived
        from it, and ``successful`` stays null while the job runs rather than defaulting to
        false. A finished copy reports the new project in ``project`` and the URL it lives at
        in ``result_url``.

        Pitfalls: a 200 does not mean the job worked — read ``status``. Polling is on you:
        wait a few seconds between calls rather than looping tightly. OpenProject drops job
        statuses after a while, so a 404 can mean 'long finished' as easily as 'wrong id';
        confirm with ``get_project`` or ``list_projects``. When a job fails, ``message`` is
        what OpenProject recorded — there is no API to retry it, so the underlying tool has to
        be called again deliberately.

        Cross-references: ``copy_project`` and ``delete_project`` produce the ``job_id``;
        ``get_project`` / ``list_projects`` verify what the job actually did.
        """
        ctx = get_tool_context()
        key = str(job_id).strip()
        if not key:
            raise InputValidationError(
                "job_id must not be empty.",
                hint="Pass the 'job_id' from copy_project or delete_project.",
            )
        try:
            payload = await ctx.client.get_json(f"job_statuses/{quote(key, safe='')}")
        except NotFoundError as exc:
            raise NotFoundError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=(
                    f"No background job with id {key!r}. Job ids come from copy_project or "
                    "delete_project and are uuids, not project ids. OpenProject also discards "
                    "job statuses after a while, so a job that finished long ago answers 404 — "
                    "check the outcome directly with get_project or list_projects."
                ),
            ) from exc
        return _job_status(payload, requested_id=key)

    @mcp.tool(
        name="set_project_favorite",
        tags=tool_tags(GROUP_PROJECTS, WRITE),
        annotations=write_annotations(title="Set project favorite", idempotent=True),
    )
    @tool_errors
    async def set_project_favorite(
        id_or_identifier: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric project id or URL identifier to favorite or un-favorite; both are "
                    "accepted and come from list_projects or get_project."
                )
            ),
        ],
        favorite: Annotated[
            bool,
            Field(
                description=(
                    "true adds the project to the authenticated user's favorites, false removes "
                    "it. Required — there is no toggle, so read the current state with "
                    "list_projects(favorites_only=true) if you do not know it."
                )
            ),
        ],
    ) -> ProjectFavoriteResult:
        """Add or remove a project from the authenticated user's favorites (OpenProject 17+).

        Favorites are per user, not per project: this changes what the account behind
        OPENPROJECT_API_KEY sees starred on its own overview page, and nothing about the
        project itself or about anybody else's view. Use it when the user asks to pin, star or
        favorite a project they work in.

        Returns ``{id, favorite, message}`` — ``favorite`` is the state now in effect. The call
        is idempotent: favoriting an already-favorited project succeeds again.

        Pitfalls: this endpoint only exists from OpenProject 17. When the instance reports a
        version older than that the call is REFUSED before anything is sent, with the detected
        version in the message, because there is no downgrade that would achieve the same
        thing — favorite the project in the web UI instead. When it reports no version at all
        the request IS sent, since an unreported version says nothing about the endpoint. A 404
        is ambiguous on purpose in the hint: it means either the project does not exist for
        this account or the endpoint is missing. This is not project 'status' and not a
        work-package watcher.

        Cross-references: ``list_projects(favorites_only=true)`` lists the current favorites
        (and works on older instances too); ``get_project`` resolves an identifier first;
        ``get_instance_info`` reports the detected OpenProject version.
        """
        ctx = get_tool_context()
        key = _project_key(id_or_identifier)

        probe = await ctx.probe()
        # Refuse only what is *known* to be too old. An instance that reports no
        # version at all is not evidence the endpoint is missing, so the request
        # goes out and the 404 handler below reports what actually happened (G5).
        detected = parse_version(probe.core_version)
        if detected is not None and detected < PROJECT_FAVORITES_MIN:
            raise InputValidationError(
                f"Project favorites require OpenProject 17; this instance reports "
                f"{probe.core_version}.",
                hint=(
                    "POST/DELETE /projects/{id}/favorite does not exist before OpenProject 17, "
                    "so nothing was changed rather than being silently dropped. Favorite the "
                    "project in the web UI, or upgrade the instance. "
                    "list_projects(favorites_only=true) still reads existing favorites, and "
                    "get_instance_info reports the detected version."
                ),
            )

        path = f"projects/{quote(key, safe='')}/favorite"
        try:
            if favorite:
                # The empty JSON body is load-bearing — OpenProject answers any
                # bodyless POST with 406 "Missing content-type header"; DELETE
                # is accepted without one.
                await ctx.client.request("POST", path, json={})
            else:
                await ctx.client.request("DELETE", path)
        except NotFoundError as exc:
            raise NotFoundError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=(
                    f"Either no project matches {key!r} for this account, or this instance does "
                    f"not expose /projects/{{id}}/favorite even though it reports "
                    f"{probe.core_version or 'no version'} (the endpoint arrived in OpenProject "
                    "17 and can be disabled). Nothing was changed. Check the project with "
                    "get_project, and set the favorite in the web UI if the endpoint is absent."
                ),
            ) from exc

        state = "is now a favorite" if favorite else "is no longer a favorite"
        return ProjectFavoriteResult(
            id=id_or_identifier,
            favorite=favorite,
            message=f"Project {key!r} {state} of the authenticated user.",
        )

    @mcp.tool(
        name="list_project_phase_definitions",
        tags=tool_tags(GROUP_PROJECTS, READ),
        annotations=read_annotations(title="List project phase definitions"),
    )
    @tool_errors
    async def list_project_phase_definitions(
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_PAGE_SIZE,
                description=(
                    f"Records per page (max {MAX_PAGE_SIZE}); most instances define fewer "
                    "than a page of phases."
                ),
            ),
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[PhaseDefinitionRow]:
        """List the instance-global phase definitions — the vocabulary of the project life cycle.

        Use it to learn which phases (Initiating, Planning, …) and gates this instance
        defines, and to get the definition id or name that ``list_projects(in_phase=...)``
        accepts.

        Returns the standard list envelope of ``{id, name, start_gate, start_gate_name,
        finish_gate, finish_gate_name}`` rows. Gates are the checkpoints a phase can begin
        or end with; a definition without gates has both flags false.

        Pitfalls: definitions are the instance-wide *catalog*, not any project's actual
        phases — a project may deactivate phases or set no dates. Phase *dates* are not
        exposed by the API at all. Requires OpenProject 16.1+ and the
        ``view_project_phases`` permission in at least one project; older instances 404
        (the error hint says so).

        Cross-references: ``list_projects(in_phase=...)`` to find the projects a phase
        covers today; ``get_project_phase`` for one project's phase record (its id comes
        from a work package's ``project_phase``).
        """
        ctx = get_tool_context()
        params = query_params(page=page, page_size=page_size)
        try:
            payload = await ctx.client.get_json("project_phase_definitions", params=params)
        except NotFoundError as exc:
            raise NotFoundError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=_PHASES_UNSUPPORTED_HINT,
            ) from exc
        unwrapped = hal.collection(payload)
        rows = [_phase_definition_row(element) for element in unwrapped]
        return envelope_from_collection(unwrapped, rows, page=page, page_size=page_size)

    @mcp.tool(
        name="get_project_phase",
        tags=tool_tags(GROUP_PROJECTS, READ),
        annotations=read_annotations(title="Get project phase"),
    )
    @tool_errors
    async def get_project_phase(
        phase_id: Annotated[
            int,
            Field(
                description=(
                    "Per-project phase record id, from a work package's project_phase "
                    "reference. NOT a definition id from list_project_phase_definitions."
                )
            ),
        ],
    ) -> ProjectPhaseDetail:
        """Read one project's phase record: name, active flag and its definition.

        Use it after ``get_work_package`` surfaced a ``project_phase`` reference and you
        need to know which project and definition that phase belongs to, or whether it is
        still active.

        Returns ``{id, name, active, definition, project, created_at, updated_at}``.

        Pitfalls: the API has no phases index — ids only come from work packages'
        ``project_phase`` references. Phase dates are not exposed by the API (the ``notes``
        say so), so "which projects are in this phase now" goes through
        ``list_projects(in_phase=...)`` instead. A 404 covers a wrong id, a phase invisible
        to this user (``view_project_phases``), and instances that predate project phases
        (16.1).

        Cross-references: ``list_project_phase_definitions`` for the instance-wide catalog
        and gates; ``list_projects(in_phase=...)`` for date-based phase queries.
        """
        ctx = get_tool_context()
        try:
            payload = await ctx.client.get_json(f"project_phases/{phase_id}")
        except NotFoundError as exc:
            raise NotFoundError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=(
                    f"No phase record {phase_id} is visible to this account. Phase ids come "
                    "from a work package's project_phase reference — there is no phases "
                    "index. The 404 also covers missing view_project_phases permission and "
                    "instances that predate project phases (16.1)."
                ),
            ) from exc
        detail = _project_phase_detail(payload)
        detail.notes = [PHASE_DATES_NOTE]
        return detail
