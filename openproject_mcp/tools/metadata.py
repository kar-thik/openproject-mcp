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

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    NotFoundError,
    OpenProjectError,
    UnexpectedResponseError,
    ValidationFailedError,
)
from openproject_mcp.client.filters import (
    Filter,
    FilterType,
    Op,
    make_filter,
    query_params,
    register_filter_type,
)
from openproject_mcp.client.payloads import build_write_payload, links_payload
from openproject_mcp.config import PROBE_CACHE_TTL
from openproject_mcp.projections import ListEnvelope, Ref, custom_field_type_name
from openproject_mcp.tools._shared import (
    GROUP_METADATA,
    READ,
    ToolContext,
    build_envelope,
    get_configuration,
    get_tool_context,
    read_annotations,
    report_progress,
    tool_errors,
    tool_tags,
)
from openproject_mcp.version_probe import (
    CACHE_KEY_CAPABILITIES_CONTEXT,
    CACHE_KEY_ROOT,
    CapabilitiesContextPrefix,
    InstanceProbe,
    cached_capabilities_context,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "CACHE_KEY_CURRENT_USER",
    "CapabilityGroup",
    "InstanceInfo",
    "PermissionsResult",
    "ProjectMetadata",
    "WorkPackageSchema",
    "register",
]

CACHE_KEY_CURRENT_USER = "metadata:users:me"

#: Allowed values longer than this are cut, with a note, to protect the context.
MAX_ALLOWED_VALUES = 50


# --- output models --------------------------------------------------------


class CurrentUser(BaseModel):
    """The authenticated principal behind ``OPENPROJECT_API_KEY``."""

    id: int | str | None = Field(default=None, description="Numeric user id.")
    name: str | None = Field(default=None, description="Display name.")
    login: str | None = Field(default=None, description="Login handle.")
    admin: bool | None = Field(
        default=None, description="True for instance administrators; null when not disclosed."
    )
    email: str | None = Field(default=None, description="Mail address, when the API exposes it.")


class InstanceInfo(BaseModel):
    """Connection test result plus what this instance supports."""

    core_version: str | None = Field(
        default=None, description="OpenProject core version, e.g. '17.7.1'."
    )
    instance_name: str | None = Field(default=None, description="Configured instance name.")
    api_url: str | None = Field(default=None, description="API base URL this server talks to.")
    maximum_attachment_file_size_bytes: int | None = Field(
        default=None,
        description="Upload ceiling; upload_attachment pre-flights against it.",
    )
    per_page_options: list[int] = Field(
        default_factory=list[int],
        description="Page sizes the instance offers; the largest is the effective page_size cap.",
    )
    current_user: CurrentUser = Field(description="Who this server is authenticated as.")
    features: InstanceProbe = Field(
        description="Probed feature availability; read it before using gated params."
    )


class NamedRow(BaseModel):
    """The ``{id, name}`` shape every enumeration row extends."""

    id: int | str | None = Field(default=None, description="Resource id — pass this, not the name.")
    name: str | None = Field(default=None, description="Display name as this instance spells it.")


class TypeRow(NamedRow):
    """A work-package type (Task, Bug, Milestone, …).

    OpenProject 17.7+ collapses type *variants* into their root type: the global
    listing returns fewer types than older versions showed, and variant-aware
    instances fill ``own_name``/``parent`` on the variants that do appear.
    """

    is_default: bool | None = Field(default=None, description="Pre-selected for new work packages.")
    is_milestone: bool | None = Field(
        default=None, description="Milestone types take a single 'date', not start/due."
    )
    own_name: str | None = Field(
        default=None,
        description="Variant's own name without the parent prefix (17.7+); null elsewhere.",
    )
    parent: Ref | None = Field(
        default=None,
        description="Parent type when this is a variant (17.7+); null elsewhere.",
    )


class StatusRow(NamedRow):
    """A work-package status, with the flag that defines 'done'."""

    is_closed: bool | None = Field(
        default=None,
        description="Authoritative done/not-done flag — never classify by status name.",
    )
    is_default: bool | None = Field(default=None, description="Status new work packages start in.")


class PriorityRow(NamedRow):
    """A work-package priority. Ids are instance-specific — never hardcode them."""

    is_default: bool | None = Field(default=None, description="Applied when none is given.")
    is_active: bool | None = Field(default=None, description="False for retired priorities.")


class RoleRow(NamedRow):
    """A membership role."""


