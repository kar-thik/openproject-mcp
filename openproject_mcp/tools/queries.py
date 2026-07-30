"""Saved-query tools (SPEC §6.7 — Phase 2).

Lands here:

=======================  ======  ==================================================
Tool                     Phase   Endpoint(s)
=======================  ======  ==================================================
🔍 ``list_queries``      2       ``GET /queries?filters=[project]``
🔍 ``run_query``         2       ``GET /queries/{id}`` (results embedded)
✏️ ``save_query``        3       form → ``POST /queries`` (+ ``PATCH …/star``)
=======================  ======  ==================================================

Non-negotiables for this module:

* **Queries run on read.** ``GET /queries/{id}`` embeds a fully computed
  ``WorkPackageCollection`` under ``_embedded.results``; there is no separate
  "execute" call. ``offset``/``pageSize``/``filters`` sent alongside override the
  stored properties **for this call only** — nothing is ever written back.
* Results are projected to the same ``WorkPackageRow`` and the same §9.3
  envelope ``list_work_packages`` returns, so a saved view is interchangeable
  with a filtered listing. Groups and sums come from the embedded collection and
  are computed server-side over the whole result set, never over one page.
* ``override_filters`` is the typed escape hatch (§9.2), identical in shape to
  ``list_work_packages.raw_filters`` — and it **replaces** the stored filters
  wholesale rather than adding to them, which is stated in the description
  because the API gives no way to merge.
* The queries endpoint has its own filter set; its names are registered with
  :func:`register_filter_type` under the ``queries`` resource instead of being
  added to the shared table.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import InputValidationError, OpenProjectError
from openproject_mcp.client.filters import (
    DEFAULT_PAGE_SIZE,
    WORK_PACKAGE_SORT_KEYS,
    Filter,
    FilterType,
    Op,
    RawFilter,
    filter_from_raw,
    make_filter,
    pagination_params,
    register_filter_type,
    serialize_filters,
    serialize_sort_by,
    to_wire_name,
)
from openproject_mcp.client.payloads import build_write_payload, link
from openproject_mcp.projections import ListEnvelope, Ref, WorkPackageRow
from openproject_mcp.tools import _forms, _shared

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["QueryInfo", "QueryResults", "QueryRow", "SavedQuery", "register"]

#: Resource key for the queries endpoint's own filter set.
QUERIES_RESOURCE = "queries"

#: ``anthropic/maxResultSizeChars`` for ``run_query`` (SPEC §5.4): a saved view
#: can legitimately return a hundred rows plus groups and sums.
MAX_RESULT_CHARS = 100_000

OVERRIDE_NOTE = (
    "override_filters replaced the stored filters for this call only; the saved query is "
    "unchanged. Its stored definition is in 'query.filters'."
)
NO_RESULTS_NOTE = (
    "This instance returned the query without embedded results, so no work packages could "
    "be read. Re-run without override_filters, or list the work packages directly with "
    "list_work_packages."
)
GLOBAL_QUERY_NOTE = (
    "Rows with project=null are global queries (saved outside any project); the rest belong "
    "to the project shown."
)

#: Rendered in a filter summary for the empty bound of an open-ended range.
OPEN_BOUND = "(open)"


class QueryRow(BaseModel):
    """Compact saved-query row."""

    id: int | str | None = Field(
        default=None, description="Query id — pass it to run_query as query_id."
    )
    name: str | None = Field(default=None, description="Query name as its author saved it.")
    project: Ref | None = Field(
        default=None,
        description="Owning project, or null for a global (cross-project) query.",
    )
    public: bool = Field(
        default=False,
        description="True when shared with everyone who can see the project; false when "
        "private to its owner.",
    )
    starred: bool = Field(
        default=False, description="True when the current user starred (favorited) it."
    )
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class QueryInfo(QueryRow):
    """The stored definition of the query that just ran."""

    filters: list[str] = Field(
        default_factory=list[str],
        description="The stored filters as readable sentences, e.g. 'Status open' or "
        "'Assignee is (OR) Grace Hopper'. Empty when the query filters nothing.",
    )
    group_by: str | None = Field(
        default=None, description="Column the results are grouped by, when the query groups."
    )
    sort_by: list[str] = Field(
        default_factory=list[str], description="Stored sort order, e.g. ['Finish date asc']."
    )
    display_sums: bool = Field(
        default=False,
        description="True when the query asks for totals; then the result 'sums' is populated.",
    )


class QueryResults(ListEnvelope[WorkPackageRow]):
    """A saved query's results: the standard list envelope plus what actually ran."""

    query: QueryInfo = Field(
        description="The stored query definition, so the rows can be interpreted."
    )


