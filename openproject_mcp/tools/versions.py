"""Version and sprint tools (SPEC §6.10).

Lands here:

=========================  ======  ====================================================
Tool                       Phase   Endpoint(s)
=========================  ======  ====================================================
🔍 ``list_versions``       2       ``GET /versions`` or ``/projects/{id}/versions``
                                   (+ ``/projects/{id}/sprints`` Ⓜ backlogs)
✏️ ``create_version``      2       form → ``POST /versions``
✏️ ``update_version``      2       form → ``PATCH /versions/{id}``
🗑 ``delete_version``      2       ``DELETE /versions/{id}``
=========================  ======  ====================================================

Non-negotiables for this module:

* ``end_date`` maps to the wire attribute ``endDate``. The old server's tool and
  client disagreed on the name and the value vanished silently — the protocol
  tests assert the exact wire key on both create and update (defect ledger §17).
* Versions carry **no** ``lockVersion``: the update is a plain PATCH after the
  form validated it, never a lock echo (a fabricated ``lockVersion: 0`` is worse
  than none, SPEC §4.4).
* Sprints come from the backlogs module. A 404 degrades into a ``notes`` marker
  saying the module is not installed, never into a failed call (G5).
* Deleting a version that work packages still point at fails with a 422; the
  hint says how to find and unassign them instead of just repeating the message.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
    violations_from_form,
)
from openproject_mcp.client.payloads import build_write_payload, formattable_field, link
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools._shared import (
    DESTRUCTIVE,
    GROUP_VERSIONS,
    READ,
    WRITE,
    build_envelope,
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
    "VersionDeletionResult",
    "VersionDetail",
    "VersionRow",
    "register",
]

#: Sentinel for update parameters that can be *cleared* as well as left alone.
KEEP = "__unchanged__"

#: Versions are a small, fetched-in-full collection (SPEC §9.3); this is the
#: ceiling for the instance-wide listing, which is the only paginated one.
VERSIONS_PAGE_SIZE = 100

VersionStatus = Literal["open", "locked", "closed"]
VersionSharing = Literal["none", "descendants", "hierarchy", "tree", "system"]

VERSION_STATUSES: tuple[str, ...] = ("open", "locked", "closed")
VERSION_SHARINGS: tuple[str, ...] = ("none", "descendants", "hierarchy", "tree", "system")

BACKLOGS_MISSING_NOTE = (
    "sprints not included: this instance or project does not expose /projects/{id}/sprints "
    "(the backlogs module is not installed or not enabled here)"
)
BACKLOGS_FORBIDDEN_NOTE = (
    "sprints not included: no permission to read the backlogs sprints of this project"
)


class VersionRow(BaseModel):
    """One version (or backlogs sprint) as list results return it."""

    id: int | str | None = Field(
        default=None,
        description="Version id — what update_version, delete_version and the work-package "
        "'version' field consume.",
    )
    name: str | None = Field(default=None, description="Version name, e.g. 'Sprint 12' or '2.1'.")
    project: Ref | None = Field(
        default=None,
        description="The project that DEFINES the version. A version shared from a parent "
        "project shows that parent here, not the project you asked about.",
    )
    status: str | None = Field(
        default=None,
        description="open, locked or closed. Locked and closed versions reject new work "
        "package assignments.",
    )
    start_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD).")
    end_date: str | None = Field(
        default=None, description="ISO date (YYYY-MM-DD); the version's finish date."
    )
    description: str | None = Field(
        default=None, description="Description as markdown (raw); html is dropped."
    )
    sharing: str | None = Field(
        default=None,
        description="How far the version is shared: none, descendants, hierarchy, tree, system.",
    )
    source: Literal["version", "sprint"] = Field(
        default="version",
        description="'sprint' when the row came from the backlogs sprints endpoint, "
        "'version' otherwise. Sprints ARE versions upstream, so a row can be both and is "
        "then reported as 'sprint'.",
    )


class VersionDetail(VersionRow):
    """A single version as the write tools return it."""

    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class VersionDeletionResult(BaseModel):
    """Outcome of ``delete_version``."""

    id: int = Field(description="Id of the version that was deleted.")
    deleted: bool = Field(description="True once OpenProject accepted the deletion.")
    message: str = Field(description="Human-readable confirmation.")


# --- projections ----------------------------------------------------------


def _version_row(payload: Mapping[str, Any], *, source: str = "version") -> VersionRow:
    project = Ref.from_hal(payload, "definingProject") or Ref.from_hal(payload, "project")
    status = payload.get("status")
    sharing = payload.get("sharing")
    return VersionRow(
        id=hal.self_id(payload),
        name=payload.get("name"),
        project=project,
        status=status if isinstance(status, str) else None,
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        description=hal.formattable(payload.get("description")),
        sharing=sharing if isinstance(sharing, str) else None,
        source="sprint" if source == "sprint" else "version",
    )


def _version_detail(payload: Mapping[str, Any]) -> VersionDetail:
    row = _version_row(payload)
    return VersionDetail(
        **row.model_dump(),
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
    )


# --- form flow (SPEC §4.5) ------------------------------------------------


def _form_section(form: Mapping[str, Any], key: str) -> Any:
    inner = form.get("_embedded")
    return inner.get(key) if isinstance(inner, Mapping) else None


def _raise_form_validation_errors(form: Mapping[str, Any]) -> None:
    """Turn a version form's ``validationErrors`` into a typed error with a usable hint."""
    errors = _form_section(form, "validationErrors")
    if not isinstance(errors, Mapping) or not errors:
        return

    violations = violations_from_form(errors)
    hints: list[str] = []
    if "name" in errors:
        hints.append(
            "Version names must be unique within the defining project; list_versions shows "
            "the ones already taken."
        )
    if "endDate" in errors or "startDate" in errors:
        hints.append("Dates are ISO YYYY-MM-DD and end_date must not precede start_date.")
    if "sharing" in errors:
        hints.append(f"sharing must be one of: {', '.join(VERSION_SHARINGS)}.")
    if "status" in errors:
        hints.append(f"status must be one of: {', '.join(VERSION_STATUSES)}.")
    if not hints:
        hints.append(
            "Fix the attributes listed in 'violations'; list_versions(project_id=...) shows "
            "what already exists in this project."
        )

    identifier: str | None = None
    first = next((value for value in errors.values() if isinstance(value, Mapping)), None)
    if first is not None and isinstance(first.get("errorIdentifier"), str):
        identifier = first["errorIdentifier"]

    raise ValidationFailedError(
        violations[0]["message"] if violations else "OpenProject rejected the version.",
        http_status=422,
        error_identifier=identifier,
        hint=" ".join(hints),
        violations=violations,
    )