class VersionRow(NamedRow):
    """A version / sprint defined in or shared with a project."""

    status: str | None = Field(default=None, description="open, locked or closed.")
    start_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD).")
    end_date: str | None = Field(default=None, description="ISO date (YYYY-MM-DD).")
    sharing: str | None = Field(
        default=None, description="Sharing scope: none, descendants, hierarchy, tree, system."
    )


class CategoryRow(NamedRow):
    """A work-package category, defined per project."""

    default_assignee: Ref | None = Field(
        default=None, description="User auto-assigned when this category is chosen."
    )


class ActivityRow(NamedRow):
    """A time-entry activity, discovered from the time-entry form (never hardcoded)."""

    is_default: bool | None = Field(default=None, description="Used when log_time omits activity.")


class ProjectMetadata(BaseModel):
    """The ids and names that are valid here."""

    project_id: int | str | None = Field(
        default=None, description="The project this was scoped to; null for the global sets."
    )
    types: list[TypeRow] = Field(
        default_factory=list[TypeRow],
        description="Work-package types; scoped to the project when project_id was given.",
    )
    statuses: list[StatusRow] = Field(
        default_factory=list[StatusRow], description="All statuses, each with is_closed."
    )
    priorities: list[PriorityRow] = Field(
        default_factory=list[PriorityRow], description="All priorities."
    )
    roles: list[RoleRow] = Field(default_factory=list[RoleRow], description="All membership roles.")
    versions: list[VersionRow] | None = Field(
        default=None, description="Project versions/sprints; null unless project_id was given."
    )
    categories: list[CategoryRow] | None = Field(
        default=None, description="Project categories; null unless project_id was given."
    )
    time_entry_activities: list[ActivityRow] | None = Field(
        default=None,
        description="Activities log_time accepts here; null unless project_id was given.",
    )
    notes: list[str] | None = Field(
        default=None, description="Degradation notes: modules off, permissions missing."
    )


class SchemaField(BaseModel):
    """One attribute of a work-package schema."""

    key: str = Field(description="Wire attribute name, e.g. 'subject', 'assignee', 'startDate'.")
    name: str | None = Field(default=None, description="Localized label shown in the UI.")
    type: str | None = Field(
        default=None, description="Schema type, e.g. 'String', 'Formattable', 'User', '[]User'."
    )
    required: bool = Field(default=False, description="Creation fails without it.")
    writable: bool = Field(default=False, description="False means read-only; do not send it.")
    has_default: bool | None = Field(
        default=None, description="True when OpenProject fills it in for you."
    )
    allowed_values: list[Ref] | None = Field(
        default=None,
        description="Valid {id, name} choices when the schema inlines them (status, category, "
        "version); null when the API only offers a lookup URL.",
    )


class SchemaCustomField(BaseModel):
    """Custom-field definition: the canonical custom-field read shape without a value."""

    key: str = Field(description="Wire key, e.g. 'customField12' — use it in raw_filters.")
    name: str | None = Field(default=None, description="Display name, e.g. 'Severity'.")
    type: str | None = Field(
        default=None,
        description="Normalized custom-field type: 'list', 'string', 'text', 'user', 'date', … — "
        "the same vocabulary get_work_package reports in custom_fields[].type.",
    )
    required: bool = Field(default=False, description="Creation fails without it.")
    writable: bool = Field(default=False, description="False means computed/read-only.")
    options: list[Ref] | None = Field(
        default=None,
        description="Allowed {id, name} options for list/user/version custom fields; null for "
        "free-text and numeric ones.",
    )


class WorkPackageSchema(BaseModel):
    """Everything a create/update call needs to be valid."""

    project_id: int | str = Field(description="Project the schema was requested for.")
    type_id: int | str = Field(description="Work-package type the schema was requested for.")
    required_fields: list[str] = Field(
        default_factory=list[str], description="Writable keys that must be supplied on create."
    )
    fields: list[SchemaField] = Field(
        default_factory=list[SchemaField],
        description="Core attributes with type/required/writable.",
    )
    custom_fields: list[SchemaCustomField] = Field(
        default_factory=list[SchemaCustomField],
        description="Always a list; empty when the type has none.",
    )
    notes: list[str] | None = Field(
        default=None, description="Degradation notes, e.g. capped allowed-value lists."
    )