# --- projections ----------------------------------------------------------


def _bool(value: Any) -> bool:
    return value is True


def _query_row(element: Mapping[str, Any]) -> QueryRow:
    return QueryRow(
        id=hal.self_id(element),
        name=element.get("name"),
        project=Ref.from_hal(element, "project"),
        public=_bool(element.get("public")),
        starred=_bool(element.get("starred")),
        updated_at=element.get("updatedAt"),
    )


def _link_title(payload: Mapping[str, Any], key: str) -> str | None:
    """A link's title, falling back to the id in its href (``…/operators/%3D`` → ``=``)."""
    resolved = hal.ref(payload, key)
    if resolved is None:
        return None
    if resolved.name:
        return resolved.name
    return str(resolved.id) if resolved.id is not None else None


def _filter_values(entry: Mapping[str, Any]) -> list[str]:
    """Filter values as text: link titles for resources, plain values otherwise."""
    values = [
        item.name or (str(item.id) if item.id is not None else "")
        for item in hal.refs(entry, "values")
    ]
    if values:
        return values
    raw = hal.as_array(entry.get("values"))
    if raw is not None:
        return [str(item) if item is not None else "" for item in raw]
    return []


def _filter_summary(entry: Mapping[str, Any]) -> str:
    """One stored filter as a readable sentence: '<field> <operator> <values>'."""
    name = _link_title(entry, "filter") or entry.get("name") or "filter"
    operator = _link_title(entry, "operator") or ""
    values = ", ".join(value or OPEN_BOUND for value in _filter_values(entry))
    return " ".join(part for part in (str(name), operator, values) if part).strip()


def _filter_summaries(payload: Mapping[str, Any]) -> list[str]:
    return [_filter_summary(entry) for entry in hal.as_objects(payload.get("filters"))]


def _query_info(payload: Mapping[str, Any]) -> QueryInfo:
    return QueryInfo(
        **_query_row(payload).model_dump(),
        filters=_filter_summaries(payload),
        group_by=_link_title(payload, "groupBy"),
        sort_by=[item.name for item in hal.refs(payload, "sortBy") if item.name],
        display_sums=_bool(payload.get("sums")),
    )


# --- Phase 3: saving a view (SPEC §6.7, §4.5) -----------------------------

#: A query's parts are written as links into the query metadata collections, not
#: as names: the filter, its operator, the sort criteria and the grouping column
#: all address ``/api/v3/queries/...`` resources.
QUERY_FILTER_HREF = "/api/v3/queries/filters/{name}"
QUERY_OPERATOR_HREF = "/api/v3/queries/operators/{operator}"
QUERY_SORT_HREF = "/api/v3/queries/sort_bys/{column}-{direction}"
QUERY_GROUP_HREF = "/api/v3/queries/group_bys/{column}"

#: Filter names whose values a query stores as *resources*, mapped to the API
#: collection those ids live in. Their values must be written as
#: ``_links.values`` hrefs, not as a plain array: the filter representer branches
#: on the filter being object-backed and reads each entry's href to get the id,
#: so a bare ``["12"]`` is a parse failure upstream rather than a validation
#: error. Only the id is taken out of the href, which is why the principal
#: filters may name ``principals`` even though a value can be a user, a group or
#: a placeholder user.
#:
#: The table is explicit rather than derived from :class:`FilterType`, because
#: "list-valued" is a different question: ``reason`` and ``entityType`` are list
#: filters over plain strings and would be corrupted by an href. Names that are
#: absent — custom fields, instance-specific filters — keep the plain array;
#: ``/api/v3/queries/filter_instance_schemas/{name}`` is where an unknown one's
#: value type can be looked up.
QUERY_FILTER_VALUE_RESOURCES: dict[str, str] = {
    "id": "work_packages",
    "parent": "work_packages",
    "ancestor": "work_packages",
    "children": "work_packages",
    "status": "statuses",
    "type": "types",
    "priority": "priorities",
    "version": "versions",
    "category": "categories",
    "project": "projects",
    "subprojectId": "projects",
    "onlySubproject": "projects",
    "author": "principals",
    "assignee": "principals",
    "assigneeOrGroup": "principals",
    "responsible": "principals",
    "watcher": "principals",
    "group": "groups",
    "role": "roles",
}