def _links_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    links = payload.get("_links")
    return dict(links) if isinstance(links, Mapping) else {}


def _merge_form_payload(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the form's defaulted payload with ours; ours wins, ``_links`` merge."""
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


# --- input helpers --------------------------------------------------------


def _iso_date(value: str, field_name: str) -> str:
    """Validate an ISO date locally so a typo never costs a round trip (G2)."""
    candidate = value.strip()
    try:
        dt.date.fromisoformat(candidate)
    except ValueError as exc:
        raise InputValidationError(
            f"{field_name}={value!r} is not an ISO date.",
            hint=f"{field_name} must be YYYY-MM-DD, e.g. '2026-09-30'.",
        ) from exc
    return candidate


def _date_attribute(value: str | None, field_name: str) -> str | None:
    """Map a KEEP-sentinel date parameter to its wire value (``None`` clears it)."""
    if value is None:
        return None
    return _iso_date(value, field_name)


def _version_not_found(exc: NotFoundError, version_id: int) -> NotFoundError:
    return NotFoundError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=(
            f"No version with id {version_id}. Version ids come from "
            "list_versions(project_id=...) or get_project_metadata(project_id=...).versions — "
            "not from the version name and not from a sprint number."
        ),
    )


async def _sprint_rows(
    project_id: int | str,
) -> tuple[list[VersionRow], str | None]:
    """Fetch backlogs sprints for a project; a missing module degrades to a note (G5)."""
    ctx = get_tool_context()
    try:
        payload = await ctx.client.get_json(f"projects/{project_id}/sprints")
    except NotFoundError:
        return [], BACKLOGS_MISSING_NOTE
    except PermissionDeniedError:
        return [], BACKLOGS_FORBIDDEN_NOTE
    return [
        _version_row(element, source="sprint") for element in hal.collection(payload).elements
    ], None


def _merge_sprints(rows: Sequence[VersionRow], sprints: Sequence[VersionRow]) -> list[VersionRow]:
    """Merge sprint rows into the version rows, marking (not duplicating) overlaps."""
    merged = list(rows)
    by_id = {row.id: row for row in merged if row.id is not None}
    for sprint in sprints:
        existing = by_id.get(sprint.id) if sprint.id is not None else None
        if existing is not None:
            existing.source = "sprint"
            continue
        merged.append(sprint)
        if sprint.id is not None:
            by_id[sprint.id] = sprint
    return merged