# --- HAL helpers ----------------------------------------------------------


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _allowed_options(
    entry: Mapping[str, Any],
) -> list[tuple[int | str | None, str | None, bool | None]]:
    """Allowed values of a schema field as ``(id, name, is_default)`` triples.

    Handles both spellings OpenProject uses: ``_links.allowedValues`` (link
    objects) and ``_embedded.allowedValues`` (whole resources). A single link
    object means "fetch them from this URL" and yields nothing.
    """
    links = hal.as_object(entry.get("_links"))
    if links is not None and hal.as_array(links.get("allowedValues")) is not None:
        return [
            (
                hal.id_from_href(item.get("href") if isinstance(item.get("href"), str) else None),
                item.get("title") if isinstance(item.get("title"), str) else None,
                None,
            )
            for item in hal.as_objects(links.get("allowedValues"))
        ]
    inlined = hal.embedded(entry, "allowedValues")
    if hal.as_array(inlined) is not None:
        return [
            (
                hal.self_id(item),
                _text_or_none(item.get("name") or item.get("title") or item.get("value")),
                _bool_or_none(item.get("default")),
            )
            for item in hal.as_objects(inlined)
        ]
    return []


def _type_row(payload: Mapping[str, Any]) -> TypeRow:
    own_name = payload.get("ownName")
    return TypeRow(
        id=hal.self_id(payload),
        name=payload.get("name"),
        is_default=_bool_or_none(payload.get("isDefault")),
        is_milestone=_bool_or_none(payload.get("isMilestone")),
        own_name=own_name if isinstance(own_name, str) else None,
        parent=Ref.from_hal(payload, "parent"),
    )


def _status_row(payload: Mapping[str, Any]) -> StatusRow:
    return StatusRow(
        id=hal.self_id(payload),
        name=payload.get("name"),
        is_closed=_bool_or_none(payload.get("isClosed")),
        is_default=_bool_or_none(payload.get("isDefault")),
    )


def _priority_row(payload: Mapping[str, Any]) -> PriorityRow:
    return PriorityRow(
        id=hal.self_id(payload),
        name=payload.get("name"),
        is_default=_bool_or_none(payload.get("isDefault")),
        is_active=_bool_or_none(payload.get("isActive")),
    )


def _role_row(payload: Mapping[str, Any]) -> RoleRow:
    return RoleRow(id=hal.self_id(payload), name=payload.get("name"))


def _version_row(payload: Mapping[str, Any]) -> VersionRow:
    return VersionRow(
        id=hal.self_id(payload),
        name=payload.get("name"),
        status=payload.get("status"),
        start_date=payload.get("startDate"),
        end_date=payload.get("endDate"),
        sharing=payload.get("sharing"),
    )


def _category_row(payload: Mapping[str, Any]) -> CategoryRow:
    return CategoryRow(
        id=hal.self_id(payload),
        name=payload.get("name"),
        default_assignee=Ref.from_hal(payload, "defaultAssignee"),
    )


async def _cached_rows[T](
    ctx: ToolContext,
    key: str,
    path: str,
    builder: Callable[[Mapping[str, Any]], T],
    *,
    refresh: bool,
) -> list[T]:
    """GET a HAL collection, project it, and memoize the rows per credential."""

    async def fetch() -> list[T]:
        payload = await ctx.client.get_json(path)
        return [builder(element) for element in hal.collection(payload)]

    return await ctx.cache.get_or_set(
        key, fetch, scope=ctx.scope, ttl=ctx.settings.cache_ttl, refresh=refresh
    )


async def _cached_json(
    ctx: ToolContext, key: str, factory: Callable[[], Awaitable[dict[str, Any]]], *, refresh: bool
) -> dict[str, Any]:
    return await ctx.cache.get_or_set(
        key, factory, scope=ctx.scope, ttl=ctx.settings.cache_ttl, refresh=refresh
    )


async def _time_entry_activities(
    ctx: ToolContext, project_id: int | str, *, refresh: bool
) -> tuple[list[ActivityRow], str | None]:
    """Discover the activities this project accepts from the time-entry form.

    SPEC §6.9/G3: the allowed activities are instance data, so they are read from
    ``POST /time_entries/form`` rather than hardcoded. A missing time-tracking
    module or a permission gap degrades to a note (G5), never to a failure.
    """

    async def fetch() -> list[ActivityRow]:
        body = build_write_payload(links=links_payload(project=project_id))
        form = await ctx.client.post_json("time_entries/form", json=body)
        schema = hal.as_object(hal.embedded(form, "schema"))
        entry = hal.as_object(schema.get("activity")) if schema is not None else None
        if entry is None:
            return []
        return [
            ActivityRow(id=option_id, name=name, is_default=is_default)
            for option_id, name, is_default in _allowed_options(entry)
        ]

    try:
        rows = await ctx.cache.get_or_set(
            f"metadata:project:{project_id}:time_entry_activities",
            fetch,
            scope=ctx.scope,
            ttl=ctx.settings.cache_ttl,
            refresh=refresh,
        )
    except OpenProjectError as exc:
        return [], (
            f"time-entry activities unavailable on this instance ({exc.error_type}): {exc.message}"
        )
    return rows, None


