"""Collaboration-module tools: meetings, wiki, documents, budgets (SPEC §6.13).

Lands here:

===================================  ======  ==============================================
Tool                                 Phase   Endpoint(s)
===================================  ======  ==============================================
🔍Ⓜ ``list_meetings``                3       ``GET /meetings`` (project + time filters)
🔍Ⓜ ``get_meeting``                  3       ``GET /meetings/{id}``
                                             + ``…/agenda_items``
✏️Ⓜ ``create_meeting``               3       form → ``POST /meetings``
✏️Ⓜ ``update_meeting``               3       ``PATCH /meetings/{id}``
🗑Ⓜ ``delete_meeting``               3       ``DELETE /meetings/{id}``
✏️Ⓜ ``add_meeting_agenda_item``      3       ``POST /meeting_agenda_items``
✏️Ⓜ ``update_meeting_agenda_item``   3       ``PATCH /meeting_agenda_items/{id}``
🗑Ⓜ ``delete_meeting_agenda_item``   3       ``DELETE /meeting_agenda_items/{id}``
✏️Ⓜ ``add_meeting_outcome``          3       ``POST /meeting_outcomes``
✏️Ⓜ ``update_meeting_outcome``       3       ``PATCH /meeting_outcomes/{id}``
🗑Ⓜ ``delete_meeting_outcome``       3       ``DELETE /meeting_outcomes/{id}``
🔍Ⓜ ``get_wiki_page``                3       ``GET /wiki_pages/{id}``
🔍Ⓜ ``list_documents``               3       ``GET /documents``
🔍Ⓜ ``get_document``                 3       ``GET /documents/{id}``
🔍Ⓜ ``list_budgets``                 3       ``GET /projects/{id}/budgets``
===================================  ======  ==============================================

Non-negotiables for this module:

* **Every tool here is module-dependent (Ⓜ, SPEC §4.7).** The backing endpoint
  answers 404 when the module is not installed on the instance or not enabled in
  the project, and 403 when it is there but this account may not read it. Either
  way the answer is never a silent "there are none" — but *how* it is said depends
  on whether there is an envelope to say it in. The **list** tools return an empty
  page plus the explanation in ``notes``, which is the shape SPEC §4.7/G5 mandate
  and the one ``list_file_links`` already uses. The **single-resource reads and
  the writes** have no ``notes`` field to carry it, so there the same 404/403
  become a *typed* error whose hint names both possibilities. Sub-resource
  failures (a meeting's agenda items) degrade to a ``notes`` marker too.
* **The meetings ``time`` filter is not part of the shared operator grammar,
  and it changed spelling in 17.6.** Current instances take the value-less
  ``upcoming`` / ``past`` operators (``Queries::Meetings::Filters::TimeFilter``);
  older ones (live-confirmed on 16.6) take ``=`` with a single ``past``/
  ``future`` value. Both are meetings-only, so the wire filter is built directly
  rather than through ``make_filter``, and ``list_meetings`` discovers the
  instance's dialect on first use and caches it (SPEC §4.7).
* **Meeting listings are explicitly sorted.** The meetings query drops its
  default order, so an unsorted page is an arbitrary slice; ``sortBy`` on
  ``startTime`` is always sent, and an instance that rejects it degrades to an
  unsorted result plus a note rather than to an error (G5).
* **Wiki pages carry no content.** API v3 exposes id, title and attachments
  only, and there is no wiki index or search — both stated in the description
  and in-band in ``notes`` (SPEC §18).
* **Budgets are id + subject only.** The v3 budget representer exposes nothing
  else: no planned or spent amounts. Saying so beats inventing them (G3).
* **The meeting writes are version-fenced.** OpenProject grew this API in
  steps: before 17.4 no meeting write route exists at all (404 at route level,
  or 405 where the read path exists but the verb is not routed), and the flat
  ``/meeting_agenda_items/{id}`` and ``/meeting_outcomes`` write routes only
  ship with 17.6. Every write tool's 404 hint names this third reading, and a
  405 is mapped onto the same explanation rather than the generic
  unexpected-status hint.
* **Writes are lock-safe where upstream versions them.** Meetings and agenda
  items carry ``lockVersion``, so their update tools go through
  ``patch_with_lock`` (SPEC §4.4): the caller's ``lock_version`` is echoed (or
  the current one fetched first), and a stale write surfaces as a ``conflict``
  carrying the fresh version. Outcomes carry no ``lockVersion``, so their PATCH
  is plain — and every outcome write only works while the meeting state is
  exactly ``in_progress``, which the 422 hint says out loud.

Out of scope here: meeting sections' own CRUD and document update (SPEC §18).
Recurring meetings live in the sibling ``meetings_recurring`` module.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp import version_probe
from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    NotFoundError,
    PermissionDeniedError,
    UnexpectedResponseError,
    ValidationFailedError,
)
from openproject_mcp.client.filters import (
    DEFAULT_PAGE_SIZE,
    Filter,
    FilterType,
    Op,
    make_filter,
    pagination_params,
    register_filter_type,
    serialize_filters,
    serialize_sort_by,
)
from openproject_mcp.client.locking import extract_lock_version, patch_with_lock
from openproject_mcp.client.payloads import build_write_payload, formattable_field, link
from openproject_mcp.config import PROBE_CACHE_TTL
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools import _forms, _shared

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "KEEP",
    "AgendaItem",
    "AgendaItemDetail",
    "BudgetRow",
    "DocumentDetail",
    "DocumentRow",
    "MeetingDeletionResult",
    "MeetingDetail",
    "MeetingOutcome",
    "MeetingOutcomeDetail",
    "MeetingRow",
    "WikiPage",
    "register",
]

#: Sentinel default for update parameters that can be *cleared*: omitting the
#: parameter (this value) leaves the field untouched, ``None``/``""`` clears it.
KEEP = "__unchanged__"

#: Resource key for the meetings endpoint's own filter set (SPEC §9.1).
MEETINGS_RESOURCE = "meetings"

#: The value-less operator of the meetings ``time`` filter (its sibling is
#: ``past``). Both exist on this endpoint only, so they are deliberately kept out
#: of the shared operator vocabulary; ``upcoming_only=False`` sends no time
#: filter at all rather than ``past``, which would hide future meetings.
TIME_UPCOMING = "upcoming"

#: Pre-17.6 spelling of the same filter: a list filter taking ``=`` with a
#: single ``past``/``future`` value (live-confirmed on 16.6, which answers the
#: operator dialect with 400 "Start time is not set to one of the allowed
#: values"). Both dialects select exactly the same rows; ``list_meetings``
#: discovers which one the instance speaks and caches it (SPEC §4.7).
TIME_LEGACY_FUTURE = "future"

#: The one sort key both this module and the upstream meetings query agree on.
MEETING_SORT_KEY = "start_time"

#: OpenProject renders this URN instead of an href when an agenda item links a
#: work package the current account may not see.
UNDISCLOSED_URN = "urn:openproject-org:api:v3:undisclosed"

WIKI_CONTENT_NOTE = (
    "wiki page content is not available: API v3 exposes only the page id, title and its "
    "attachments. Read the text in the browser, or fetch the files with "
    "list_attachments(container_type='wiki_page', container_id=<id>)."
)
MEETING_SORT_NOTE = (
    "meetings are unsorted: this instance rejected sortBy=startTime, so the page is an "
    "arbitrary slice of the matching meetings rather than the next ones by date"
)
AGENDA_FORBIDDEN_NOTE = (
    "agenda items: no permission (403) — the meeting itself is readable, but this account may "
    "not read its agenda"
)
AGENDA_MISSING_NOTE = (
    "agenda items: not available (404) — this instance does not expose "
    "/meetings/{id}/agenda_items, so the agenda could not be read"
)

#: Lifecycle states a meeting accepts on PATCH. ``state`` is a plain writable
#: attribute upstream — open/close/cancel/publish are all the same PATCH — and
#: there is no transition matrix, with one rule: a closed meeting accepts a
#: state-only patch (reopening it) and nothing else.
MeetingState = Literal["open", "draft", "in_progress", "cancelled", "closed"]

#: Outcome kinds. 'decision' is real despite being absent from the upstream API
#: docs; 'information' requires notes, 'work_package' requires a visible linked
#: work package, 'decision' requires nothing extra.
OutcomeKind = Literal["information", "decision", "work_package"]

#: The third reading a 404/405 on each write family can have, appended to the
#: module-absence hint: the whole meetings write API only exists from 17.4, and
#: the flat agenda-item / outcome routes only from 17.6 (SPEC §6.13).
MEETING_WRITE_DISCOVERY = (
    "Meeting ids come from list_meetings. A third reading applies to writes: OpenProject "
    "before 17.4 (16.6 among them) does not route the meetings write API at all, so this "
    "fails regardless of the meeting id even when the module is enabled and list_meetings "
    "works."
)
AGENDA_WRITE_DISCOVERY = (
    "Agenda item ids come from get_meeting, not from list_meetings. A third reading applies "
    "to writes: the flat /meeting_agenda_items/{id} write routes only exist from OpenProject "
    "17.6 (and 16.x has no meetings write API at all), so this fails regardless of the item "
    "id even when the module is enabled."
)
OUTCOME_WRITE_DISCOVERY = (
    "Outcome ids come from get_meeting's agenda_items[].outcomes. A third reading applies "
    "to writes: OpenProject before 17.6 has no outcomes API at all, so every "
    "/meeting_outcomes route fails there even when outcomes are visible in the UI."
)


@dataclass(frozen=True, slots=True)
class _Module:
    """One optional OpenProject module, and how its absence must be explained."""

    label: str
    permission: str


MEETINGS = _Module("Meetings", "view meetings")
WIKI = _Module("Wiki", "view wiki pages")
DOCUMENTS = _Module("Documents", "view documents")
BUDGETS = _Module("Budgets", "view budgets")

#: The Meetings module seen through each write permission — a 403 hint must name
#: the permission the write actually needs, not the read one (engine.rb §6.13:
#: edit_meetings, delete_meetings, manage_agendas, manage_outcomes).
MEETINGS_EDIT = _Module("Meetings", "edit meetings")
MEETINGS_DELETE = _Module("Meetings", "delete meetings")
AGENDAS = _Module("Meetings", "manage agendas")
OUTCOMES = _Module("Meetings", "manage outcomes")


# --- projections ----------------------------------------------------------


class MeetingRow(BaseModel):
    """One meeting as list results return it."""

    id: int | str | None = Field(
        default=None, description="Meeting id — what get_meeting and add_meeting_agenda_item take."
    )
    title: str | None = Field(default=None, description="Meeting title.")
    project: Ref | None = Field(default=None, description="Project the meeting belongs to.")
    start_time: str | None = Field(
        default=None, description="ISO 8601 UTC start timestamp; null for an undated meeting."
    )
    end_time: str | None = Field(
        default=None, description="ISO 8601 UTC end timestamp, derived from start plus duration."
    )
    duration_hours: float | None = Field(
        default=None,
        description="Scheduled length in hours (1.5 = 90 minutes); the wire sends an ISO "
        "duration, which is converted here.",
    )
    location: str | None = Field(
        default=None, description="Room name or meeting URL as typed by the organizer."
    )
    state: str | None = Field(
        default=None,
        description="Lifecycle state: 'draft' (not yet opened to participants), 'open', "
        "'in_progress', 'closed' or 'cancelled'. Cancelled meetings are excluded from listings.",
    )


class MeetingOutcome(BaseModel):
    """One recorded outcome of an agenda item."""

    id: int | str | None = Field(default=None, description="Outcome id.")
    kind: str | None = Field(
        default=None, description="Outcome kind as the instance defines it, e.g. 'decision'."
    )
    notes: str | None = Field(
        default=None, description="Outcome text as markdown (raw); html is dropped."
    )
    author: Ref | None = Field(default=None, description="User who recorded the outcome.")
    work_package: Ref | None = Field(
        default=None, description="Work package the outcome points at, when one was linked."
    )


class MeetingOutcomeDetail(MeetingOutcome):
    """An outcome as the outcome write tools return it."""

    agenda_item: Ref | None = Field(
        default=None, description="Agenda item the outcome is recorded against."
    )


class AgendaItem(BaseModel):
    """One agenda item of a meeting, with its outcomes."""

    id: int | str | None = Field(
        default=None, description="Agenda item id (not the meeting id, not a work package id)."
    )
    title: str | None = Field(
        default=None,
        description="Item title. Empty for a work-package item, where the work package's "
        "subject is the title shown in the UI.",
    )
    notes: str | None = Field(
        default=None, description="Item notes as markdown (raw); html is dropped."
    )
    duration_minutes: int | None = Field(
        default=None, description="Planned length in minutes; null when the organizer set none."
    )
    position: int | None = Field(default=None, description="1-based order within the agenda.")
    item_type: str | None = Field(
        default=None, description="'simple' for a free-text item, 'work_package' for a linked one."
    )
    presenter: Ref | None = Field(default=None, description="User presenting this item.")
    work_package: Ref | None = Field(
        default=None,
        description="Work package this item discusses. Null both when none is linked and when "
        "the linked one is invisible to this account — 'notes' says when the latter happened.",
    )
    section: Ref | None = Field(
        default=None, description="Agenda section the item sits in, when the meeting uses sections."
    )
    outcomes: list[MeetingOutcome] = Field(
        default_factory=list[MeetingOutcome],
        description="Outcomes recorded against this item; always a list, empty when none.",
    )
    lock_version: int | None = Field(
        default=None,
        description="Optimistic-lock version. Echo it as update_meeting_agenda_item's "
        "lock_version so a concurrent edit fails loudly (409) instead of being overwritten.",
    )


class AgendaItemDetail(AgendaItem):
    """An agenda item as ``add_meeting_agenda_item`` returns it."""

    meeting: Ref | None = Field(default=None, description="Meeting the item was added to.")
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class MeetingDetail(MeetingRow):
    """One meeting in full: participants plus the agenda and its outcomes."""

    lock_version: int | None = Field(
        default=None,
        description="Optimistic-lock version. Echo it as update_meeting's lock_version so a "
        "concurrent edit fails loudly (409) instead of being overwritten.",
    )
    author: Ref | None = Field(default=None, description="User who created the meeting.")
    participants: list[Ref] = Field(
        default_factory=list[Ref],
        description="Invited users; always a list. Attendance is not exposed by API v3.",
    )
    agenda_items: list[AgendaItem] = Field(
        default_factory=list[AgendaItem],
        description="The agenda in order; always a list. Empty means either no agenda or an "
        "unreadable one — check 'notes' before concluding the meeting had none.",
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    notes: list[str] = Field(
        default_factory=list[str],
        description="Degradation notes: agenda items that could not be read, work "
        "packages this account may not see.",
    )


class WikiPage(BaseModel):
    """A wiki page as API v3 exposes it: identity only, never content."""

    id: int | str | None = Field(default=None, description="Wiki page id.")
    title: str | None = Field(default=None, description="Page title as shown in the wiki menu.")
    project: Ref | None = Field(default=None, description="Project that owns the wiki.")
    notes: list[str] = Field(
        default_factory=list[str],
        description="Always carries the marker that page content is not part of API v3.",
    )


class DocumentRow(BaseModel):
    """One document as list results return it."""

    id: int | str | None = Field(
        default=None, description="Document id — pass it to get_document for the description."
    )
    title: str | None = Field(default=None, description="Document title.")
    project: Ref | None = Field(default=None, description="Project the document belongs to.")
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class DocumentDetail(DocumentRow):
    """A single document, with its description text."""

    description: str | None = Field(
        default=None, description="Description as markdown (raw); html is dropped."
    )


class BudgetRow(BaseModel):
    """One budget. API v3 exposes the identity only — no amounts."""

    id: int | str | None = Field(
        default=None,
        description="Budget id — the container_id for list_attachments(container_type='budget').",
    )
    subject: str | None = Field(default=None, description="Budget name as shown in the UI.")


class MeetingDeletionResult(BaseModel):
    """Outcome of the meetings-family delete tools."""

    id: int = Field(
        description="Id of the meeting, agenda item, outcome or recurring series that was deleted."
    )
    deleted: bool = Field(description="True once OpenProject accepted the deletion.")
    message: str = Field(description="Human-readable confirmation naming what was removed.")


# --- payload helpers ------------------------------------------------------


def _is_undisclosed(payload: Mapping[str, Any], key: str) -> bool:
    """True when OpenProject withheld a linked resource behind the undisclosed URN."""
    resolved = hal.ref(payload, key)
    return resolved is not None and (resolved.href or "").startswith(UNDISCLOSED_URN)


def _visible_ref(payload: Mapping[str, Any], key: str) -> Ref | None:
    """A link ref, unless OpenProject replaced the href with the undisclosed URN.

    An agenda item that references a work package the account may not see still
    renders the link, pointing at ``urn:…:undisclosed``. Surfacing that as an id
    would invent a resource, so it becomes ``None`` plus a note instead.
    """
    if _is_undisclosed(payload, key):
        return None
    return Ref.from_hal(payload, key)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _embedded_elements(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """Embedded sub-resources, whether sent as a bare list or a HAL collection."""
    raw = hal.embedded(payload, key)
    wrapped = hal.as_object(raw)
    if wrapped is not None:
        return hal.collection(wrapped).elements
    return [dict(item) for item in hal.as_objects(raw)]


def _meeting_row(payload: Mapping[str, Any]) -> MeetingRow:
    state = payload.get("state")
    return MeetingRow(
        id=hal.self_id(payload),
        title=payload.get("title"),
        project=Ref.from_hal(payload, "project"),
        start_time=payload.get("startTime"),
        end_time=payload.get("endTime"),
        duration_hours=hal.duration_hours(payload.get("duration")),
        location=payload.get("location"),
        state=state if isinstance(state, str) else None,
    )


def _outcome(payload: Mapping[str, Any]) -> MeetingOutcome:
    kind = payload.get("kind")
    return MeetingOutcome(
        id=hal.self_id(payload),
        kind=kind if isinstance(kind, str) else None,
        notes=hal.formattable(payload.get("notes")),
        author=Ref.from_hal(payload, "author"),
        work_package=_visible_ref(payload, "workPackage"),
    )


def _outcome_detail(payload: Mapping[str, Any]) -> MeetingOutcomeDetail:
    return MeetingOutcomeDetail(
        **_outcome(payload).model_dump(),
        agenda_item=Ref.from_hal(payload, "agendaItem"),
    )


def _agenda_item(payload: Mapping[str, Any]) -> AgendaItem:
    item_type = payload.get("itemType")
    return AgendaItem(
        id=hal.self_id(payload),
        title=payload.get("title"),
        notes=hal.formattable(payload.get("notes")),
        duration_minutes=_int_or_none(payload.get("durationInMinutes")),
        position=_int_or_none(payload.get("position")),
        item_type=item_type if isinstance(item_type, str) else None,
        presenter=Ref.from_hal(payload, "presenter"),
        work_package=_visible_ref(payload, "workPackage"),
        section=Ref.from_hal(payload, "section"),
        outcomes=[_outcome(item) for item in _embedded_elements(payload, "outcomes")],
        lock_version=extract_lock_version(payload),
    )


def _agenda_item_detail(payload: Mapping[str, Any]) -> AgendaItemDetail:
    return AgendaItemDetail(
        **_agenda_item(payload).model_dump(),
        meeting=Ref.from_hal(payload, "meeting"),
        created_at=payload.get("createdAt"),
    )


def _participants(payload: Mapping[str, Any]) -> list[Ref]:
    """Participants from ``_links.participants``, falling back to the embedded copy."""
    linked = Ref.list_from_hal(payload, "participants")
    if linked:
        return linked
    return [
        Ref(id=hal.self_id(item), name=item.get("name"))
        for item in _embedded_elements(payload, "participants")
    ]


def _meeting_detail(
    payload: Mapping[str, Any], *, agenda_items: list[AgendaItem], notes: list[str]
) -> MeetingDetail:
    """One meeting body plus its (separately fetched) agenda, as the full projection."""
    return MeetingDetail(
        **_meeting_row(payload).model_dump(),
        lock_version=extract_lock_version(payload),
        author=Ref.from_hal(payload, "author"),
        participants=_participants(payload),
        agenda_items=agenda_items,
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
        notes=notes,
    )


def _undisclosed_note(elements: list[dict[str, Any]]) -> str | None:
    """The note for agenda items whose linked work package is withheld, if any."""
    undisclosed = [
        str(hal.self_id(element)) for element in elements if _is_undisclosed(element, "workPackage")
    ]
    if not undisclosed:
        return None
    return (
        f"agenda item(s) {', '.join(undisclosed)} link a work package this account may "
        "not view; OpenProject withholds its id, so work_package is null there"
    )


def _document_row(payload: Mapping[str, Any]) -> DocumentRow:
    return DocumentRow(
        id=hal.self_id(payload),
        title=payload.get("title"),
        project=Ref.from_hal(payload, "project"),
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
    )


def _budget_row(payload: Mapping[str, Any]) -> BudgetRow:
    return BudgetRow(id=hal.self_id(payload), subject=payload.get("subject"))


# --- module gating (SPEC §4.7, G5) ----------------------------------------
#
# One condition upstream, two shapes down here, chosen by what the return type can
# carry. A list tool returns ListEnvelope, whose ``notes`` field exists for exactly
# this (SPEC §4.7: 404 → "unavailable (module not installed)", 403 → "no
# permission"), so absence is reported in-band and the call succeeds with an empty
# page. A detail read or a write has nowhere in-band to put it, so there the same
# 404/403 is a typed error. The explanation is identical either way: both shapes
# are built from the two ``_hint`` functions below.


def _module_not_found_hint(*, module: _Module, subject: str, discovery: str) -> str:
    """What a 404 on a module endpoint can mean — both readings, then how to check."""
    return (
        f"Either {subject} does not exist, or the {module.label} module is not installed on "
        f"this instance / not enabled in this project — a 404 cannot tell those apart, so do "
        f"not report that there are none. {discovery} "
        f"get_project_metadata(project_id=...) and the project's module settings show whether "
        f"{module.label} is enabled here."
    )


def _module_forbidden_hint(*, module: _Module) -> str:
    """What a 403 on a module endpoint means: the module is there, the account is not allowed."""
    return (
        f"The {module.label} module is enabled but this account lacks the "
        f"'{module.permission}' permission in this project — that is an account problem, not "
        "a bad id, so retrying with another id will not help. list_permissions shows what "
        "the current user may do."
    )


def _module_not_found(
    exc: NotFoundError, *, module: _Module, subject: str, discovery: str
) -> NotFoundError:
    """A 404 on a module detail read or write, as a typed error."""
    return NotFoundError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=_module_not_found_hint(module=module, subject=subject, discovery=discovery),
    )


def _module_forbidden(exc: PermissionDeniedError, *, module: _Module) -> PermissionDeniedError:
    """A 403 on a module detail read or write, as a typed error."""
    return PermissionDeniedError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=_module_forbidden_hint(module=module),
    )


async def _module_get(
    path: str,
    *,
    module: _Module,
    subject: str,
    discovery: str,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """GET a module endpoint, mapping 404/403 onto the two honest explanations."""
    ctx = _shared.get_tool_context()
    try:
        return await ctx.client.get_json(path, params=params)
    except NotFoundError as exc:
        raise _module_not_found(exc, module=module, subject=subject, discovery=discovery) from exc
    except PermissionDeniedError as exc:
        raise _module_forbidden(exc, module=module) from exc


def _module_missing_note(*, module: _Module, subject: str, discovery: str) -> str:
    """A 404 on a module list endpoint, worded as an in-band degradation marker."""
    return (
        f"unavailable (module not installed): nothing could be read (404), so the empty 'items' "
        f"is the absence of an answer and not an answer. "
        f"{_module_not_found_hint(module=module, subject=subject, discovery=discovery)}"
    )


def _module_forbidden_note(*, module: _Module) -> str:
    """A 403 on a module list endpoint, worded as an in-band degradation marker."""
    return (
        f"no permission (403): nothing could be read, so the empty 'items' is the absence of an "
        f"answer and not an answer. {_module_forbidden_hint(module=module)}"
    )


async def _module_list_get(
    path: str,
    *,
    module: _Module,
    subject: str,
    discovery: str,
    params: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """GET a module list endpoint; 404/403 degrade to a note rather than raising (G5).

    Returns the payload and no notes on success, or ``None`` plus the one note that
    explains which of the two things happened. The caller turns that into an empty
    envelope carrying the note — see :func:`_unavailable_envelope`.
    """
    ctx = _shared.get_tool_context()
    try:
        return await ctx.client.get_json(path, params=params), []
    except NotFoundError:
        return None, [_module_missing_note(module=module, subject=subject, discovery=discovery)]
    except PermissionDeniedError:
        return None, [_module_forbidden_note(module=module)]


def _unavailable_envelope(notes: list[str]) -> ListEnvelope[Any]:
    """The envelope a list tool returns when its module could not be read."""
    return _shared.build_envelope([], total=0, page=1, page_size=1, notes=notes)


async def _module_post(
    path: str,
    *,
    json: Any,
    module: _Module,
    subject: str,
    discovery: str,
) -> dict[str, Any]:
    """POST to a module endpoint with the same 404/403 mapping as :func:`_module_get`."""
    ctx = _shared.get_tool_context()
    try:
        return await ctx.client.post_json(path, json=json)
    except NotFoundError as exc:
        raise _module_not_found(exc, module=module, subject=subject, discovery=discovery) from exc
    except PermissionDeniedError as exc:
        raise _module_forbidden(exc, module=module) from exc


def _module_unrouted(
    exc: UnexpectedResponseError, *, module: _Module, discovery: str
) -> UnexpectedResponseError:
    """A 405 on a module write: the read path exists, the write verb is not routed.

    Pre-17.4 OpenProject has the meetings *read* routes but none of the writes,
    and Grape answers a known path with an unrouted verb with 405 rather than
    404. Both spell "this instance predates the meetings write API", so the 405
    carries the same version-aware reading instead of the generic
    unexpected-status hint.
    """
    return UnexpectedResponseError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=(
            f"HTTP 405 here means the path exists for reads but this write verb is not routed "
            f"— this OpenProject predates the {module.label} write API, the same reading as a "
            f"404 on the sibling write routes, so retrying with another id will not help. "
            f"{discovery}"
        ),
    )


async def _module_patch_with_lock(
    path: str,
    payload: Mapping[str, Any],
    *,
    lock_version: int | None,
    module: _Module,
    subject: str,
    discovery: str,
) -> dict[str, Any]:
    """PATCH a lock-versioned module resource, 404/403/405 mapped like :func:`_module_get`.

    The lockVersion handling itself is :func:`patch_with_lock` (SPEC §4.4): the
    caller's value is echoed, or the current one is fetched first, and a stale
    write comes back as a ``conflict`` error carrying the fresh version — that
    error passes through untouched, because a 409 says "re-read and retry", not
    "module missing". A 404 from the pre-fetch means the same two-or-three
    things a 404 on the PATCH itself would, so both get the same mapping.
    """
    ctx = _shared.get_tool_context()
    try:
        return await patch_with_lock(ctx.client, path, payload, lock_version=lock_version)
    except NotFoundError as exc:
        raise _module_not_found(exc, module=module, subject=subject, discovery=discovery) from exc
    except PermissionDeniedError as exc:
        raise _module_forbidden(exc, module=module) from exc
    except UnexpectedResponseError as exc:
        if exc.http_status == 405:
            raise _module_unrouted(exc, module=module, discovery=discovery) from exc
        raise


async def _module_patch(
    path: str,
    *,
    json: Any,
    module: _Module,
    subject: str,
    discovery: str,
) -> dict[str, Any]:
    """Plain PATCH for module resources without a ``lockVersion`` (outcomes)."""
    ctx = _shared.get_tool_context()
    try:
        return await ctx.client.patch_json(path, json=json)
    except NotFoundError as exc:
        raise _module_not_found(exc, module=module, subject=subject, discovery=discovery) from exc
    except PermissionDeniedError as exc:
        raise _module_forbidden(exc, module=module) from exc
    except UnexpectedResponseError as exc:
        if exc.http_status == 405:
            raise _module_unrouted(exc, module=module, discovery=discovery) from exc
        raise


async def _module_delete(
    path: str,
    *,
    module: _Module,
    subject: str,
    discovery: str,
) -> dict[str, Any]:
    """DELETE a module endpoint with the same 404/403/405 mapping as the writes."""
    ctx = _shared.get_tool_context()
    try:
        return await ctx.client.delete(path)
    except NotFoundError as exc:
        raise _module_not_found(exc, module=module, subject=subject, discovery=discovery) from exc
    except PermissionDeniedError as exc:
        raise _module_forbidden(exc, module=module) from exc
    except UnexpectedResponseError as exc:
        if exc.http_status == 405:
            raise _module_unrouted(exc, module=module, discovery=discovery) from exc
        raise


async def _agenda_items(meeting_id: int) -> tuple[list[dict[str, Any]], str | None]:
    """Read a meeting's agenda; an unreadable agenda degrades to a note (G5)."""
    ctx = _shared.get_tool_context()
    try:
        payload = await ctx.client.get_json(f"meetings/{meeting_id}/agenda_items")
    except NotFoundError:
        return [], AGENDA_MISSING_NOTE
    except PermissionDeniedError:
        return [], AGENDA_FORBIDDEN_NOTE
    return hal.collection(payload).elements, None


