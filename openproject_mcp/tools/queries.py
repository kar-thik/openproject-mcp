"""Saved-query tools (SPEC §6.7 — Phase 2).

Lands here:

=======================  ======  ==================================================
Tool                     Phase   Endpoint(s)
=======================  ======  ==================================================
🔍 ``list_queries``      2       ``GET /queries?filters=[project]``
🔍 ``run_query``         2       ``GET /queries/{id}`` (results embedded)
✏️ ``save_query``        3       form → ``POST /queries`` (+ ``/star``)
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

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.filters import (
    DEFAULT_PAGE_SIZE,
    FilterType,
    Op,
    RawFilter,
    filter_from_raw,
    make_filter,
    pagination_params,
    register_filter_type,
    serialize_filters,
)
from openproject_mcp.projections import ListEnvelope, Ref, WorkPackageRow
from openproject_mcp.tools import _shared

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["QueryInfo", "QueryResults", "QueryRow", "register"]

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
    """Compact saved-query row (SPEC §6.7)."""

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
    """A saved query's results: the §9.3 envelope plus what actually ran."""

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