def _schema_rows(
    schema: Mapping[str, Any],
) -> tuple[list[SchemaField], list[SchemaCustomField], list[str]]:
    fields: list[SchemaField] = []
    custom_fields: list[SchemaCustomField] = []
    notes: list[str] = []

    for key, raw_entry in schema.items():
        entry = hal.as_object(raw_entry)
        if key.startswith("_") or entry is None or "type" not in entry:
            continue
        options = _allowed_options(entry)
        if len(options) > MAX_ALLOWED_VALUES:
            notes.append(
                f"{key}: {len(options)} allowed values, only the first "
                f"{MAX_ALLOWED_VALUES} are listed"
            )
            options = options[:MAX_ALLOWED_VALUES]
        refs = [Ref(id=option_id, name=name) for option_id, name, _ in options] or None
        name = entry.get("name")
        type_name = entry.get("type")
        if key.startswith("customField"):
            custom_fields.append(
                SchemaCustomField(
                    key=key,
                    name=name if isinstance(name, str) else None,
                    # One vocabulary for both tools: '[]User' reads back as 'user'
                    # here exactly as get_work_package reports it.
                    type=custom_field_type_name(type_name),
                    required=bool(entry.get("required")),
                    writable=bool(entry.get("writable")),
                    options=refs,
                )
            )
            continue
        fields.append(
            SchemaField(
                key=key,
                name=name if isinstance(name, str) else None,
                type=type_name if isinstance(type_name, str) else None,
                required=bool(entry.get("required")),
                writable=bool(entry.get("writable")),
                has_default=_bool_or_none(entry.get("hasDefault")),
                allowed_values=refs,
            )
        )
    return fields, custom_fields, notes


# --- capabilities (SPEC §6.1, §4.7) ---------------------------------------

#: Resource key for the filter registry — ``/capabilities`` has its own names.
CAPABILITIES_RESOURCE = "capabilities"

#: Page size and hard cap for the capability sweep; the cap is reported (G1).
CAPABILITIES_PAGE_SIZE = 100
MAX_CAPABILITIES = 500

#: The caveat the capabilities API documents about itself, surfaced in-band.
CAPABILITIES_CAVEAT = (
    "OpenProject's capabilities API exposes only a subset of its permissions, so the absence "
    "of an action here is not proof that the user lacks the permission."
)


class CapabilityGroup(BaseModel):
    """The actions the current user may perform in one context."""

    id: str = Field(description="Stable row id: 'global' or 'project:<id>'. Also the grouping key.")
    context: Literal["global", "project"] = Field(
        description="'global' for instance-wide actions, 'project' for actions inside one project."
    )
    project: Ref | None = Field(
        default=None, description="The project this row is about; null for the global context."
    )
    actions: list[str] = Field(
        default_factory=list[str],
        description="Action names as OpenProject spells them, e.g. 'memberships/create', "
        "'work_packages/read'. Sorted; always a list.",
    )


class PermissionCheck(BaseModel):
    """The answer to the optional ``permission`` predicate."""

    checked: str = Field(description="The permission string that was checked, as normalized.")
    allowed: bool = Field(
        description="True when the action appears in the capabilities listed here. False means "
        "'not listed' — see the tool's caveat, it is not proof of a denial."
    )
    granted_in: list[str] = Field(
        default_factory=list[str],
        description="Ids of the CapabilityGroup rows that carry the action; empty when not found.",
    )


class PermissionsResult(ListEnvelope[CapabilityGroup]):
    """The standard list envelope of capability rows, plus who was asked about and the check."""

    principal: Ref = Field(
        description="The user the capabilities were resolved for — always the authenticated "
        "account, by numeric id."
    )
    capability_count: int = Field(
        default=0, description="Individual capabilities read from the API before grouping."
    )
    check: PermissionCheck | None = Field(
        default=None, description="Present only when the 'permission' parameter was given."
    )


def _register_capability_filters() -> None:
    """Teach the filter validator the capability filter names (never edit the shared table)."""
    register_filter_type("context", FilterType.LIST, CAPABILITIES_RESOURCE)
    register_filter_type("principal", FilterType.LIST, CAPABILITIES_RESOURCE)