# --- input helpers --------------------------------------------------------


def _numeric_project_id(project_id: int | str) -> int:
    """The meetings project filter takes numeric ids only; reject a slug locally (G2)."""
    if isinstance(project_id, int) and not isinstance(project_id, bool):
        return project_id
    text = str(project_id).strip()
    if text.isdigit():
        return int(text)
    raise InputValidationError(
        f"project_id={project_id!r} is not a numeric project id.",
        hint=(
            "The meetings endpoint filters by numeric project id only — a project identifier "
            "(the URL slug) is rejected upstream. list_projects returns both; use the id."
        ),
    )


def _iso_datetime(value: str, field_name: str) -> str:
    """Validate an ISO 8601 datetime *with* an offset, locally (G2/G3)."""
    candidate = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise InputValidationError(
            f"{field_name}={value!r} is not an ISO 8601 datetime.",
            hint=(
                f"{field_name} must be ISO 8601 with a timezone, e.g. '2026-08-03T14:00:00Z' "
                "or '2026-08-03T16:00:00+02:00'."
            ),
        ) from exc
    if parsed.tzinfo is None:
        raise InputValidationError(
            f"{field_name}={value!r} has no UTC offset.",
            hint=(
                f"{field_name} must carry a timezone, e.g. '2026-08-03T14:00:00Z' (UTC) or "
                "'2026-08-03T16:00:00+02:00'. Guessing the user's zone would book the wrong hour."
            ),
        )
    return candidate