STAR_FAILED_NOTE = (
    "The query was saved but starring it failed ({message}). The view exists and is usable — "
    "star it in the OpenProject UI, or ignore it; nothing is retried automatically."
)
STAR_NO_ECHO_NOTE = (
    "OpenProject accepted the star request but did not return the updated query, so 'starred' "
    "below is the state at creation time. Re-read it with list_queries if it matters."
)
FILTERS_CHANGED_NOTE = (
    "{sent} filters were sent but the saved query stores {stored}: OpenProject dropped or merged "
    "some. 'filters' below is what it actually kept — check it before relying on the view."
)


class SavedQuery(QueryInfo):
    """The query ``save_query`` created, exactly as OpenProject stored it."""

    notes: list[str] = Field(
        default_factory=list[str],
        description="What happened beyond the create itself: a failed star, filters OpenProject "
        "did not keep. Empty when everything landed as asked.",
    )


def _query_filter_payload(entry: Filter) -> dict[str, Any]:
    """One filter as the create payload spells it.

    The filter and its operator are always links. The values depend on the
    filter: a resource-valued one (status, assignee, type, version, …) writes
    them as ``_links.values`` hrefs, because OpenProject reads the id back out of
    each href; everything else (dates, subject, custom fields) keeps the plain
    array. The form is asked to re-render the whole set before the commit, so an
    instance that spells a filter differently still gets its own shape.
    """
    links: dict[str, Any] = {
        "filter": {"href": QUERY_FILTER_HREF.format(name=entry.name)},
        "operator": {"href": QUERY_OPERATOR_HREF.format(operator=quote(entry.operator, safe=""))},
    }
    resource = QUERY_FILTER_VALUE_RESOURCES.get(entry.name)
    if resource is None:
        return {"_links": links, "values": list(entry.values)}
    links["values"] = [link(resource, value) for value in entry.values]
    return {"_links": links}


def _sort_by_links(sort_by: Sequence[Sequence[str]] | None) -> list[dict[str, str]]:
    """``[["due_date", "asc"]]`` → the ``sortBy`` link array.

    Validation (known key, known direction) is the shared sort validator's, so an
    unknown key fails locally with the allowed set listed instead of costing a
    round trip (G2).
    """
    serialized = serialize_sort_by(sort_by, allowed_keys=WORK_PACKAGE_SORT_KEYS)
    if serialized is None:
        return []
    pairs: list[list[str]] = json.loads(serialized)
    return [
        {"href": QUERY_SORT_HREF.format(column=column, direction=direction)}
        for column, direction in pairs
    ]


def _query_form_hints(form: Mapping[str, Any], errors: Mapping[str, Any]) -> list[str]:
    """The query-specific half of a form rejection: what a valid value looks like."""
    hints: list[str] = []
    if "name" in errors:
        hints.append("A query needs a non-empty name; list_queries shows the ones in use.")
    if "filters" in errors:
        hints.append(
            "Every filter name must exist in this query's context — a project-scoped filter "
            "(version, category, subprojectId) is invalid on a global query, and custom fields "
            "are spelled customField{N} (get_work_package_schema lists them). The same names "
            "work in list_work_packages(raw_filters=...)."
        )
    if "sortBy" in errors or "sortCriteria" in errors:
        hints.append(
            "sort_by keys are snake_case work-package columns, e.g. [['due_date', 'asc']]."
        )
    if "groupBy" in errors:
        hints.append(
            "group_by is a single work-package column such as 'status', 'assignee', 'type' or "
            "'version' — not a list, and not a custom sort key."
        )
    if "project" in errors:
        hints.append(
            "project_id must be a numeric project id you may save queries in; list_projects "
            "shows the candidates. Omit it for a global (cross-project) view."
        )
    return hints


def _raise_query_form_validation_errors(form: Mapping[str, Any]) -> None:
    """Turn a query form's ``validationErrors`` into a typed 422 with a usable hint."""
    _forms.raise_validation_errors(
        form,
        subject="query",
        hints=_query_form_hints,
        fallback_hint=(
            "Fix the attributes listed in 'violations'. list_queries shows the views that "
            "already exist, and list_work_packages proves a filter set before it is saved."
        ),
    )