async def _current_user(ctx: ToolContext) -> dict[str, Any]:
    """The cached ``users/me`` document; fetched and cached on a miss."""
    cached = hal.as_object(ctx.cache.get(CACHE_KEY_CURRENT_USER, scope=ctx.scope))
    if cached is not None:
        return dict(cached)
    me = await ctx.client.get_json("users/me")
    ctx.cache.set(CACHE_KEY_CURRENT_USER, me, scope=ctx.scope, ttl=ctx.settings.cache_ttl)
    return me


def _action_name(element: Mapping[str, Any]) -> str | None:
    """``/api/v3/actions/memberships/create`` → ``memberships/create``.

    The action name has a slash in it, so the generic href parser (which takes the
    last segment) would return ``create`` and lose the resource.
    """
    action = hal.ref(element, "action")
    if action is not None and isinstance(action.href, str) and "/actions/" in action.href:
        name = action.href.split("/actions/", 1)[1].strip("/")
        if name:
            return name
    if action is not None and action.name:
        return action.name
    return None


def _context_of(element: Mapping[str, Any]) -> tuple[str, Literal["global", "project"], Ref | None]:
    """Classify a capability's context link into ``(row id, kind, project ref)``."""
    context = hal.ref(element, "context")
    href = context.href if context is not None else None
    if isinstance(href, str) and "/projects/" in href:
        project_id = hal.id_from_href(href)
        name = context.name if context is not None else None
        return f"project:{project_id}", "project", Ref(id=project_id, name=name)
    return "global", "global", None


def _group_capabilities(elements: Sequence[Mapping[str, Any]]) -> list[CapabilityGroup]:
    """Collapse capability resources into one row per context."""
    rows: dict[str, CapabilityGroup] = {}
    actions: dict[str, set[str]] = {}
    for element in elements:
        row_id, kind, project = _context_of(element)
        row = rows.get(row_id)
        if row is None:
            row = CapabilityGroup(id=row_id, context=kind, project=project)
            rows[row_id] = row
            actions[row_id] = set()
        elif row.project is None and project is not None:
            row.project = project
        action = _action_name(element)
        if action:
            actions[row_id].add(action)
    for row_id, row in rows.items():
        row.actions = sorted(actions[row_id])
    return [rows[key] for key in sorted(rows)]


def _capability_params(principal_id: int | str, context: str, page: int) -> dict[str, Any]:
    filters: list[Filter] = [
        make_filter("principal", Op.EQ, [principal_id], resource=CAPABILITIES_RESOURCE),
        make_filter("context", Op.EQ, [context], resource=CAPABILITIES_RESOURCE),
    ]
    return query_params(filters=filters, page=page, page_size=CAPABILITIES_PAGE_SIZE)