def _iso_duration(minutes: int) -> str:
    """Minutes as the ISO 8601 duration the meetings API stores: 90 → ``PT1H30M``."""
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"PT{hours}H{rest}M"
    if hours:
        return f"PT{hours}H"
    return f"PT{rest}M"


def _meeting_form_hints(form: Mapping[str, Any], errors: Mapping[str, Any]) -> list[str]:
    """The meeting-specific half of a form rejection."""
    hints: list[str] = []
    if "title" in errors:
        hints.append("A meeting needs a non-empty title.")
    if "startTime" in errors or "startDate" in errors:
        hints.append("start_time must be ISO 8601 with a timezone, e.g. '2026-08-03T14:00:00Z'.")
    if "duration" in errors:
        hints.append("duration_minutes must be a positive whole number of minutes.")
    if "project" in errors or "projectId" in errors:
        hints.append(
            "The project must exist, have the Meetings module enabled, and this account needs "
            "the 'create meetings' permission in it."
        )
    return hints


def _raise_meeting_form_errors(form: Mapping[str, Any]) -> None:
    _forms.raise_validation_errors(
        form,
        subject="meeting",
        hints=_meeting_form_hints,
        fallback_hint=(
            "Fix the attributes listed in 'violations'; list_meetings(project_id=...) shows what "
            "this project already schedules."
        ),
    )


