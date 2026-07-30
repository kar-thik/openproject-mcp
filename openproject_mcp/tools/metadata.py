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
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import NotFoundError, OpenProjectError
from openproject_mcp.client.payloads import build_write_payload, links_payload
from openproject_mcp.config import PROBE_CACHE_TTL
from openproject_mcp.projections import Ref
from openproject_mcp.tools._shared import (
    GROUP_METADATA,
    READ,
    ToolContext,
    get_tool_context,
    read_annotations,
    tool_errors,
    tool_tags,
)
from openproject_mcp.version_probe import CACHE_KEY_ROOT, InstanceProbe

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "CACHE_KEY_CONFIGURATION",
    "CACHE_KEY_CURRENT_USER",
    "InstanceInfo",
    "ProjectMetadata",
    "WorkPackageSchema",
    "register",
]

CACHE_KEY_CONFIGURATION = "metadata:configuration"
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
    """Connection test result plus what this instance supports (SPEC §6.1, §4.7)."""

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
        default_factory=list,
        description="Page sizes the instance offers; the largest is the effective page_size cap.",
    )
    current_user: CurrentUser = Field(description="Who this server is authenticated as.")
    features: InstanceProbe = Field(
        description="Probed feature availability (SPEC §4.7); read it before using gated params."
    )


class NamedRow(BaseModel):
    """The ``{id, name}`` shape every enumeration row extends."""

    id: int | str | None = Field(default=None, description="Resource id — pass this, not the name.")
    name: str | None = Field(default=None, description="Display name as this instance spells it.")


class TypeRow(NamedRow):
    """A work-package type (Task, Bug, Milestone, …)."""

    is_default: bool | None = Field(default=None, description="Pre-selected for new work packages.")
    is_milestone: bool | None = Field(
        default=None, description="Milestone types take a single 'date', not start/due."
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
    """The ids and names that are valid here (SPEC §6.12)."""

    project_id: int | str | None = Field(
        default=None, description="The project this was scoped to; null for the global sets."
    )
    types: list[TypeRow] = Field(
        default_factory=list,
        description="Work-package types; scoped to the project when project_id was given.",
    )
    statuses: list[StatusRow] = Field(
        default_factory=list, description="All statuses, each with is_closed."
    )
    priorities: list[PriorityRow] = Field(default_factory=list, description="All priorities.")
    roles: list[RoleRow] = Field(default_factory=list, description="All membership roles.")
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
        default=None, description="Degradation markers (G5): modules off, permissions missing."
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
    """Custom-field definition, the read shape of SPEC §6.2.1 without a value."""

    key: str = Field(description="Wire key, e.g. 'customField12' — use it in raw_filters.")
    name: str | None = Field(default=None, description="Display name, e.g. 'Severity'.")
    type: str | None = Field(
        default=None, description="Schema type; a leading '[]' means multi-value."
    )
    required: bool = Field(default=False, description="Creation fails without it.")
    writable: bool = Field(default=False, description="False means computed/read-only.")
    options: list[Ref] | None = Field(
        default=None,
        description="Allowed {id, name} options for list/user/version custom fields; null for "
        "free-text and numeric ones.",
    )


class WorkPackageSchema(BaseModel):
    """Everything a create/update call needs to be valid (SPEC §6.12, §6.2.1)."""

    project_id: int | str = Field(description="Project the schema was requested for.")
    type_id: int | str = Field(description="Work-package type the schema was requested for.")
    required_fields: list[str] = Field(
        default_factory=list, description="Writable keys that must be supplied on create."
    )
    fields: list[SchemaField] = Field(
        default_factory=list, description="Core attributes with type/required/writable."
    )
    custom_fields: list[SchemaCustomField] = Field(
        default_factory=list, description="Always a list; empty when the type has none."
    )
    notes: list[str] | None = Field(
        default=None, description="Degradation markers (G5), e.g. capped allowed-value lists."
    )


# --- HAL helpers ----------------------------------------------------------


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _allowed_options(
    entry: Mapping[str, Any],
) -> list[tuple[int | str | None, str | None, bool | None]]:
    """Allowed values of a schema field as ``(id, name, is_default)`` triples.

    Handles both spellings OpenProject uses: ``_links.allowedValues`` (link
    objects) and ``_embedded.allowedValues`` (whole resources). A single link
    object means "fetch them from this URL" and yields nothing.
    """
    links = entry.get("_links")
    if isinstance(links, Mapping):
        values = links.get("allowedValues")
        if isinstance(values, Sequence) and not isinstance(values, str | bytes):
            return [
                (
                    hal.id_from_href(
                        item.get("href") if isinstance(item.get("href"), str) else None
                    ),
                    item.get("title") if isinstance(item.get("title"), str) else None,
                    None,
                )
                for item in values
                if isinstance(item, Mapping)
            ]
    inlined = hal.embedded(entry, "allowedValues")
    if isinstance(inlined, Sequence) and not isinstance(inlined, str | bytes):
        return [
            (
                hal.self_id(item),
                item.get("name") or item.get("title") or item.get("value"),
                _bool_or_none(item.get("default")),
            )
            for item in inlined
            if isinstance(item, Mapping)
        ]
    return []


def _type_row(payload: Mapping[str, Any]) -> TypeRow:
    return TypeRow(
        id=hal.self_id(payload),
        name=payload.get("name"),
        is_default=_bool_or_none(payload.get("isDefault")),
        is_milestone=_bool_or_none(payload.get("isMilestone")),
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
        schema = hal.embedded(form, "schema")
        entry = schema.get("activity") if isinstance(schema, Mapping) else None
        if not isinstance(entry, Mapping):
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

    for key, entry in schema.items():
        if key.startswith("_") or not isinstance(entry, Mapping) or "type" not in entry:
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
                    type=type_name if isinstance(type_name, str) else None,
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

        async def fetch_configuration() -> dict[str, Any]:
            return await ctx.client.get_json("configuration")

        configuration = await _cached_json(
            ctx, CACHE_KEY_CONFIGURATION, fetch_configuration, refresh=False
        )

        me = await ctx.client.get_json("users/me")
        ctx.cache.set(CACHE_KEY_CURRENT_USER, me, scope=ctx.scope, ttl=ctx.settings.cache_ttl)

        probe = await ctx.probe()

        raw_options = configuration.get("perPageOptions")
        per_page_options = (
            [int(option) for option in raw_options if isinstance(option, int)]
            if isinstance(raw_options, Sequence) and not isinstance(raw_options, str | bytes)
            else []
        )
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