def register(mcp: FastMCP) -> None:
    """Register the metadata tools. Phase 1 fills in the three read tools."""

    @mcp.tool(
        name="get_instance_info",
        tags=tool_tags(GROUP_METADATA, READ),
        annotations=read_annotations(title="Get instance info"),
    )
    @tool_errors
    async def get_instance_info() -> InstanceInfo:
        """Check the OpenProject connection and report what this instance supports.

        Call this first when anything fails in an unexplained way, when the user asks "am I
        connected / who am I", or before using a version-gated parameter. It is the
        server's connection test: it authenticates on every call rather than answering from
        cache.

        Returns the core version and instance name, the attachment size ceiling
        (``maximum_attachment_file_size_bytes``), the page sizes the instance offers, the
        authenticated user ``{id, name, login, admin}``, and ``features`` — the probe result
        telling you whether internal comments, emoji reactions and project favorites exist
        here, and which time-entry filter spelling this version uses.

        Pitfalls: ``features`` describes the *server version*, not this user's permissions —
        a supported feature can still 403. A failure here is the actionable one: 401 means
        the API key is wrong or revoked, a network error means the URL, DNS, proxy or TLS
        trust is wrong; both come back with a hint naming the environment variable to fix.

        For per-project ids (types, statuses, priorities, versions, categories, activities)
        use ``get_project_metadata``; for what the current user may do use ``list_permissions``.
        """
        ctx = get_tool_context()

        root = await ctx.client.get_json("")
        ctx.cache.set(CACHE_KEY_ROOT, root, scope=ctx.scope, ttl=PROBE_CACHE_TTL)

        configuration = await get_configuration(ctx)

        me = await ctx.client.get_json("users/me")
        ctx.cache.set(CACHE_KEY_CURRENT_USER, me, scope=ctx.scope, ttl=ctx.settings.cache_ttl)

        probe = await ctx.probe()

        raw_options = hal.as_array(configuration.get("perPageOptions")) or ()
        per_page_options = [option for option in raw_options if isinstance(option, int)]
        max_size = configuration.get("maximumAttachmentFileSize")

        return InstanceInfo(
            core_version=probe.core_version or root.get("coreVersion"),
            instance_name=probe.instance_name or root.get("instanceName"),
            api_url=ctx.settings.url,
            maximum_attachment_file_size_bytes=max_size if isinstance(max_size, int) else None,
            per_page_options=per_page_options,
            current_user=CurrentUser(
                id=hal.self_id(me),
                name=me.get("name"),
                login=me.get("login"),
                admin=_bool_or_none(me.get("admin")),
                email=me.get("email"),
            ),
            features=probe,
        )

    @mcp.tool(
        name="get_project_metadata",
        tags=tool_tags(GROUP_METADATA, READ),
        annotations=read_annotations(title="Get project metadata"),
    )
    @tool_errors
    async def get_project_metadata(
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric project id or URL identifier. Omit for the instance-global sets "
                    "(types, statuses, priorities, roles) — cross-project filtering never "
                    "needs an arbitrary project. Supply it to additionally get this project's "
                    "types, versions, categories and time-entry activities."
                )
            ),
        ] = None,
        refresh: Annotated[
            bool,
            Field(
                description=(
                    "Bypass the metadata cache. Use it right after an administrator added a "
                    "type, status, version or category; otherwise leave it false."
                )
            ),
        ] = False,
    ) -> ProjectMetadata:
        """List the ids and names that are actually valid on this instance.

        This is the one-call answer to "what do I pass for type / status / priority /
        version / category / activity". Call it before any create or update, before
        filtering by ids, and whenever a write fails with an allowed-values error. Nothing
        here is hardcoded — priority ids and activity ids differ per instance.

        Without ``project_id`` returns the global ``types``, ``statuses``, ``priorities`` and
        ``roles``. With ``project_id`` the ``types`` list narrows to the ones enabled in that
        project and ``versions``, ``categories`` and ``time_entry_activities`` are filled in.
        Every row is ``{id, name}`` plus its flags: ``statuses[].is_closed`` is the
        authoritative done marker (never classify by status name — it is localized),
        ``types[].is_milestone`` tells you the type takes a single date, and
        ``priorities[].is_default`` / ``time_entry_activities[].is_default`` say what you get
        by omitting the field.

        Pitfalls: results are cached (default 300 s) — pass ``refresh=true`` after an admin
        change. Time-entry activities are read from the time-entry form, so if the time
        tracking module is off or you lack permission the list comes back empty with a note
        in ``notes`` rather than an error (check ``notes``).

        For the writable fields and custom fields of one project+type combination use
        ``get_work_package_schema``; for project ids themselves use ``list_projects``.
        """
        ctx = get_tool_context()
        notes: list[str] = []

        statuses = await _cached_rows(
            ctx, "metadata:statuses", "statuses", _status_row, refresh=refresh
        )
        priorities = await _cached_rows(
            ctx, "metadata:priorities", "priorities", _priority_row, refresh=refresh
        )
        roles = await _cached_rows(ctx, "metadata:roles", "roles", _role_row, refresh=refresh)

        if project_id is None:
            types = await _cached_rows(ctx, "metadata:types", "types", _type_row, refresh=refresh)
            return ProjectMetadata(
                types=types,
                statuses=statuses,
                priorities=priorities,
                roles=roles,
                notes=notes or None,
            )

        types = await _cached_rows(
            ctx,
            f"metadata:project:{project_id}:types",
            f"projects/{project_id}/types",
            _type_row,
            refresh=refresh,
        )
        versions = await _cached_rows(
            ctx,
            f"metadata:project:{project_id}:versions",
            f"projects/{project_id}/versions",
            _version_row,
            refresh=refresh,
        )
        categories = await _cached_rows(
            ctx,
            f"metadata:project:{project_id}:categories",
            f"projects/{project_id}/categories",
            _category_row,
            refresh=refresh,
        )
        activities, activity_note = await _time_entry_activities(ctx, project_id, refresh=refresh)
        if activity_note:
            notes.append(activity_note)

        return ProjectMetadata(
            project_id=project_id,
            types=types,
            statuses=statuses,
            priorities=priorities,
            roles=roles,
            versions=versions,
            categories=categories,
            time_entry_activities=activities,
            notes=notes or None,
        )

    @mcp.tool(
        name="get_work_package_schema",
        tags=tool_tags(GROUP_METADATA, READ),
        annotations=read_annotations(title="Get work package schema"),
    )
    @tool_errors
    async def get_work_package_schema(
        project_id: Annotated[
            int | str,
            Field(
                description=(
                    "Numeric project id — this endpoint does not accept the string "
                    "identifier. The schema is per project AND type: the same type carries "
                    "different custom fields in another project."
                )
            ),
        ],
        type_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric work-package type id from "
                    "get_project_metadata(project_id=...).types — not the type name."
                )
            ),
        ],
        refresh: Annotated[
            bool,
            Field(
                description=(
                    "Bypass the cache after an administrator changed the type or its custom fields."
                )
            ),
        ] = False,
    ) -> WorkPackageSchema:
        """Show which fields a work package of this type accepts in this project.

        Call it before ``create_work_package``/``update_work_package`` when you need the
        required fields, when you want a custom field's key or its allowed options, or after
        a 422 that named a field you do not recognise.

        Returns ``required_fields`` (writable keys you must supply), ``fields`` — every core
        attribute with ``{key, name, type, required, writable, has_default, allowed_values}``
        — and ``custom_fields`` with ``{key, name, type, required, writable, options}``.
        ``allowed_values``/``options`` are ``{id, name}`` lists for status, category, version
        and list/user custom fields.

        Pitfalls: ``key`` is the wire spelling (``startDate``, ``customField12``) — that is
        what ``raw_filters`` and ``custom_fields`` writes use, though writes also accept the
        display name. A field with ``writable: false`` is computed by OpenProject; sending it
        is an error, not a no-op. ``allowed_values`` is null when the API only offers a lookup
        URL (assignee, project) — resolve those with ``search_principals`` or ``list_projects``
        instead. Long option lists are capped at 50 with a marker in ``notes``.

        Ids for both parameters come from ``get_project_metadata``; to read the values
        actually set on one work package use ``get_work_package``.
        """
        ctx = get_tool_context()
        path = f"work_packages/schemas/{project_id}-{type_id}"

        async def fetch() -> dict[str, Any]:
            return await ctx.client.get_json(path)

        try:
            schema = await _cached_json(
                ctx, f"schema:{project_id}-{type_id}", fetch, refresh=refresh
            )
        except NotFoundError as exc:
            raise NotFoundError(
                exc.message,
                http_status=exc.http_status,
                error_identifier=exc.error_identifier,
                hint=(
                    f"No schema for project {project_id!r} with type {type_id!r}. This endpoint "
                    "needs the NUMERIC project id, not the identifier, and the type must be "
                    "enabled in that project — check get_project_metadata(project_id=...) for "
                    "the types available there."
                ),
            ) from exc

        fields, custom_fields, notes = _schema_rows(schema)
        return WorkPackageSchema(
            project_id=project_id,
            type_id=type_id,
            required_fields=[field.key for field in fields if field.required and field.writable],
            fields=fields,
            custom_fields=custom_fields,
            notes=notes or None,
        )

    _register_capability_filters()

    @mcp.tool(
        name="list_permissions",
        tags=tool_tags(GROUP_METADATA, READ),
        annotations=read_annotations(title="List permissions"),
    )
    @tool_errors
    async def list_permissions(
        project_id: Annotated[
            int | str | None,
            Field(
                description=(
                    "Numeric project id or URL identifier to scope the question to that "
                    "project ('may I create work packages HERE'). Omit it for the "
                    "instance-wide (global) actions such as creating projects or "
                    "administering users. An identifier is resolved to its numeric id first, "
                    "because the capabilities API only accepts numeric project ids."
                )
            ),
        ] = None,
        permission: Annotated[
            str | None,
            Field(
                description=(
                    "Optional single action to check, spelled the way OpenProject does: "
                    "'<resource>/<action>', e.g. 'work_packages/create', "
                    "'memberships/create', 'projects/update'. Adds a "
                    "{checked, allowed, granted_in} predicate to the result; the full "
                    "grouped listing is returned either way, so you can see the exact "
                    "spellings this instance uses."
                )
            ),
        ] = None,
    ) -> PermissionsResult:
        """List what the authenticated user is allowed to do, globally or in one project.

        Use it before attempting a write that might 403, to explain to a user why an action
        failed, or to pick between tools ("can I add a member here, or should I ask an
        admin?"). It reads the real capabilities API for the current user — it does not
        return a user profile and it never guesses from the admin flag.

        Returns the standard list envelope whose ``items`` are one row per context —
        ``{id, context, project, actions}`` with ``actions`` such as
        ``work_packages/create`` — plus ``principal`` (the user asked about),
        ``capability_count``, and ``check`` when ``permission`` was given.

        CAVEAT, straight from the API: OpenProject exposes only a SUBSET of its permissions
        as capabilities. An action missing from this list is not proof that the user lacks
        the permission — it may simply not be modelled. Treat a hit as reliable and a miss as
        "unknown, try it and read the 403".

        Pitfalls: the capabilities API has no ``"me"`` value, so the numeric id of the
        authenticated user is resolved first (from the cached ``users/me``) — you cannot ask
        about another user with this tool. Results are capped at 500 capabilities with a note
        in ``notes`` when the cap is hit; scope with ``project_id`` to stay well under it.
        Capabilities are about permission only: a permitted action can still fail validation.

        Cross-references: ``get_instance_info`` reports who this server is authenticated as
        and what the instance version supports; ``list_memberships`` and ``list_roles`` show
        where the permissions come from; ``get_project_metadata`` lists the ids a permitted
        action needs.
        """
        ctx = get_tool_context()
        notes: list[str] = []

        if permission is not None and not permission.strip():
            raise InputValidationError(
                "permission is empty.",
                hint=(
                    "Pass an action such as 'work_packages/create', or omit permission to get "
                    "the full listing."
                ),
            )

        me = await _current_user(ctx)
        principal_id = hal.self_id(me)
        if not isinstance(principal_id, int):
            raise UnexpectedResponseError(
                "Could not resolve the numeric id of the authenticated user.",
                hint=(
                    "The capabilities API filters by numeric principal id and has no 'me' "
                    "value. Check the connection with get_instance_info."
                ),
            )
        principal = Ref(id=principal_id, name=me.get("name"))

        context_value = "g"
        prefix: CapabilitiesContextPrefix | None = None
        if project_id is not None:
            numeric = project_id
            if not str(project_id).isdigit():
                project = await ctx.client.get_json(f"projects/{project_id}")
                numeric = hal.self_id(project) or project_id
            prefix = cached_capabilities_context(ctx.cache, ctx.scope) or "p"
            context_value = f"{prefix}{numeric}"

        elements: list[Mapping[str, Any]] = []
        total = 0
        page = 1
        while True:
            params = _capability_params(principal_id, context_value, page)
            try:
                payload = await ctx.client.get_json("capabilities", params=params)
            except ValidationFailedError:
                # The project context prefix moved from 'p{id}' to 'w{id}' in 17.2 (SPEC §4.7).
                # Retry once with the other spelling and remember what worked.
                if page > 1 or prefix is None or prefix == "w":
                    raise
                prefix = "w"
                context_value = f"w{context_value[1:]}"
                payload = await ctx.client.get_json(
                    "capabilities", params=_capability_params(principal_id, context_value, page)
                )
            if prefix is not None:
                ctx.cache.set(
                    CACHE_KEY_CAPABILITIES_CONTEXT, prefix, scope=ctx.scope, ttl=PROBE_CACHE_TTL
                )

            collection = hal.collection(payload)
            total = collection.total
            elements.extend(collection.elements)
            reachable = min(total, MAX_CAPABILITIES)
            if not collection.elements or len(elements) >= reachable:
                break
            page += 1
            await report_progress(len(elements), reachable, "reading capabilities")

        if total > MAX_CAPABILITIES:
            notes.append(
                f"capped at {MAX_CAPABILITIES} of {total} capabilities; the grouped actions and "
                "any permission check may be incomplete — pass project_id to narrow the scope"
            )
        notes.append(CAPABILITIES_CAVEAT)

        rows = _group_capabilities(elements)
        check: PermissionCheck | None = None
        if permission is not None:
            wanted = permission.strip().strip("/").lower()
            granted = [row.id for row in rows if wanted in {a.lower() for a in row.actions}]
            check = PermissionCheck(checked=wanted, allowed=bool(granted), granted_in=granted)

        envelope = build_envelope(
            rows, total=len(rows), page=1, page_size=max(len(rows), 1), notes=notes
        )
        return PermissionsResult(
            items=rows,
            pagination=envelope.pagination,
            notes=envelope.notes,
            principal=principal,
            capability_count=len(elements),
            check=check,
        )