def register(mcp: FastMCP) -> None:
    """Register the version and sprint tools (SPEC §6.10)."""

    @mcp.tool(
        name="list_versions",
        tags=tool_tags(GROUP_VERSIONS, READ),
        annotations=read_annotations(title="List versions"),
    )
    @tool_errors
    async def list_versions(
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric project id or URL identifier to list the versions available in "
                    "that project — including the ones shared into it from a parent. Omit it "
                    "to list every version visible to you across the instance."
                )
            ),
        ] = None,
        include_sprints: Annotated[
            bool,
            Field(
                description=(
                    "Also read /projects/{id}/sprints from the backlogs module and merge the "
                    "rows in with source='sprint'. Requires project_id. If backlogs is not "
                    "installed the versions are still returned and 'notes' says why sprints "
                    "are missing — the call does not fail."
                )
            ),
        ] = False,
    ) -> ListEnvelope[VersionRow]:
        """List versions (releases, milestones, sprints) you can assign work packages to.

        Use it to turn "Sprint 12" or "release 2.1" into the version id that
        ``create_work_package``/``update_work_package`` need, to see which versions are still
        open, or to review a release plan's dates. With ``project_id`` it answers "what can I
        target in THIS project", which includes versions shared down from parent projects.

        Returns the standard list envelope: ``items`` of ``{id, name, project, status,
        start_date, end_date, description, sharing, source}`` plus ``pagination`` and
        ``notes``. A project-scoped listing is fetched in full, so ``has_more`` is false.

        Pitfalls: ``project`` is the project that DEFINES the version, which for a shared
        version is not the project you asked about — assigning still works. ``status`` is
        open/locked/closed and OpenProject refuses to put work packages into a closed
        version. The instance-wide listing is capped at one page of 100; if more exist,
        ``pagination.has_more`` is true and ``notes`` says so — narrow with ``project_id``
        rather than assuming you saw everything. ``include_sprints`` depends on the backlogs
        module: where it is not installed the versions still come back and ``notes`` explains
        the absence, so read ``notes`` before telling a user a project has no sprints.

        Cross-references: ``create_version`` adds one, ``update_version`` moves its dates or
        closes it, ``delete_version`` removes it; ``get_project_metadata(project_id=...)``
        returns the same versions alongside types and categories; to see what is IN a version
        use ``list_work_packages`` with a version filter.
        """
        ctx = get_tool_context()
        notes: list[str] = []

        if include_sprints and project_id is None:
            raise InputValidationError(
                "include_sprints requires project_id.",
                hint=(
                    "Sprints are read per project (/projects/{id}/sprints). Pass project_id, "
                    "or call list_versions without include_sprints for the instance-wide list."
                ),
            )

        if project_id is None:
            payload = await ctx.client.get_json("versions", params={"pageSize": VERSIONS_PAGE_SIZE})
            collection = hal.collection(payload)
            rows = [_version_row(element) for element in collection.elements]
            if collection.total > len(rows):
                notes.append(
                    f"{collection.total} versions exist on this instance; the first "
                    f"{len(rows)} are listed. Pass project_id to narrow the question."
                )
            return envelope_from_collection(
                collection, rows, page=1, page_size=VERSIONS_PAGE_SIZE, notes=notes or None
            )

        payload = await ctx.client.get_json(f"projects/{project_id}/versions")
        rows = [_version_row(element) for element in hal.collection(payload).elements]

        if include_sprints:
            sprints, sprint_note = await _sprint_rows(project_id)
            if sprint_note:
                notes.append(sprint_note)
            rows = _merge_sprints(rows, sprints)

        return build_envelope(
            rows,
            total=len(rows),
            page=1,
            page_size=max(len(rows), 1),
            notes=notes or None,
        )

    @mcp.tool(
        name="create_version",
        tags=tool_tags(GROUP_VERSIONS, WRITE),
        annotations=write_annotations(title="Create version"),
    )
    @tool_errors
    async def create_version(
        project_id: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric id or identifier of the project that will DEFINE the version. "
                    "Sharing decides which other projects can use it; the defining project "
                    "cannot be changed afterwards."
                )
            ),
        ],
        name: Annotated[
            str,
            Field(
                description=(
                    "Version name, e.g. 'Sprint 12' or 'Release 2.1'. Must be unique inside "
                    "the defining project."
                )
            ),
        ],
        start_date: Annotated[
            str | None,
            Field(description="ISO date (YYYY-MM-DD) the version starts. Omit for none."),
        ] = None,
        end_date: Annotated[
            str | None,
            Field(
                description=(
                    "ISO date (YYYY-MM-DD) the version finishes — this is the version's due "
                    "date, sent as the API's 'endDate'. Omit for none."
                )
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Markdown description of the version's scope. Omit for none."),
        ] = None,
        status: Annotated[
            VersionStatus | None,
            Field(
                description=(
                    "open (default upstream), locked or closed. Locked and closed versions "
                    "cannot receive new work packages, so create with 'open' unless you are "
                    "recording history."
                )
            ),
        ] = None,
        sharing: Annotated[
            VersionSharing | None,
            Field(
                description=(
                    "Which other projects may use this version: none (default), descendants, "
                    "hierarchy, tree or system (the whole instance). Use 'descendants' for a "
                    "release shared with subprojects."
                )
            ),
        ] = None,
    ) -> VersionDetail:
        """Create a version (release, milestone or sprint) inside a project.

        Use it to open a new sprint or plan a release before assigning work packages to it.
        The call goes through ``POST /versions/form`` first, so a duplicate name or an
        impossible date range comes back as ``violations`` naming the attribute instead of an
        opaque rejection.

        Returns the created version ``{id, name, project, status, start_date, end_date,
        description, sharing, source, created_at, updated_at}``. The ``id`` is what
        ``update_work_package(version=...)`` and ``update_version`` consume.

        Pitfalls: ``end_date`` is the version's finish date and is written to the API's
        ``endDate`` field — passing a date here always lands (an older client dropped it
        silently). Creating versions needs the 'manage versions' permission in the project,
        so a 403 is about the account, not the payload. Versions are per project: sharing is
        the only way another project sees this one.

        Cross-references: ``list_versions`` for what already exists (and for the ids);
        ``update_version`` to change dates or close it later; ``list_projects`` for the
        project id.
        """
        ctx = get_tool_context()
        if not name or not name.strip():
            raise InputValidationError(
                "name is empty.",
                hint="Pass the version name, e.g. 'Sprint 12'.",
            )

        attributes: dict[str, Any] = {"name": name.strip()}
        if start_date is not None:
            attributes["startDate"] = _iso_date(start_date, "start_date")
        if end_date is not None:
            # The wire name is endDate — the one the old server got wrong (§17).
            attributes["endDate"] = _iso_date(end_date, "end_date")
        if description is not None:
            attributes["description"] = formattable_field(description)
        if status is not None:
            attributes["status"] = status
        if sharing is not None:
            attributes["sharing"] = sharing

        payload = build_write_payload(attributes, {"definingProject": link("projects", project_id)})
        form = await ctx.client.post_json("versions/form", json=payload)
        _raise_form_validation_errors(form)

        defaults = _form_section(form, "payload")
        body = _merge_form_payload(defaults, payload) if isinstance(defaults, Mapping) else payload
        created = await ctx.client.post_json("versions", json=body)
        return _version_detail(created)

    @mcp.tool(
        name="update_version",
        tags=tool_tags(GROUP_VERSIONS, WRITE),
        annotations=write_annotations(title="Update version"),
    )
    @tool_errors
    async def update_version(
        version_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric version id from list_versions or "
                    "get_project_metadata(project_id=...).versions."
                )
            ),
        ],
        name: Annotated[
            str | None,
            Field(description="New name. Omit to leave it alone."),
        ] = None,
        start_date: Annotated[
            str | None,
            Field(
                description=(
                    "New start date, ISO YYYY-MM-DD. Pass null to clear it. Omit the "
                    "parameter entirely (the default) to leave it untouched."
                )
            ),
        ] = KEEP,
        end_date: Annotated[
            str | None,
            Field(
                description=(
                    "New finish date, ISO YYYY-MM-DD, written to the API's 'endDate'. Pass "
                    "null to clear it. Omit the parameter to leave it untouched."
                )
            ),
        ] = KEEP,
        description: Annotated[
            str | None,
            Field(
                description=(
                    "New markdown description; REPLACES the existing text. Pass null or an "
                    "empty string to clear it. Omit to leave it untouched."
                )
            ),
        ] = KEEP,
        status: Annotated[
            VersionStatus | None,
            Field(
                description=(
                    "open, locked or closed. Closing a version keeps its work packages but "
                    "stops new ones being assigned — it is the safe alternative to "
                    "delete_version. Omit to leave the status alone."
                )
            ),
        ] = None,
        sharing: Annotated[
            VersionSharing | None,
            Field(
                description=(
                    "none, descendants, hierarchy, tree or system. Narrowing the sharing of a "
                    "version other projects already use is rejected by the API. Omit to leave "
                    "it alone."
                )
            ),
        ] = None,
    ) -> VersionDetail:
        """Change a version's name, dates, description, status or sharing.

        Use it to move a sprint's dates, to close a finished release (``status='closed'``),
        or to widen sharing so a subproject can use the version. The change is validated
        through ``POST /versions/{id}/form`` first, so rejected values come back as
        ``violations`` naming the attribute.

        Only the parameters you pass are sent, so concurrent edits to other fields survive.
        Versions carry no ``lockVersion`` upstream, so there is nothing to echo and no lock
        parameter here — a 409 would mean the resource itself changed, not a stale version.

        Returns the updated version in the same shape as ``create_version``.

        Pitfalls: ``end_date`` writes the API's ``endDate`` — it lands, unlike in the old
        server. ``description`` REPLACES the stored text. Closing a version does not move or
        unassign its work packages; they keep pointing at it. The defining project cannot be
        changed — create a new version instead.

        Cross-references: ``list_versions`` for ids and current values; ``delete_version``
        when the version must really disappear; ``update_work_package(version=...)`` to move
        individual work packages between versions.
        """
        ctx = get_tool_context()

        attributes: dict[str, Any] = {}
        if name is not None:
            if not name.strip():
                raise InputValidationError(
                    "name is empty.",
                    hint="Omit name to leave it unchanged; a version cannot have a blank name.",
                )
            attributes["name"] = name.strip()
        if start_date != KEEP:
            attributes["startDate"] = _date_attribute(start_date, "start_date")
        if end_date != KEEP:
            attributes["endDate"] = _date_attribute(end_date, "end_date")
        if description != KEEP:
            attributes["description"] = formattable_field(description or "")
        if status is not None:
            attributes["status"] = status
        if sharing is not None:
            attributes["sharing"] = sharing

        if not attributes:
            raise InputValidationError(
                "update_version was called with nothing to change.",
                hint=(
                    "Pass at least one of name, start_date, end_date, description, status or "
                    "sharing."
                ),
            )

        payload = build_write_payload(attributes)
        path = f"versions/{version_id}"
        try:
            form = await ctx.client.post_json(f"{path}/form", json=payload)
        except NotFoundError as exc:
            raise _version_not_found(exc, version_id) from exc
        _raise_form_validation_errors(form)

        # Plain PATCH: versions have no lockVersion, and inventing one would be worse than
        # sending none (SPEC §4.4).
        updated = await ctx.client.patch_json(path, json=payload)
        return _version_detail(updated)

    @mcp.tool(
        name="delete_version",
        tags=tool_tags(GROUP_VERSIONS, WRITE, DESTRUCTIVE),
        annotations=destructive_annotations(title="Delete version"),
    )
    @tool_errors
    async def delete_version(
        version_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric version id to delete. Read it back with list_versions first — "
                    "version names repeat across projects, ids do not."
                )
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be true. Ask the user first: there is no undo. Calling with "
                    "confirm=false returns a confirmation_required error and deletes nothing."
                )
            ),
        ] = False,
    ) -> VersionDeletionResult:
        """Permanently delete a version.

        Use it only for a version created by mistake. For a finished release or sprint,
        ``update_version(status='closed')`` is almost always the right answer: it keeps the
        history and stops new assignments.

        Returns a small confirmation once OpenProject accepts the deletion. Work packages are
        NOT deleted — they simply lose their version — but that only happens on instances
        that allow the deletion at all.

        Pitfalls: OpenProject refuses (422) to delete a version that work packages still
        reference; the error hint explains how to find them. Deleting a shared version
        affects every project that used it. The 'manage versions' permission is required, so
        a 403 is about the account.

        Cross-references: ``list_versions`` for the id; ``update_version(status='closed')``
        for the reversible alternative; ``list_work_packages`` to find what still points at
        the version before removing it.
        """
        require_confirmation(
            confirm,
            action="delete version",
            target=f"#{version_id}",
            consequence=(
                "The version is removed for good; every work package assigned to it loses its "
                "version, and the deletion cannot be undone through the API."
            ),
        )
        ctx = get_tool_context()
        try:
            await ctx.client.delete(f"versions/{version_id}")
        except NotFoundError as exc:
            raise _version_not_found(exc, version_id) from exc
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    f"OpenProject refuses to delete version {version_id}, normally because work "
                    "packages (or budgets) still reference it. Find them with "
                    'list_work_packages(raw_filters=[{"name": "version", "operator": "=", '
                    f'"values": ["{version_id}"]}}], status_scope="all"), move them with '
                    "update_work_package(version=<other id>), then retry — or keep the version "
                    "and call update_version(status='closed') instead."
                ),
            ) from exc

        return VersionDeletionResult(
            id=version_id,
            deleted=True,
            message=f"Version {version_id} was deleted; work packages that used it now have none.",
        )