def register(mcp: FastMCP) -> None:
    """Register the saved-query tools (SPEC §6.7)."""
    # The queries endpoint has its own filter set; teach the validator about the
    # names this module sends instead of editing the shared table (SPEC §9.1).
    # 'project' is list-typed here — unlike the work-package spelling it takes no
    # '*'/'!*', so asking for "global queries only" is not expressible upstream.
    register_filter_type("project", FilterType.LIST, resource=QUERIES_RESOURCE)

    @mcp.tool(
        name="list_queries",
        tags=_shared.tool_tags(_shared.GROUP_QUERIES, _shared.READ),
        annotations=_shared.read_annotations(title="List saved queries"),
    )
    @_shared.tool_errors
    async def list_queries(
        project_id: Annotated[
            int | str | None,
            Field(
                description="Numeric project id to list only that project's saved views. Comes "
                "from list_projects. Omit to list everything visible to the current user, "
                "global queries included. A project identifier (URL slug) is not accepted here "
                "by OpenProject — use the numeric id."
            ),
        ] = None,
        page: Annotated[int, Field(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(ge=1, le=100, description="Queries per page (max 100).")
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[QueryRow]:
        """List the saved work-package views (queries) this user can open.

        Use it to discover what a team already tracks — "Sprint board", "My open bugs",
        "Overdue in Platform" — before hand-building filters: running someone's saved view
        with `run_query` reproduces exactly what they see in the UI, including their
        grouping and sums.

        Returns the standard list envelope: rows of `{id, name, project, public, starred,
        updated_at}` plus `pagination`. `project` is null for a global query (saved outside
        any project); `public` false means the query is private to its owner, and only the
        owner's queries are visible to this account.

        Pitfalls. Query ids are instance-wide, not per project — never guess one, take it
        from here. This lists definitions only; it never runs them, so nothing here says how
        many work packages a query returns.

        Cross-references: run one with `run_query(query_id=…)`; build an ad-hoc query
        instead with `list_work_packages`; project ids come from `list_projects`.
        """
        context = _shared.get_tool_context()
        filters = (
            [make_filter("project", Op.EQ, [project_id], resource=QUERIES_RESOURCE)]
            if project_id is not None
            else []
        )
        params: dict[str, Any] = dict(pagination_params(page, page_size))
        serialized = serialize_filters(filters)
        if serialized is not None:
            params["filters"] = serialized

        payload = await context.client.get_json("queries", params=params)
        collection = hal.collection(payload)
        rows = [_query_row(element) for element in collection]
        notes = [GLOBAL_QUERY_NOTE] if any(row.project is None for row in rows) else None
        return _shared.envelope_from_collection(
            collection, rows, page=page, page_size=page_size, notes=notes
        )

    @mcp.tool(
        name="run_query",
        tags=_shared.tool_tags(_shared.GROUP_QUERIES, _shared.READ),
        annotations=_shared.read_annotations(
            title="Run a saved query", max_result_chars=MAX_RESULT_CHARS
        ),
    )
    @_shared.tool_errors
    async def run_query(
        query_id: Annotated[
            int,
            Field(
                description="Saved query id, from list_queries. It is the same id as in the "
                "UI's ?query_id= URL parameter."
            ),
        ],
        page: Annotated[
            int | None,
            Field(
                ge=1,
                description="1-based page number. Omit to use the page the stored query starts "
                "on (the first).",
            ),
        ] = None,
        page_size: Annotated[
            int | None,
            Field(
                ge=1,
                le=100,
                description="Rows per page (max 100). Omit to keep the query's stored page "
                "size, which can be larger or smaller than this tool's usual default.",
            ),
        ] = None,
        override_filters: Annotated[
            list[RawFilter] | None,
            Field(
                description="Run the query with these filters instead of its stored ones, for "
                "this call only — e.g. [{'name': 'status', 'operator': 'o', 'values': []}] or "
                "[{'name': 'customField12', 'operator': '=', 'values': ['4']}]. This REPLACES "
                "the stored filters (the API cannot merge), so re-state anything you want to "
                "keep; 'query.filters' in the result shows what the stored ones were."
            ),
        ] = None,
    ) -> QueryResults:
        """Run a saved view and get its work packages — the fastest way to answer with a
        team's own definition of "the sprint" or "our bugs".

        OpenProject queries run on read: this returns the rows as they are right now, in the
        stored order and grouping. The result is the standard list envelope — `items` of
        compact work-package rows, `pagination`, plus `groups` when the query groups and
        `sums` when it asks for totals — with one addition: `query` carries the stored
        definition (name, project, readable `filters`, `group_by`, `sort_by`), so the rows
        can be interpreted without a second call.

        Pitfalls. `groups` and `sums` are computed server-side across the entire result set,
        not the page in front of you — never re-add them from `items`. Omitting `page_size`
        keeps the query's own page size, which may be much larger than 20. `override_filters`
        replaces the stored filters instead of narrowing them, and never edits the saved
        query. A 422 means the filter set is invalid for this query's context (a
        project-scoped filter on a global query, an unknown custom field); `violations` names
        the attribute.

        Cross-references: find query ids with `list_queries`; equivalent ad-hoc filtering
        lives in `list_work_packages`; open a single row with `get_work_package`.
        """
        context = _shared.get_tool_context()
        params: dict[str, Any] = {}
        paged = pagination_params(page or 1, page_size or DEFAULT_PAGE_SIZE)
        if page is not None:
            params["offset"] = paged["offset"]
        if page_size is not None:
            params["pageSize"] = paged["pageSize"]

        notes: list[str] = []
        if override_filters is not None:
            # An explicitly empty list means "run with no filters at all", which the
            # serializer reports as None — send the empty array rather than omitting it.
            params["filters"] = (
                serialize_filters([filter_from_raw(entry) for entry in override_filters]) or "[]"
            )
            notes.append(OVERRIDE_NOTE)

        payload = await context.client.get_json(f"queries/{query_id}", params=params)
        results = hal.as_object(hal.embedded(payload, "results"))
        if results is None:
            notes.append(NO_RESULTS_NOTE)

        collection = hal.collection(results)
        rows = [WorkPackageRow.from_hal(element) for element in collection]
        envelope = _shared.envelope_from_collection(
            collection, rows, page=page, page_size=page_size, notes=notes
        )
        return QueryResults(
            query=_query_info(payload),
            items=rows,
            pagination=envelope.pagination,
            groups=envelope.groups,
            sums=envelope.sums,
            notes=envelope.notes,
        )

    @mcp.tool(
        name="save_query",
        tags=_shared.tool_tags(_shared.GROUP_QUERIES, _shared.WRITE),
        annotations=_shared.write_annotations(title="Save a query"),
    )
    @_shared.tool_errors
    async def save_query(
        name: Annotated[
            str,
            Field(
                description="Name the view is saved under, e.g. 'Overdue in Platform'. Names are "
                "not unique upstream, so a second save with the same name creates a second view."
            ),
        ],
        filters: Annotated[
            list[RawFilter],
            Field(
                description="The filters to store, in the same shape run_query's "
                "override_filters and list_work_packages' raw_filters take — e.g. "
                "[{'name': 'status', 'operator': 'o', 'values': []}, {'name': 'assignee', "
                "'operator': '=', 'values': ['12']}]. Values are ids (or 'me'), not display "
                "names. Pass [] deliberately for a view that filters nothing: unlike a listing, "
                "a stored query with no filters shows every status.",
            ),
        ],
        project_id: Annotated[
            int | str | None,
            Field(
                description="Numeric project id the view belongs to, from list_projects. Omit "
                "for a global (cross-project) view — but project-scoped filters such as "
                "version, category or subprojectId are then rejected."
            ),
        ] = None,
        public: Annotated[
            bool,
            Field(
                description="true shares the view with everyone who can see the project; false "
                "(default) keeps it private to the authenticated user. Sharing usually needs the "
                "'manage public queries' permission."
            ),
        ] = False,
        star: Annotated[
            bool,
            Field(
                description="Also star (favorite) the view for the authenticated user, so it "
                "appears in their sidebar. Done as a second call after the query exists; if it "
                "fails the query is still saved and 'notes' says so."
            ),
        ] = False,
        sort_by: Annotated[
            list[list[str]] | None,
            Field(
                description="Stored sort order, e.g. [['due_date', 'asc'], ['id', 'desc']]. Keys "
                "are the snake_case work-package columns list_work_packages sorts by; unknown "
                "keys are rejected locally with the allowed set listed. Omit for the default."
            ),
        ] = None,
        group_by: Annotated[
            str | None,
            Field(
                description="Column to group the results by, e.g. 'status', 'assignee', 'type' "
                "or 'version'. Grouping is what makes run_query return 'groups' with per-group "
                "counts. Omit for a flat list."
            ),
        ] = None,
    ) -> SavedQuery:
        """Save a filter set as a reusable OpenProject view the whole team can open.

        Use it when a filter combination is worth keeping — "Overdue in Platform", "My open
        bugs" — instead of rebuilding it every session: the saved view shows up in the
        OpenProject UI as well, and `run_query` reproduces it exactly. Prove the filters with
        `list_work_packages` first; whatever works there works here.

        The call runs `POST /queries/form` before committing, so an invalid filter name, an
        operator the filter does not support, or a project-scoped filter on a global view comes
        back as `violations` naming the attribute — nothing is saved. Returns the stored
        definition: `{id, name, project, public, starred, filters (as readable sentences),
        group_by, sort_by, display_sums, updated_at, notes}`. Keep the `id`: it is what
        `run_query` takes.

        Pitfalls. Filter values are ids, not names — 'Grace Hopper' is not a value, `12` is.
        `star=true` is a second request after the query exists; if it fails the query is still
        saved and `notes` says so, so never re-save on a starring failure. If OpenProject keeps
        fewer filters than were sent, `notes` says that too — read `filters` rather than
        assuming the view matches the request. Custom-field filters (`customField12`) are sent
        as plain values because a list-typed one cannot be told apart from a text one without
        asking the instance; if such a filter makes the call fail, nothing was saved — save that
        view in the UI. Editing and deleting saved views is deliberately not offered here:
        change or remove them in the OpenProject UI.

        Cross-references: `list_queries` lists what already exists (and gives ids);
        `run_query(query_id=...)` runs this view; `list_work_packages` is the ad-hoc equivalent
        and the place to validate filters first; `list_projects` supplies `project_id`.
        """
        context = _shared.get_tool_context()
        if not name or not name.strip():
            raise InputValidationError(
                "name is empty.",
                hint="Pass the name the view is saved under, e.g. 'Overdue in Platform'.",
            )

        wire_filters = [filter_from_raw(entry) for entry in filters]
        attributes: dict[str, Any] = {
            "name": name.strip(),
            "public": public,
            "filters": [_query_filter_payload(entry) for entry in wire_filters],
        }

        links: dict[str, Any] = {}
        if project_id is not None:
            links["project"] = link("projects", project_id)
        if group_by is not None and group_by.strip():
            links["groupBy"] = {"href": QUERY_GROUP_HREF.format(column=to_wire_name(group_by))}
        sort_links = _sort_by_links(sort_by)
        if sort_links:
            links["sortBy"] = sort_links

        payload = build_write_payload(attributes, links)
        form = await context.client.post_json("queries/form", json=payload)
        _raise_query_form_validation_errors(form)

        echoed = _forms.form_payload(form)
        body = _forms.merge_form_payload(echoed or {}, payload)
        echoed_filters = hal.as_objects(echoed.get("filters")) if echoed is not None else []
        if len(echoed_filters) == len(wire_filters):
            # The form re-rendered our filters the way this instance stores them;
            # committing its version keeps resource-valued filters (status,
            # assignee) in whichever spelling it expects. The length guard stops a
            # form that dropped filters from silently shrinking the view (G2).
            body["filters"] = [dict(entry) for entry in echoed_filters]

        created = await context.client.post_json("queries", json=body)

        notes: list[str] = []
        stored_filters = hal.as_objects(created.get("filters"))
        if len(stored_filters) != len(wire_filters):
            notes.append(
                FILTERS_CHANGED_NOTE.format(sent=len(wire_filters), stored=len(stored_filters))
            )

        query_id = hal.self_id(created)
        if star and query_id is not None:
            try:
                # Starring is a PATCH upstream: the star namespace mounts the
                # generic Update endpoint, so POST is not routed at all.
                starred = await context.client.patch_json(f"queries/{query_id}/star")
            except OpenProjectError as exc:
                # The query exists; failing the whole call would hide its id.
                notes.append(STAR_FAILED_NOTE.format(message=exc.message))
            else:
                if hal.self_id(starred) is not None:
                    created = starred
                else:
                    notes.append(STAR_NO_ECHO_NOTE)
        elif star:
            notes.append(
                "The query was saved but OpenProject returned no id for it, so it could not be "
                "starred. Find it with list_queries and star it in the UI."
            )

        return SavedQuery(**_query_info(created).model_dump(), notes=notes)
