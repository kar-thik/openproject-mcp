"""Compact output models (SPEC §5.2, §6.2, §9.3).

These are the shapes tools return. Two rules govern everything here:

* **Refs are ``{id, name}``.** hrefs are an implementation detail of
  ``client/hal.py`` and never appear in output.
* **Every projection carries its own ``id``**, so a sibling tool can consume it
  (activity ids, relation ids, attachment ids…).

The universal list envelope (:class:`ListEnvelope`) is generic: parameterize it
per tool (``ListEnvelope[WorkPackageRow]``) and FastMCP derives the
``outputSchema`` from it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from openproject_mcp.client import hal
from openproject_mcp.client.filters import to_snake_name

__all__ = [
    "CUSTOM_FIELD_TYPE_NAMES",
    "CustomFieldValue",
    "ErrorDetail",
    "ErrorEnvelope",
    "Group",
    "ListEnvelope",
    "Pagination",
    "Ref",
    "RelationRow",
    "TruncatedList",
    "WorkPackageDetail",
    "WorkPackageRow",
    "custom_field_type_name",
]

#: Schema type → the one vocabulary custom-field types are surfaced in. Both
#: ``get_work_package`` and ``get_work_package_schema`` speak it, so a type read
#: from one can be matched against the other.
CUSTOM_FIELD_TYPE_NAMES: dict[str, str] = {
    "CustomOption": "list",
    "User": "user",
    "Group": "group",
    "Principal": "principal",
    "Version": "version",
    "Project": "project",
    "Category": "category",
    "String": "string",
    "Text": "text",
    "Formattable": "text",
    "Int": "integer",
    "Integer": "integer",
    "Float": "float",
    "Boolean": "boolean",
    "Date": "date",
    "DateTime": "date_time",
    "Duration": "duration",
    "Link": "link",
}


def custom_field_type_name(raw: Any) -> str | None:
    """Normalize an API schema type to the custom-field vocabulary.

    ``"String"`` → ``"string"``, ``"[]User"`` → ``"user"`` (the ``[]`` marks a
    multi-value field, which the values themselves already show), and anything
    this instance invented falls back to snake_case rather than being dropped.
    """
    if not isinstance(raw, str) or not raw:
        return None
    base = raw[2:] if raw.startswith("[]") else raw
    if not base:
        return None
    return CUSTOM_FIELD_TYPE_NAMES.get(base, to_snake_name(base))


class Ref(BaseModel):
    """A reference to another resource. Canonical output shape."""

    model_config = ConfigDict(extra="forbid")

    id: int | str | None = Field(default=None, description="Resource id.")
    name: str | None = Field(default=None, description="Human-readable name.")

    @classmethod
    def from_hal(cls, payload: Mapping[str, Any] | None, key: str) -> Ref | None:
        """Build a Ref from ``payload["_links"][key]``; ``None`` when unset."""
        resolved = hal.ref(payload, key)
        if resolved is None:
            return None
        return cls(id=resolved.id, name=resolved.name)

    @classmethod
    def list_from_hal(cls, payload: Mapping[str, Any] | None, key: str) -> list[Ref]:
        return [Ref(id=item.id, name=item.name) for item in hal.refs(payload, key)]


class Pagination(BaseModel):
    """Pagination facts for every list result (guarantee G1)."""

    total: int = Field(description="Total matching records on the server.")
    page: int = Field(description="1-based page number of this result.")
    page_size: int = Field(description="Records requested per page.")
    has_more: bool = Field(description="True when further pages exist.")


class Group(BaseModel):
    """A server-side group bucket, computed over the full filtered set."""

    value: str | None = Field(default=None, description="Group value, e.g. a status name.")
    count: int = Field(default=0, description="Records in this group across all pages.")
    sums: dict[str, float] | None = Field(
        default=None, description="Per-group sums when show_sums was requested."
    )


class ListEnvelope[ItemT](BaseModel):
    """The one list shape every list tool returns (SPEC §9.3).

    Small fetched-in-full collections use the same envelope with
    ``has_more: false`` — one shape everywhere.
    """

    items: list[ItemT] = Field(
        default_factory=cast("Callable[[], list[ItemT]]", list),
        description="The page of results.",
    )
    pagination: Pagination = Field(description="Total/page/page_size/has_more.")
    groups: list[Group] | None = Field(
        default=None, description="Present only when group_by was requested."
    )
    sums: dict[str, float] | None = Field(
        default=None, description="Present only when show_sums was requested."
    )
    notes: list[str] | None = Field(
        default=None,
        description="Degradation markers: capped aggregations, unavailable modules, …",
    )


class TruncatedList[ItemT](BaseModel):
    """A capped include (SPEC §6.2): the first N items plus an honest marker."""

    items: list[ItemT] = Field(
        default_factory=cast("Callable[[], list[ItemT]]", list),
        description="Items included (capped).",
    )
    truncated: bool = Field(default=False, description="True when items were dropped.")
    total: int = Field(default=0, description="Total available items.")
    more_via: str | None = Field(default=None, description="Tool call that returns the full list.")


class CustomFieldValue(BaseModel):
    """Canonical custom-field read shape (SPEC §6.2.1)."""

    key: str = Field(description="Wire key, e.g. 'customField12'.")
    name: str | None = Field(default=None, description="Display name, e.g. 'Severity'.")
    type: str | None = Field(default=None, description="Schema type, e.g. 'list', 'string'.")
    value: Any = Field(default=None, description="Scalar value or resolved name(s).")
    value_ids: list[int | str] | None = Field(
        default=None, description="Ids for list/user/version custom fields."
    )


class WorkPackageRow(BaseModel):
    """Compact work-package row for list results (SPEC §5.2)."""

    id: int | str | None = Field(default=None, description="Work package id.")
    subject: str | None = Field(default=None, description="Subject line.")
    type: Ref | None = Field(default=None, description="Work package type.")
    status: Ref | None = Field(default=None, description="Status.")
    priority: Ref | None = Field(default=None, description="Priority.")
    assignee: Ref | None = Field(default=None, description="Assigned user or group.")
    project: Ref | None = Field(default=None, description="Owning project.")
    start_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD).")
    due_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD).")
    percentage_done: int | None = Field(default=None, description="Progress, 0-100.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")

    @classmethod
    def from_hal(cls, element: Mapping[str, Any]) -> WorkPackageRow:
        """Project one HAL work package onto the compact row.

        The single construction site: list, search, query results, children and
        the detail projections all go through it, so a row means the same thing
        whichever tool produced it.
        """
        return WorkPackageRow(
            id=hal.self_id(element),
            subject=element.get("subject"),
            type=Ref.from_hal(element, "type"),
            status=Ref.from_hal(element, "status"),
            priority=Ref.from_hal(element, "priority"),
            assignee=Ref.from_hal(element, "assignee"),
            project=Ref.from_hal(element, "project"),
            start_date=element.get("startDate"),
            due_date=element.get("dueDate"),
            percentage_done=element.get("percentageDone"),
            updated_at=element.get("updatedAt"),
        )


class RelationRow(BaseModel):
    """One work-package relation (SPEC §6.3).

    The two endpoints that produce relations — the ``relations`` include of
    ``get_work_package`` and the relation write tools — return this same shape.
    ``from_work_package``/``to_work_package`` are spelled out because the wire's
    ``from``/``to`` read as prepositions rather than as the two ends of an edge.
    """

    id: int | str | None = Field(
        default=None,
        description="Relation id. Pass it to update_work_package_relation or "
        "delete_work_package_relation — it is not a work package id.",
    )
    type: str | None = Field(
        default=None,
        description="Relation type as OpenProject stored it, read from the 'from' work package "
        "(e.g. 'follows' means 'from' is scheduled after 'to').",
    )
    reverse_type: str | None = Field(
        default=None,
        description="The same relation read from the 'to' work package: 'follows' <-> 'precedes', "
        "'blocks' <-> 'blocked', 'relates' <-> 'relates'.",
    )
    from_work_package: Ref | None = Field(
        default=None, description="Work package the relation starts at."
    )
    to_work_package: Ref | None = Field(
        default=None, description="Work package the relation points to."
    )
    lag: int | None = Field(
        default=None,
        description="Working days kept between the predecessor and the successor. Only "
        "follows/precedes relations carry one; null everywhere else.",
    )
    description: str | None = Field(
        default=None, description="Free-text note stored on the relation; null when unset."
    )

    @classmethod
    def from_hal(cls, element: Mapping[str, Any]) -> RelationRow:
        """Project one HAL relation resource onto the row."""
        lag = element.get("lag")
        return RelationRow(
            id=hal.self_id(element),
            type=element.get("type") if isinstance(element.get("type"), str) else None,
            reverse_type=(
                element.get("reverseType") if isinstance(element.get("reverseType"), str) else None
            ),
            from_work_package=Ref.from_hal(element, "from"),
            to_work_package=Ref.from_hal(element, "to"),
            lag=None if isinstance(lag, bool) or not isinstance(lag, int) else lag,
            description=hal.formattable(element.get("description")),
        )


class WorkPackageDetail(WorkPackageRow):
    """Full work-package detail (SPEC §6.2): the row plus text and relations."""

    description: str | None = Field(
        default=None, description="Description as markdown (raw); html is dropped."
    )
    author: Ref | None = Field(default=None, description="Creating user.")
    responsible: Ref | None = Field(default=None, description="Accountable user.")
    version: Ref | None = Field(default=None, description="Version / sprint.")
    category: Ref | None = Field(default=None, description="Category.")
    parent: Ref | None = Field(default=None, description="Parent work package.")
    estimated_hours: float | None = Field(default=None, description="Estimate in hours.")
    spent_hours: float | None = Field(default=None, description="Logged time in hours.")
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    lock_version: int | None = Field(
        default=None, description="Optimistic-locking version; pass to update_work_package."
    )
    custom_fields: list[CustomFieldValue] = Field(
        default_factory=list[CustomFieldValue],
        description="Always a list; empty when none are set.",
    )
    available: dict[str, bool] | None = Field(
        default=None,
        description="Feature availability for this WP: dev links, meetings, files.",
    )
    notes: list[str] | None = Field(
        default=None, description="Degradation markers (G5) for this result."
    )


class ErrorDetail(BaseModel):
    """The body of the structured tool error (SPEC §4.2)."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(description="Taxonomy member, e.g. 'validation_failed'.")
    http_status: int | None = Field(default=None, description="Upstream HTTP status, if any.")
    error_identifier: str | None = Field(
        default=None, description="OpenProject error identifier urn, when provided."
    )
    message: str = Field(description="What went wrong, in plain language.")
    violations: list[dict[str, str]] | None = Field(
        default=None, description="Field-level problems: [{attribute, message}]."
    )
    hint: str | None = Field(default=None, description="How to fix it (always English).")


class ErrorEnvelope(BaseModel):
    """``{"error": {...}}`` — the text content of every failed tool call."""

    error: ErrorDetail