def register(mcp: FastMCP) -> None:
    """Register the meetings, wiki, documents and budgets tools (SPEC §6.13)."""
    # The meetings endpoint filters projects with its own optional list filter;
    # teach the validator about it rather than editing the shared table (§9.1).
    register_filter_type("project", FilterType.LIST_OPTIONAL, resource=MEETINGS_RESOURCE)

    @mcp.tool(
        name="list_meetings",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.READ),
        annotations=_shared.read_annotations(title="List meetings"),
    )
    @_shared.tool_errors
    async def list_meetings(
        project_id: Annotated[
            int | str | None,
            Field(
                description="Numeric project id to list only that project's meetings, from "
                "list_projects. Unlike most project parameters this one takes the id only — the "
                "meetings filter rejects a URL identifier, and passing one fails immediately "
                "with that explanation. Omit for every meeting visible to you."
            ),
        ] = None,
        upcoming_only: Annotated[
            bool,
            Field(
                description="True (the default) lists meetings that have not finished yet, "
                "soonest first. False lists past meetings too, most recent first — use it for "
                "'what did we discuss last week'."
            ),
        ] = True,
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=100, description="Meetings per page (max 100).")
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[MeetingRow]:
        """List meetings — the schedule side of a project: what is coming up, what already ran.

        Use it to answer "when do we next meet", "what meetings does this project have", or to
        find the meeting id that `get_meeting` and `add_meeting_agenda_item` need. Meeting ids
        are instance-wide and never guessable, so this is the way to get one.

        Returns the standard list envelope: rows of `{id, title, project, start_time, end_time,
        duration_hours, location, state}` plus `pagination` and `notes`. Times are ISO 8601 UTC
        and `duration_hours` is a float (1.5 = 90 minutes). Rows are ordered by start time —
        ascending when `upcoming_only`, descending otherwise.

        Pitfalls. Cancelled meetings and recurring-series templates are excluded upstream, so an
        absent meeting may exist in another state. `state` of 'draft' means the meeting has not
        been opened to its participants yet. Agenda items and participants are NOT in these rows;
        `get_meeting` fetches them. Meetings are a module: when it is not installed here or not
        enabled in the project (404), or this account may not read meetings (403), the call still
        SUCCEEDS with an empty `items` and the reason in `notes` — read `notes` before saying a
        project has no meetings, because an empty list with a note is not an empty schedule.

        Cross-references: `get_meeting(meeting_id=...)` for participants, agenda and outcomes;
        `create_meeting` to schedule one; `list_projects` for the project id.
        """
        base_filters: list[Filter] = []
        if project_id is not None:
            base_filters.append(
                make_filter(
                    "project",
                    Op.EQ,
                    [_numeric_project_id(project_id)],
                    resource=MEETINGS_RESOURCE,
                )
            )
        direction = "asc" if upcoming_only else "desc"

        def build_params(
            dialect: version_probe.MeetingsTimeDialect, with_sort: bool
        ) -> dict[str, Any]:
            active = list(base_filters)
            if upcoming_only:
                # Meetings-only filter, outside the shared grammar in either
                # dialect: value-less operator (17.6+) or `=` + "future" (older).
                active.append(
                    Filter(name="time", operator="=", values=[TIME_LEGACY_FUTURE])
                    if dialect == "values"
                    else Filter(name="time", operator=TIME_UPCOMING, values=[])
                )
            params: dict[str, Any] = dict(pagination_params(page, page_size))
            serialized = serialize_filters(active)
            if serialized is not None:
                params["filters"] = serialized
            if with_sort:
                sort = serialize_sort_by([[MEETING_SORT_KEY, direction]])
                if sort is not None:
                    params["sortBy"] = sort
            return params

        ctx = _shared.get_tool_context()
        known = version_probe.cached_meetings_time_dialect(ctx.cache, ctx.scope)

        # A 404 here is about the collection endpoint, not about one meeting.
        subject = "the meetings collection"
        discovery = "Meeting ids come from list_meetings."

        async def fetch(
            dialect: version_probe.MeetingsTimeDialect, with_sort: bool
        ) -> tuple[Mapping[str, Any] | None, list[str]]:
            return await _module_list_get(
                "meetings",
                module=MEETINGS,
                subject=subject,
                discovery=discovery,
                params=build_params(dialect, with_sort),
            )

        def remember(dialect: version_probe.MeetingsTimeDialect) -> None:
            # A success only proves the dialect when the time filter was sent
            # and really answered (a module-missing 404 says nothing about it).
            if upcoming_only:
                ctx.cache.set(
                    version_probe.CACHE_KEY_MEETINGS_TIME_FILTER,
                    dialect,
                    scope=ctx.scope,
                    ttl=PROBE_CACHE_TTL,
                )

        # Two independent things can be rejected here — the time-filter dialect
        # (17.6 changed its spelling) and the startTime sort (the meetings query
        # drops its default order, so unsorted is an arbitrary slice). Walk the
        # ladder: preferred dialect sorted, other dialect sorted, then both
        # unsorted with the G5 note — reading unsorted beats failing, but it is
        # said out loud. The winning dialect is cached, so only the first call
        # on an older instance pays the extra request.
        first = known or "operator"
        ladder: list[tuple[version_probe.MeetingsTimeDialect, bool, bool]] = (
            [("values", True, False), ("operator", False, True), ("values", False, True)]
            if upcoming_only and known is None and first == "operator"
            else [(first, False, True)]
        )
        notes: list[str] = []
        payload: Mapping[str, Any] | None = None
        degraded: list[str] = []
        try:
            payload, degraded = await fetch(first, True)
            if payload is not None:
                remember(first)
        except ValidationFailedError:
            for step, (dialect, with_sort, sort_note) in enumerate(ladder):
                try:
                    payload, degraded = await fetch(dialect, with_sort)
                except ValidationFailedError:
                    if step == len(ladder) - 1:
                        raise
                    continue
                if payload is not None:
                    remember(dialect)
                if sort_note:
                    notes.append(MEETING_SORT_NOTE)
                break
        if payload is None:
            # Ⓜ module absent or unreadable — a note, never an empty schedule (G5).
            return _unavailable_envelope(notes + degraded)

        collection = hal.collection(payload)
        rows = [_meeting_row(element) for element in collection]
        return _shared.envelope_from_collection(
            collection, rows, page=page, page_size=page_size, notes=notes or None
        )

    @mcp.tool(
        name="get_meeting",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.READ),
        annotations=_shared.read_annotations(title="Get meeting"),
    )
    @_shared.tool_errors
    async def get_meeting(
        meeting_id: Annotated[
            int,
            Field(
                description="Numeric meeting id from list_meetings (it is also the number in a "
                "/meetings/<id> UI URL). Never a project id or an agenda item id."
            ),
        ],
    ) -> MeetingDetail:
        """Read one meeting in full: participants, the agenda, and any recorded outcomes.

        This is the "what was discussed / what was decided" call. It returns the meeting fields
        (`title`, `project`, `start_time`, `end_time`, `duration_hours`, `location`, `state`,
        `author`, timestamps), the invited `participants` as `{id, name}` refs, and
        `agenda_items` in agenda order — each with its `title`, `notes` (markdown),
        `duration_minutes`, `presenter`, the `work_package` it discusses, its `section`, and the
        `outcomes` recorded against it (`kind`, `notes`, author, linked work package).

        Pitfalls. A work-package agenda item carries an empty `title` — the work package's
        subject is what the UI shows, so read `work_package.name`. When the linked work package
        is invisible to this account, `work_package` is null and `notes` says so; do not report
        the item as unlinked. If the agenda itself cannot be read (403/404 on the sub-resource),
        `agenda_items` is empty and `notes` explains why — an empty agenda and an unreadable one
        are different answers. Attendance, minutes as a document, and meeting sections' own
        titles beyond the item link are not exposed by API v3.

        Cross-references: `add_meeting_agenda_item` to extend the agenda; `list_meetings` for the
        id; `list_attachments(container_type='meeting', container_id=<meeting id>)` for files;
        `get_work_package` for a linked ticket. `update_meeting` / `delete_meeting` change or
        remove the meeting itself — the result's `lock_version` is what `update_meeting` echoes.
        """
        payload = await _module_get(
            f"meetings/{meeting_id}",
            module=MEETINGS,
            subject=f"meeting {meeting_id}",
            discovery="Meeting ids come from list_meetings.",
        )
        elements, agenda_note = await _agenda_items(meeting_id)
        notes = [note for note in (agenda_note, _undisclosed_note(elements)) if note]
        return _meeting_detail(
            payload, agenda_items=[_agenda_item(element) for element in elements], notes=notes
        )

    @mcp.tool(
        name="create_meeting",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE),
        annotations=_shared.write_annotations(title="Create meeting"),
    )
    @_shared.tool_errors
    async def create_meeting(
        project_id: Annotated[
            int | str,
            Field(
                description="Numeric id or identifier of the project the meeting belongs to. It "
                "must have the Meetings module enabled and this account needs the 'create "
                "meetings' permission in it."
            ),
        ],
        title: Annotated[
            str,
            Field(description="Meeting title, e.g. 'Sprint 12 planning'."),
        ],
        start_time: Annotated[
            str,
            Field(
                description="Start as ISO 8601 WITH a timezone: '2026-08-03T14:00:00Z' (UTC) or "
                "'2026-08-03T16:00:00+02:00'. A time without an offset is rejected locally "
                "rather than booked in the wrong hour."
            ),
        ],
        duration_minutes: Annotated[
            int,
            Field(
                ge=1,
                description="Scheduled length in minutes (90 = one and a half hours). Sent as "
                "the API's ISO duration; the result reports it back as duration_hours.",
            ),
        ],
        participants: Annotated[
            list[int] | None,
            Field(
                description="User ids to invite, from search_principals or a project's "
                "memberships. Every one of them needs 'view meetings' in the project or the "
                "create is rejected. Omit to let OpenProject invite only the author."
            ),
        ] = None,
    ) -> MeetingDetail:
        """Schedule a meeting in a project and optionally invite participants.

        Use it for "book a review on Thursday" style requests. The call goes through
        `POST /meetings/form` first, so a missing permission, an impossible time or a
        participant who cannot see the project comes back as `violations` naming the attribute
        instead of an opaque rejection.

        Returns the created meeting in the same shape as `get_meeting` (its `agenda_items` are
        empty — add them with `add_meeting_agenda_item`).

        Pitfalls. Check `state` in the result: current OpenProject versions create meetings as
        'draft', which means participants do not see it until it is opened —
        `update_meeting(meeting_id=..., state='open')` publishes it, exactly as the UI does.
        Invitation emails are not sent by an API create. `start_time` needs a timezone — the
        server stores an instant, not a wall-clock time. Recurring meetings cannot be created
        through this tool — use `create_recurring_meeting`.

        Cross-references: `add_meeting_agenda_item(meeting_id=...)` to build the agenda;
        `get_meeting` to read it back; `list_projects` for the project id; `search_principals` (or
        `list_project_memberships`) for participant ids.
        """
        if not title.strip():
            raise InputValidationError(
                "title is empty.",
                hint="Pass the meeting title, e.g. 'Sprint 12 planning'.",
            )

        attributes: dict[str, Any] = {
            "title": title.strip(),
            "startTime": _iso_datetime(start_time, "start_time"),
            "duration": _iso_duration(duration_minutes),
        }
        links: dict[str, Any] = {"project": link("projects", project_id)}
        if participants:
            links["participants"] = [link("users", user_id) for user_id in participants]

        payload = build_write_payload(attributes, links)
        subject = f"project {project_id}"
        discovery = (
            "Project ids come from list_projects. A third reading applies to writes: some "
            "OpenProject versions (16.6 among them) do not route the meetings write API at "
            "all, so creating 404s here even when the module is enabled and list_meetings "
            "works."
        )
        form = await _module_post(
            "meetings/form",
            json=payload,
            module=MEETINGS,
            subject=subject,
            discovery=discovery,
        )
        _raise_meeting_form_errors(form)

        body = _forms.merge_form_payload(_forms.form_payload(form) or {}, payload)
        created = await _module_post(
            "meetings",
            json=body,
            module=MEETINGS,
            subject=subject,
            discovery=discovery,
        )
        return _meeting_detail(created, agenda_items=[], notes=[])

    @mcp.tool(
        name="update_meeting",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE),
        annotations=_shared.write_annotations(title="Update meeting"),
    )
    @_shared.tool_errors
    async def update_meeting(
        meeting_id: Annotated[
            int,
            Field(
                description="Numeric meeting id from list_meetings or get_meeting. Never a "
                "project id or an agenda item id."
            ),
        ],
        title: Annotated[
            str | None,
            Field(description="New meeting title. Omit to leave it alone; it cannot be cleared."),
        ] = None,
        location: Annotated[
            str | None,
            Field(
                description="New room name or meeting URL; REPLACES the stored one. Pass null "
                "or an empty string to clear it. Omit the parameter entirely (the default) to "
                "leave it untouched."
            ),
        ] = KEEP,
        start_time: Annotated[
            str | None,
            Field(
                description="New start as ISO 8601 WITH a timezone: '2026-08-03T14:00:00Z' or "
                "'2026-08-03T16:00:00+02:00'. A time without an offset is rejected locally "
                "rather than booked in the wrong hour. Omit to keep the current time."
            ),
        ] = None,
        duration_minutes: Annotated[
            int | None,
            Field(
                ge=1,
                description="New length in minutes (90 = one and a half hours); the end time "
                "is derived from it. Omit to keep the current duration.",
            ),
        ] = None,
        state: Annotated[
            MeetingState | None,
            Field(
                description="New lifecycle state: 'open' publishes a draft to its participants "
                "(exactly what the UI's publish does), 'in_progress' starts it (required before "
                "outcomes can be recorded), 'closed' freezes it, 'cancelled' calls it off. "
                "There is no dedicated state endpoint upstream — this plain field is it."
            ),
        ] = None,
        participants: Annotated[
            list[int] | None,
            Field(
                description="User ids of the FULL new invite list, from search_principals or a "
                "project's memberships. This REPLACES the whole set — anyone not listed is "
                "uninvited — so read the current list with get_meeting first and send it "
                "complete. Omit to leave the participants untouched."
            ),
        ] = None,
        lock_version: Annotated[
            int | None,
            Field(
                description="The lock_version you read from get_meeting. Pass it and the write "
                "fails loudly (409) if somebody else edited the meeting in the meantime. Omit "
                "it and the current version is fetched and echoed — still safe, just one more "
                "round trip and a slightly wider conflict window."
            ),
        ] = None,
    ) -> MeetingDetail:
        """Change a meeting's title, time, place or invite list — or move its lifecycle state.

        Use it to reschedule ("move Thursday's review to 15:00"), to publish a draft
        (`state='open'`), to start or wrap up a running one (`state='in_progress'` /
        `'closed'` — outcomes can only be recorded while it is in progress), or to fix the
        participants. Only the parameters you pass are sent; omitted fields stay as they are.

        Returns the updated meeting in the same shape as `get_meeting`, including the fresh
        `lock_version` for a follow-up edit.

        Pitfalls. `participants` replaces the entire set — a partial list silently uninvites
        everyone else. A closed meeting accepts a state-only patch (reopening it) and nothing
        else; any other change is rejected with a validation error until it is reopened. A
        `conflict` error (409) means somebody edited the meeting since you read it — the error
        carries the fresh `lock_version` and the differing fields, so re-read, decide, retry
        deliberately. This needs the 'edit meetings' permission, and moving a meeting to
        another project is deliberately not offered.

        Cross-references: `get_meeting` for the current values and the `lock_version`;
        `create_meeting` to schedule a new one; `delete_meeting` to remove one;
        `add_meeting_outcome` for what an 'in_progress' state unlocks.
        """
        attributes: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise InputValidationError(
                    "title is empty.",
                    hint="Omit title to leave it unchanged; a meeting cannot have a blank title.",
                )
            attributes["title"] = title.strip()
        if location != KEEP:
            attributes["location"] = location or ""
        if start_time is not None:
            attributes["startTime"] = _iso_datetime(start_time, "start_time")
        if duration_minutes is not None:
            attributes["duration"] = _iso_duration(duration_minutes)
        if state is not None:
            attributes["state"] = state

        links: dict[str, Any] = {}
        if participants is not None:
            # A PATCH replaces the whole participant set upstream, so the list is
            # sent verbatim — including an (explicitly requested) empty one.
            links["participants"] = [link("users", user_id) for user_id in participants]

        if not attributes and not links:
            raise InputValidationError(
                "update_meeting was called with nothing to change.",
                hint=(
                    "Pass at least one of title, location, start_time, duration_minutes, "
                    "state or participants."
                ),
            )

        try:
            updated = await _module_patch_with_lock(
                f"meetings/{meeting_id}",
                build_write_payload(attributes, links),
                lock_version=lock_version,
                module=MEETINGS_EDIT,
                subject=f"meeting {meeting_id}",
                discovery=MEETING_WRITE_DISCOVERY,
            )
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    "OpenProject rejected the change. The usual causes: the meeting is closed "
                    "(a closed meeting accepts a state-only patch that reopens it, nothing "
                    "else), start_time is invalid, or a participant cannot view meetings in "
                    "the project. 'violations' names the attribute."
                ),
            ) from exc
        elements, agenda_note = await _agenda_items(meeting_id)
        notes = [note for note in (agenda_note, _undisclosed_note(elements)) if note]
        return _meeting_detail(
            updated, agenda_items=[_agenda_item(element) for element in elements], notes=notes
        )

    @mcp.tool(
        name="delete_meeting",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE, _shared.DESTRUCTIVE),
        annotations=_shared.destructive_annotations(title="Delete meeting"),
    )
    @_shared.tool_errors
    async def delete_meeting(
        meeting_id: Annotated[
            int,
            Field(
                description="Numeric meeting id to delete permanently. Read it back with "
                "get_meeting first and show the user the title — meeting ids are not "
                "human-readable."
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description="Must be true. Ask the user to confirm first — the API offers no "
                "undo. Calling with confirm=false returns a confirmation_required error rather "
                "than deleting anything."
            ),
        ] = False,
    ) -> MeetingDeletionResult:
        """Permanently delete a meeting, together with its agenda and recorded outcomes.

        Use only on explicit user instruction. The meeting, its agenda items, their outcomes
        and its attachments all go with it, and OpenProject offers no API-side undo. If the
        meeting merely did not happen, `update_meeting(meeting_id=..., state='cancelled')` is
        the reversible alternative that keeps the record.

        Returns a small confirmation object once OpenProject accepts the deletion.

        Pitfalls. This needs the 'delete meetings' permission, so a 403 is about the account,
        not the id. A 404 means the id is wrong, the meeting was already deleted, or — on
        OpenProject before 17.4 — the meetings write API does not exist at all; the hint
        names all readings.

        Cross-references: `get_meeting` to check what you are about to destroy;
        `update_meeting(state='cancelled')` for the reversible alternative; `list_meetings`
        for the id.
        """
        _shared.require_confirmation(
            confirm,
            action="delete meeting",
            target=f"#{meeting_id}",
            consequence=(
                "The meeting, its agenda items, their recorded outcomes and its attachments "
                "are removed permanently, and the deletion cannot be undone through the API."
            ),
        )
        await _module_delete(
            f"meetings/{meeting_id}",
            module=MEETINGS_DELETE,
            subject=f"meeting {meeting_id}",
            discovery=MEETING_WRITE_DISCOVERY,
        )
        return MeetingDeletionResult(
            id=meeting_id,
            deleted=True,
            message=(
                f"Meeting #{meeting_id} was deleted permanently, together with its agenda "
                "and outcomes."
            ),
        )

    @mcp.tool(
        name="add_meeting_agenda_item",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE),
        annotations=_shared.write_annotations(title="Add meeting agenda item"),
    )
    @_shared.tool_errors
    async def add_meeting_agenda_item(
        meeting_id: Annotated[
            int,
            Field(
                description="Numeric meeting id from list_meetings or get_meeting. The item is "
                "appended to that meeting's last agenda section."
            ),
        ],
        title: Annotated[
            str,
            Field(
                description="Agenda item title, e.g. 'Release readiness'. For a work-package "
                "item the UI shows the work package's subject instead, but a title is still "
                "accepted and stored."
            ),
        ],
        notes: Annotated[
            str | None,
            Field(description="Markdown notes for the item. Omit for none."),
        ] = None,
        duration_minutes: Annotated[
            int | None,
            Field(
                ge=0,
                le=1440,
                description="Planned length of this item in minutes (0-1440). Omit to leave the "
                "item untimed; the meeting's own duration is unaffected either way.",
            ),
        ] = None,
        work_package_id: Annotated[
            int | None,
            Field(
                description="Work package to discuss under this item, from list_work_packages or "
                "search_work_packages. Passing it makes this a work-package item, which is what "
                "puts the meeting into that ticket's Meetings tab. It must be visible to this "
                "account."
            ),
        ] = None,
    ) -> AgendaItemDetail:
        """Add one item to a meeting's agenda, optionally pinned to a work package.

        Use it to build or extend an agenda: "add 'Release readiness' with 15 minutes", or "put
        #1234 on Thursday's agenda". Linking a work package is also how a ticket learns it was
        discussed — the link shows up on the work package's Meetings tab.

        Returns the created item: `{id, title, notes, duration_minutes, position, item_type,
        presenter, work_package, section, outcomes, meeting, created_at}`. `position` is where
        it landed in the agenda; items are appended to the meeting's last section.

        Pitfalls. This needs the 'manage agendas' permission, so a 403 is about the account, not
        the payload. A 422 usually means the work package is not visible to this account or the
        meeting is already closed — `violations` names the attribute. The item's type is fixed
        here: a simple item cannot become a work-package one later, because `itemType` is
        create-only upstream.

        Cross-references: `get_meeting(meeting_id=...)` to see the agenda you are appending to;
        `update_meeting_agenda_item` to fix or reorder the item afterwards;
        `delete_meeting_agenda_item` to remove it; `add_meeting_outcome` to record a decision
        against it; `list_meetings` for the meeting id; `search_work_packages` for the work
        package id.
        """
        if not title.strip():
            raise InputValidationError(
                "title is empty.",
                hint="Pass the agenda item title, e.g. 'Release readiness'.",
            )

        attributes: dict[str, Any] = {"title": title.strip()}
        if notes is not None:
            attributes["notes"] = formattable_field(notes)
        if duration_minutes is not None:
            attributes["durationInMinutes"] = duration_minutes
        links: dict[str, Any] = {"meeting": link("meetings", meeting_id)}
        if work_package_id is not None:
            # A work-package item is a distinct item_type upstream; without it the
            # link is stored but the item still renders as free text.
            attributes["itemType"] = "work_package"
            links["workPackage"] = link("work_packages", work_package_id)

        payload = build_write_payload(attributes, links)
        try:
            created = await _module_post(
                "meeting_agenda_items",
                json=payload,
                module=AGENDAS,
                subject=f"meeting {meeting_id}",
                discovery=(
                    "Meeting ids come from list_meetings. A third reading applies to writes: "
                    "some OpenProject versions (16.6 among them) do not route the meetings "
                    "write API at all, so this 404s regardless of the meeting id even when "
                    "the module is enabled."
                ),
            )
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    "OpenProject rejected the agenda item. The usual causes: the work package is "
                    "not visible to this account (check it with get_work_package), the meeting is "
                    "closed, or duration_minutes is outside 0-1440. 'violations' names the "
                    "attribute."
                ),
            ) from exc
        return _agenda_item_detail(created)

    @mcp.tool(
        name="update_meeting_agenda_item",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE),
        annotations=_shared.write_annotations(title="Update meeting agenda item"),
    )
    @_shared.tool_errors
    async def update_meeting_agenda_item(
        agenda_item_id: Annotated[
            int,
            Field(
                description="Numeric agenda item id from get_meeting's agenda_items (or from "
                "add_meeting_agenda_item's result). Not the meeting id, not a work package id."
            ),
        ],
        title: Annotated[
            str | None,
            Field(
                description="New item title. Omit to leave it alone; it cannot be cleared, "
                "because simple items require one."
            ),
        ] = None,
        notes: Annotated[
            str | None,
            Field(
                description="New markdown notes; they REPLACE the existing text, so read the "
                "current one with get_meeting first if you mean to extend it. Pass null or an "
                "empty string to clear. Omit the parameter entirely (the default) to leave it "
                "untouched."
            ),
        ] = KEEP,
        duration_minutes: Annotated[
            int | None,
            Field(
                ge=0,
                le=1440,
                description="New planned length of this item in minutes (0-1440). Omit to "
                "leave it as it is.",
            ),
        ] = None,
        position: Annotated[
            int | None,
            Field(
                ge=1,
                description="New 1-based position within the item's section — this is how the "
                "agenda is reordered; the other items shift around it. Omit to keep the order.",
            ),
        ] = None,
        presenter_id: Annotated[
            int | None,
            Field(
                description="User id of the new presenter, from search_principals or the meeting's "
                "participants. They must be able to view meetings in the project. Omit to "
                "leave the presenter alone."
            ),
        ] = None,
        work_package_id: Annotated[
            int | None,
            Field(
                description="Re-point a work-package item at another work package, which must "
                "be visible to this account. Omit to leave the link alone; a simple item "
                "cannot be turned into a work-package one (itemType is create-only)."
            ),
        ] = None,
        lock_version: Annotated[
            int | None,
            Field(
                description="The item's lock_version as read from get_meeting. Pass it and the "
                "write fails loudly (409) on a concurrent edit; omit it and the current "
                "version is fetched and echoed."
            ),
        ] = None,
    ) -> AgendaItemDetail:
        """Edit one agenda item: retitle it, rewrite its notes, retime, reorder or re-link it.

        Use it for "give that item 20 minutes", "move it to the top" (`position=1`), "let
        Grace present it", or to fix a wrong work-package link. Only the parameters you pass
        are sent; everything else is left exactly as it is.

        Returns the updated item in the same shape as `add_meeting_agenda_item`, including the
        fresh `lock_version` for a follow-up edit.

        Pitfalls. Once the meeting is CLOSED its agenda is frozen — every write answers a
        validation error until the meeting is reopened with `update_meeting(state='open')`.
        The item's type is fixed at creation: `itemType` is create-only upstream, so this tool
        never sends it and a simple item stays a simple item. A 422 otherwise usually means
        the presenter or work package is not visible in the project — `violations` names the
        attribute. This needs the 'manage agendas' permission (403 otherwise), and a 409 means
        a concurrent edit — the error carries the fresh `lock_version`.

        Cross-references: `get_meeting` for the item ids, current values and lock versions;
        `delete_meeting_agenda_item` to remove the item; `add_meeting_outcome` to record what
        was decided under it.
        """
        attributes: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise InputValidationError(
                    "title is empty.",
                    hint="Omit title to leave it unchanged; a simple item cannot lose its title.",
                )
            attributes["title"] = title.strip()
        if notes != KEEP:
            attributes["notes"] = formattable_field(notes or "")
        if duration_minutes is not None:
            attributes["durationInMinutes"] = duration_minutes
        if position is not None:
            attributes["position"] = position

        links: dict[str, Any] = {}
        if presenter_id is not None:
            links["presenter"] = link("users", presenter_id)
        if work_package_id is not None:
            links["workPackage"] = link("work_packages", work_package_id)

        if not attributes and not links:
            raise InputValidationError(
                "update_meeting_agenda_item was called with nothing to change.",
                hint=(
                    "Pass at least one of title, notes, duration_minutes, position, "
                    "presenter_id or work_package_id."
                ),
            )

        # itemType is deliberately never sent: it is create-only upstream and
        # PATCHing it answers 422 PropertyIsReadOnly.
        try:
            updated = await _module_patch_with_lock(
                f"meeting_agenda_items/{agenda_item_id}",
                build_write_payload(attributes, links),
                lock_version=lock_version,
                module=AGENDAS,
                subject=f"agenda item {agenda_item_id}",
                discovery=AGENDA_WRITE_DISCOVERY,
            )
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    "OpenProject rejected the change. The usual causes: the meeting is closed "
                    "(its agenda is frozen until update_meeting reopens it), the presenter or "
                    "work package is not visible in the project, or duration_minutes is "
                    "outside 0-1440. 'violations' names the attribute."
                ),
            ) from exc
        return _agenda_item_detail(updated)

    @mcp.tool(
        name="delete_meeting_agenda_item",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE, _shared.DESTRUCTIVE),
        annotations=_shared.destructive_annotations(title="Delete meeting agenda item"),
    )
    @_shared.tool_errors
    async def delete_meeting_agenda_item(
        agenda_item_id: Annotated[
            int,
            Field(
                description="Numeric agenda item id to delete, from get_meeting's "
                "agenda_items. Check the item's title there first — it is not the meeting id."
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description="Must be true. Ask the user to confirm first — the API offers no "
                "undo. Calling with confirm=false returns a confirmation_required error rather "
                "than deleting anything."
            ),
        ] = False,
    ) -> MeetingDeletionResult:
        """Permanently delete one agenda item, with any outcomes recorded against it.

        Use only on explicit user instruction. The item and its recorded outcomes disappear
        from the agenda for good; the items after it move up. The meeting itself is untouched.

        Returns a small confirmation object once OpenProject accepts the deletion.

        Pitfalls. A CLOSED meeting's agenda is frozen: the delete answers a validation error,
        not a 403, until the meeting is reopened with `update_meeting(state='open')`. This
        needs the 'manage agendas' permission. A 404 means the id is wrong, the item is
        already gone — or, on OpenProject before 17.6, that the flat agenda-item write routes
        do not exist; the hint names all readings.

        Cross-references: `get_meeting` to check which item the id points at;
        `update_meeting_agenda_item` when the item only needs fixing, not removing.
        """
        _shared.require_confirmation(
            confirm,
            action="delete agenda item",
            target=f"#{agenda_item_id}",
            consequence=(
                "The agenda item and any outcomes recorded against it are removed "
                "permanently, and the deletion cannot be undone through the API."
            ),
        )
        try:
            await _module_delete(
                f"meeting_agenda_items/{agenda_item_id}",
                module=AGENDAS,
                subject=f"agenda item {agenda_item_id}",
                discovery=AGENDA_WRITE_DISCOVERY,
            )
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    "OpenProject refused the deletion — usually because the meeting is closed "
                    "and its agenda is frozen. Reopen it with update_meeting(state='open') "
                    "first, or leave the record as it is."
                ),
            ) from exc
        return MeetingDeletionResult(
            id=agenda_item_id,
            deleted=True,
            message=(
                f"Agenda item #{agenda_item_id} was deleted permanently, together with its "
                "outcomes."
            ),
        )

    @mcp.tool(
        name="add_meeting_outcome",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE),
        annotations=_shared.write_annotations(title="Add meeting outcome"),
    )
    @_shared.tool_errors
    async def add_meeting_outcome(
        agenda_item_id: Annotated[
            int,
            Field(
                description="Numeric agenda item id the outcome is recorded against, from "
                "get_meeting's agenda_items. Not the meeting id."
            ),
        ],
        kind: Annotated[
            OutcomeKind,
            Field(
                description="'information' (a note for the minutes, requires notes), "
                "'decision' (what was decided), or 'work_package' (the outcome IS a follow-up "
                "ticket, requires work_package_id)."
            ),
        ] = "information",
        notes: Annotated[
            str | None,
            Field(
                description="The outcome text as markdown. Required for kind='information'; "
                "optional but usually worth writing for the other kinds."
            ),
        ] = None,
        work_package_id: Annotated[
            int | None,
            Field(
                description="Work package the outcome points at, from search_work_packages. "
                "Required for kind='work_package'; it must be visible to this account."
            ),
        ] = None,
    ) -> MeetingOutcomeDetail:
        """Record an outcome — a decision, a note, a follow-up ticket — against an agenda item.

        This is how minutes are written through the API: "decision: ship on Friday" becomes
        `kind='decision'` with the text in `notes`; linking the follow-up work package makes
        it `kind='work_package'`. Outcomes appear under their agenda item in `get_meeting`.

        Returns the created outcome: `{id, kind, notes, author, work_package, agenda_item}`.

        Pitfalls — the timing rule matters most. Outcomes can only be written while the
        meeting state is exactly 'in_progress': before that, and again once it is closed,
        every outcome write answers a validation error. Start the meeting with
        `update_meeting(meeting_id=..., state='in_progress')` first. Items in a backlog
        section refuse outcomes the same way. This needs the 'manage outcomes' permission, so
        a 403 is about the account, not the payload. On OpenProject before 17.6 there is no
        outcomes API at all — the 404 hint says so.

        Cross-references: `get_meeting` for the agenda item id and to read the outcome back;
        `update_meeting` to put the meeting into 'in_progress'; `update_meeting_outcome` /
        `delete_meeting_outcome` to correct or remove it while the meeting still runs.
        """
        if kind == "information" and not (notes and notes.strip()):
            raise InputValidationError(
                "notes is required for kind='information'.",
                hint=(
                    "An information outcome is its text — pass notes, or use kind='decision' "
                    "for a bare decision marker."
                ),
            )
        if kind == "work_package" and work_package_id is None:
            raise InputValidationError(
                "work_package_id is required for kind='work_package'.",
                hint=(
                    "A work_package outcome points at the follow-up ticket — pass "
                    "work_package_id, or use kind='decision'/'information' for a text-only "
                    "outcome."
                ),
            )

        attributes: dict[str, Any] = {"kind": kind}
        if notes is not None:
            attributes["notes"] = formattable_field(notes)
        links: dict[str, Any] = {"agendaItem": link("meeting_agenda_items", agenda_item_id)}
        if work_package_id is not None:
            links["workPackage"] = link("work_packages", work_package_id)

        try:
            created = await _module_post(
                "meeting_outcomes",
                json=build_write_payload(attributes, links),
                module=OUTCOMES,
                subject=f"agenda item {agenda_item_id}",
                discovery=OUTCOME_WRITE_DISCOVERY,
            )
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    "OpenProject rejected the outcome. Outcomes can only be written while the "
                    "meeting state is exactly 'in_progress' — update_meeting(state="
                    "'in_progress') starts it — the agenda item must not sit in a backlog "
                    "section, and a linked work package must be visible to this account. "
                    "'violations' names the attribute."
                ),
            ) from exc
        return _outcome_detail(created)

    @mcp.tool(
        name="update_meeting_outcome",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE),
        annotations=_shared.write_annotations(title="Update meeting outcome"),
    )
    @_shared.tool_errors
    async def update_meeting_outcome(
        outcome_id: Annotated[
            int,
            Field(
                description="Numeric outcome id from get_meeting's agenda_items[].outcomes or "
                "from add_meeting_outcome's result. Not an agenda item id."
            ),
        ],
        kind: Annotated[
            OutcomeKind | None,
            Field(
                description="New kind: 'information', 'decision' or 'work_package'. Omit to "
                "keep the current one. Switching to 'work_package' needs a linked work "
                "package; switching to 'information' needs notes."
            ),
        ] = None,
        notes: Annotated[
            str | None,
            Field(
                description="New outcome text as markdown; it REPLACES the stored text. Omit "
                "to leave it untouched."
            ),
        ] = None,
        work_package_id: Annotated[
            int | None,
            Field(
                description="Re-point the outcome at another work package, which must be "
                "visible to this account. Omit to leave the link alone."
            ),
        ] = None,
    ) -> MeetingOutcomeDetail:
        """Correct a recorded outcome's kind, text or linked work package.

        Use it while the meeting still runs: fix a typo in the minutes, upgrade an
        information note to a decision, or attach the follow-up ticket that was created after
        the fact. Only the parameters you pass are sent.

        Returns the updated outcome in the same shape as `add_meeting_outcome`.

        Pitfalls. The same timing rule as every outcome write: this only works while the
        meeting state is exactly 'in_progress' — once it is closed, the minutes are what they
        are, and the write answers a validation error. Outcomes carry no `lock_version`
        upstream, so there is nothing to echo and a simultaneous edit by somebody else is
        silently overwritten — read the outcome via `get_meeting` first. This needs the
        'manage outcomes' permission (403 otherwise).

        Cross-references: `get_meeting` for the outcome id and current text;
        `add_meeting_outcome` for the kind rules; `delete_meeting_outcome` to remove it.
        """
        attributes: dict[str, Any] = {}
        if kind is not None:
            attributes["kind"] = kind
        if notes is not None:
            attributes["notes"] = formattable_field(notes)
        links: dict[str, Any] = {}
        if work_package_id is not None:
            links["workPackage"] = link("work_packages", work_package_id)

        if not attributes and not links:
            raise InputValidationError(
                "update_meeting_outcome was called with nothing to change.",
                hint="Pass at least one of kind, notes or work_package_id.",
            )

        # Outcomes carry no lockVersion upstream, so a plain PATCH — not
        # patch_with_lock; inventing a version would be rejected (SPEC §4.4).
        try:
            updated = await _module_patch(
                f"meeting_outcomes/{outcome_id}",
                json=build_write_payload(attributes, links),
                module=OUTCOMES,
                subject=f"outcome {outcome_id}",
                discovery=OUTCOME_WRITE_DISCOVERY,
            )
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    "OpenProject rejected the change. Outcomes can only be edited while the "
                    "meeting state is exactly 'in_progress', kind='information' requires "
                    "notes, kind='work_package' requires a visible linked work package, and "
                    "the agenda item must not sit in a backlog section. 'violations' names "
                    "the attribute."
                ),
            ) from exc
        return _outcome_detail(updated)

    @mcp.tool(
        name="delete_meeting_outcome",
        tags=_shared.tool_tags(_shared.GROUP_MEETINGS, _shared.WRITE, _shared.DESTRUCTIVE),
        annotations=_shared.destructive_annotations(title="Delete meeting outcome"),
    )
    @_shared.tool_errors
    async def delete_meeting_outcome(
        outcome_id: Annotated[
            int,
            Field(
                description="Numeric outcome id to delete, from get_meeting's "
                "agenda_items[].outcomes. Check its text there first — ids are not "
                "human-readable."
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description="Must be true. Ask the user to confirm first — the API offers no "
                "undo. Calling with confirm=false returns a confirmation_required error rather "
                "than deleting anything."
            ),
        ] = False,
    ) -> MeetingDeletionResult:
        """Permanently delete a recorded outcome from a running meeting's minutes.

        Use only on explicit user instruction, for an outcome recorded by mistake or against
        the wrong item. If the text is merely wrong, `update_meeting_outcome` corrects it and
        keeps the record.

        Returns a small confirmation object once OpenProject accepts the deletion.

        Pitfalls. The outcome timing rule applies to deletion too: it only works while the
        meeting state is exactly 'in_progress' — a closed meeting's minutes are frozen, and
        the delete answers a validation error, not a 403. This needs the 'manage outcomes'
        permission. A 404 means the id is wrong, the outcome is already gone — or, on
        OpenProject before 17.6, that there is no outcomes API at all.

        Cross-references: `get_meeting` to check which outcome the id points at;
        `update_meeting_outcome` for the reversible fix.
        """
        _shared.require_confirmation(
            confirm,
            action="delete outcome",
            target=f"#{outcome_id}",
            consequence=(
                "The recorded outcome disappears from the meeting's minutes permanently, and "
                "the deletion cannot be undone through the API."
            ),
        )
        try:
            await _module_delete(
                f"meeting_outcomes/{outcome_id}",
                module=OUTCOMES,
                subject=f"outcome {outcome_id}",
                discovery=OUTCOME_WRITE_DISCOVERY,
            )
        except ValidationFailedError as exc:
            raise ValidationFailedError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                violations=exc.violations,
                hint=(
                    "OpenProject refused the deletion — outcomes can only be removed while "
                    "the meeting state is exactly 'in_progress'. A closed meeting's minutes "
                    "are frozen; reopen deliberately with update_meeting if the record really "
                    "must change."
                ),
            ) from exc
        return MeetingDeletionResult(
            id=outcome_id,
            deleted=True,
            message=f"Outcome #{outcome_id} was deleted permanently from the meeting's minutes.",
        )

    @mcp.tool(
        name="get_wiki_page",
        tags=_shared.tool_tags(_shared.GROUP_WIKI, _shared.READ),
        annotations=_shared.read_annotations(title="Get wiki page"),
    )
    @_shared.tool_errors
    async def get_wiki_page(
        wiki_page_id: Annotated[
            int,
            Field(
                description="Numeric wiki page id. There is no wiki index or search in API v3, "
                "so this id can only come from a page URL the user gives you — the number in "
                "/projects/<project>/wiki/<id> or in the page's 'Info' view. Never guess it."
            ),
        ],
    ) -> WikiPage:
        """Read a wiki page's identity and project — NOT its content.

        Two limits define this tool, and both must be passed on to the user rather than worked
        around. First, `wiki_page_id` comes from a wiki page URL the user supplies: API v3 has no
        wiki index and no wiki search, so there is no way to look a page up by title or to list a
        project's pages. Second, the page's CONTENT is not exposed by the API at all — the
        response carries only `{id, title, project}` plus the page's attachments, and `notes`
        repeats that in-band.

        So: use it to confirm which page a URL points at, to get the project a page belongs to,
        and as the step before fetching its files. To read the text, ask the user to paste it or
        open the page in the browser.

        Pitfalls. A 404 means the id is wrong, the page was deleted, or the wiki is disabled in
        that project — it does not mean the wiki is empty. Sub-pages, revisions, page history and
        wiki-page↔work-package links are not exposed either. Creating or editing wiki pages is
        not supported by this server.

        Cross-references: `list_attachments(container_type='wiki_page', container_id=<id>)` lists
        the files on the page and `download_attachment` fetches one; `get_project_metadata` for
        what the project does expose.
        """
        payload = await _module_get(
            f"wiki_pages/{wiki_page_id}",
            module=WIKI,
            subject=f"wiki page {wiki_page_id}",
            discovery=(
                "Wiki page ids come from a page URL the user supplies — API v3 has no wiki "
                "index or search."
            ),
        )
        return WikiPage(
            id=hal.self_id(payload),
            title=payload.get("title"),
            project=Ref.from_hal(payload, "project"),
            notes=[WIKI_CONTENT_NOTE],
        )

    @mcp.tool(
        name="list_documents",
        tags=_shared.tool_tags(_shared.GROUP_DOCUMENTS, _shared.READ),
        annotations=_shared.read_annotations(title="List documents"),
    )
    @_shared.tool_errors
    async def list_documents(
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=100, description="Documents per page (max 100).")
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[DocumentRow]:
        """List the documents visible to you, across every project.

        Documents are OpenProject's filing cabinet: a title, a description, and attached files.
        Use this to find a document id for `get_document` or for
        `list_attachments(container_type='document', ...)`.

        Returns the standard list envelope: rows of `{id, title, project, created_at,
        updated_at}` plus `pagination`. The description is deliberately left out of the rows —
        `get_document` returns it in full.

        Pitfalls. This is instance-wide: the endpoint takes no project parameter here, so filter
        by reading `project` on the rows, and page through rather than assuming page one is
        everything (`pagination.has_more` says). Documents are a module: where it is not
        installed, or not enabled in any project you can see (404), or where this account may not
        read documents (403), the call still SUCCEEDS with an empty `items` and the reason in
        `notes` — an empty list with a note does not mean no documents exist, so read `notes`
        first. The files themselves are attachments, not part of these rows.

        Cross-references: `get_document(document_id=...)` for the description;
        `list_attachments(container_type='document', container_id=...)` then
        `download_attachment` for the files.
        """
        payload, degraded = await _module_list_get(
            "documents",
            module=DOCUMENTS,
            subject="the documents collection",
            discovery="Document ids come from list_documents.",
            params=pagination_params(page, page_size),
        )
        if payload is None:
            # Ⓜ module absent or unreadable — a note, never a silent "no documents" (G5).
            return _unavailable_envelope(degraded)

        collection = hal.collection(payload)
        rows = [_document_row(element) for element in collection]
        return _shared.envelope_from_collection(collection, rows, page=page, page_size=page_size)

    @mcp.tool(
        name="get_document",
        tags=_shared.tool_tags(_shared.GROUP_DOCUMENTS, _shared.READ),
        annotations=_shared.read_annotations(title="Get document"),
    )
    @_shared.tool_errors
    async def get_document(
        document_id: Annotated[
            int,
            Field(
                description="Numeric document id from list_documents (it is also the number in "
                "a /documents/<id> UI URL)."
            ),
        ],
    ) -> DocumentDetail:
        """Read one document with its full description text.

        Use it after `list_documents` when the title is not enough: this adds `description` as
        markdown, alongside `{id, title, project, created_at, updated_at}`.

        Pitfalls. The attached files are not part of this result — list them with
        `list_attachments(container_type='document', container_id=<this id>)`. A document created
        with OpenProject's block editor keeps its rich content in a field API v3 does not render,
        so `description` can be empty for a document that clearly has text in the UI; say so
        rather than reporting the document as blank. Editing documents is not supported by
        this server.

        Cross-references: `list_documents` for the id; `download_attachment` for the files.
        """
        payload = await _module_get(
            f"documents/{document_id}",
            module=DOCUMENTS,
            subject=f"document {document_id}",
            discovery="Document ids come from list_documents.",
        )
        return DocumentDetail(
            **_document_row(payload).model_dump(),
            description=hal.formattable(payload.get("description")),
        )

    @mcp.tool(
        name="list_budgets",
        tags=_shared.tool_tags(_shared.GROUP_BUDGETS, _shared.READ),
        annotations=_shared.read_annotations(title="List budgets"),
    )
    @_shared.tool_errors
    async def list_budgets(
        project_id: Annotated[
            int | str,
            Field(
                description="Numeric project id or URL identifier, from list_projects. Budgets "
                "are always read per project; there is no instance-wide budget listing."
            ),
        ],
    ) -> ListEnvelope[BudgetRow]:
        """List a project's budgets — their ids and names, which is all API v3 exposes.

        Use it to see whether a project tracks budgets at all and to get a budget id, which is
        what `list_attachments(container_type='budget', ...)` consumes.

        Returns the standard list envelope with rows of `{id, subject}`. The collection is
        fetched in full, so `has_more` is false.

        Pitfalls — read before answering a money question. API v3's budget representer carries
        **no amounts**: planned costs, spent costs, labor/material breakdowns and the assigned
        work packages are simply not there. Do not infer them and do not present a budget row as
        financial data; point the user at the budget in the UI, or use
        `get_project_report_data` / `list_time_entries` for the effort side. Budgets are also a
        module: a 404 (not installed, or not enabled in this project) and a 403 (this account
        lacks 'view budgets') both come back as a SUCCESSFUL call with an empty `items` and the
        reason in `notes`. Neither means the project has no budgets, so check `notes` before
        answering — only an empty list with no notes means there are none.

        Cross-references: `list_projects` for the project id; `list_time_entries` for logged
        effort; `list_attachments(container_type='budget', container_id=...)` for budget files.
        """
        payload, degraded = await _module_list_get(
            f"projects/{project_id}/budgets",
            module=BUDGETS,
            subject=f"project {project_id}",
            discovery="Project ids come from list_projects.",
        )
        if payload is None:
            # Ⓜ module absent or unreadable — a note, never a silent "no budgets" (G5).
            return _unavailable_envelope(degraded)

        collection = hal.collection(payload)
        rows = [_budget_row(element) for element in collection]
        return _shared.build_envelope(
            rows,
            total=collection.total or len(rows),
            page=1,
            page_size=max(len(rows), 1),
        )
