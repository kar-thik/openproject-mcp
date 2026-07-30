"""People and access tools (SPEC §6.11).

Lands here:

==============================  ======  =========================================
Tool                            Phase   Endpoint(s)
==============================  ======  =========================================
read ``search_principals``      2       ``GET /principals``
read ``get_user``               2       ``GET /users/{id}``
read ``list_memberships``       2       ``GET /memberships``
admin write ``create_membership``   2   form -> ``POST /memberships``
admin write ``update_membership``   2   form -> ``PATCH /memberships/{id}``
admin destructive ``delete_membership``  2  ``DELETE /memberships/{id}``
read ``list_roles``             2       ``GET /roles``
==============================  ======  =========================================

Non-negotiables for this module:

* Users and groups are **read-only** (SPEC §6.11, §18). ``search_principals`` is
  the id-producing tool for every principal parameter in the server — assignees,
  watchers, time-entry users and membership principals — so its description says
  so and no user/group CRUD exists anywhere.
* The ``admin`` tag covers exactly the three membership writes. Everything else
  here is a plain read that any authenticated user may call.
* A membership grant must not surprise-email a production instance: the create
  payload sends ``_meta.sendNotifications = false`` unless the caller supplied a
  ``notify_message``.
* ``/principals`` spells its search filter ``any_name_attribute`` — snake_case on
  the wire, unlike almost every other filter — which is why
  ``client.filters.WIRE_NAME_OVERRIDES`` maps that name to itself.
* Memberships carry no ``lockVersion``: their PATCH is a plain PATCH, not
  ``patch_with_lock``, and a 409 from this endpoint means the API refused the
  change, not a stale read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import InputValidationError, UnexpectedResponseError
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
from openproject_mcp.client.payloads import link, links_payload
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools import _forms
from openproject_mcp.tools._shared import (
    ADMIN,
    DESTRUCTIVE,
    GROUP_PEOPLE,
    READ,
    WRITE,
    ToolContext,
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
    "MembershipDeletion",
    "MembershipRow",
    "PrincipalRef",
    "PrincipalRow",
    "RoleRow",
    "UserDetail",
    "register",
]

#: Resource keys for the filter registry — these endpoints have their own strategies.
PRINCIPALS_RESOURCE = "principals"
MEMBERSHIPS_RESOURCE = "memberships"

#: The three kinds of principal this server distinguishes.
PrincipalKind = Literal["user", "group", "placeholder"]

#: Tool-facing principal kind -> the API's ``type`` filter value.
PRINCIPAL_TYPE_VALUES: dict[PrincipalKind, str] = {
    "user": "User",
    "group": "Group",
    "placeholder": "PlaceholderUser",
}
#: API ``_type`` -> tool-facing principal kind.
PRINCIPAL_KINDS: dict[str, PrincipalKind] = {
    wire: kind for kind, wire in PRINCIPAL_TYPE_VALUES.items()
}
#: API collection segment -> tool-facing principal kind, for href-only payloads.
PRINCIPAL_PATH_KINDS: dict[str, PrincipalKind] = {
    "users": "user",
    "groups": "group",
    "placeholder_users": "placeholder",
}

#: Account statuses OpenProject exposes on the ``/principals`` status filter.
PRINCIPAL_STATUSES: tuple[str, ...] = ("active", "invited", "registered", "locked")

PRINCIPAL_VISIBILITY_NOTE = (
    "email, login and status come back only for user principals the authenticated account is "
    "allowed to see; groups and placeholder users never carry them."
)
ROLE_TOKEN_NOTE = (
    "permission arrays are long — call list_roles without include_permissions when all you need "
    "is a role id for create_membership or update_membership."
)
ROLE_PERMISSIONS_MISSING_NOTE = (
    "include_permissions was requested but this instance's /roles endpoint does not expose "
    "permission arrays; use list_permissions to check what the current user may actually do."
)
NO_NOTIFICATION_NOTE = (
    "No invitation email was sent: pass notify_message to notify the principal on the next call."
)


# --- projections ----------------------------------------------------------


class PrincipalRow(BaseModel):
    """One user, group or placeholder user (SPEC §6.11)."""

    id: int | str | None = Field(
        default=None,
        description="Principal id. This is the value every principal parameter wants: "
        "assignee_id, watcher ids, time-entry user_id, create_membership.principal_id.",
    )
    name: str | None = Field(default=None, description="Display name as OpenProject renders it.")
    type: PrincipalKind | None = Field(
        default=None,
        description="'user' for a person, 'group' for a user group, 'placeholder' for a "
        "placeholder user (a licence-free stand-in that can be assigned work but cannot log in).",
    )
    email: str | None = Field(
        default=None,
        description="Email address; users only, and only when the caller may see it.",
    )
    login: str | None = Field(
        default=None, description="Login name; users only, and only when the caller may see it."
    )
    status: str | None = Field(
        default=None,
        description=f"Account status, one of: {', '.join(PRINCIPAL_STATUSES)}. Users only. "
        "'locked' accounts still appear in old assignments but cannot be assigned new work.",
    )


class PrincipalRef(BaseModel):
    """A principal reference that keeps its kind — used inside memberships."""

    id: int | str | None = Field(default=None, description="Principal id.")
    name: str | None = Field(default=None, description="Display name.")
    type: PrincipalKind | None = Field(
        default=None,
        description="'user', 'group' or 'placeholder'; null when the instance did not say.",
    )


class UserDetail(BaseModel):
    """Full user detail (SPEC §6.11). The avatar URL is deliberately dropped."""

    id: int | str | None = Field(default=None, description="Numeric user id.")
    name: str | None = Field(default=None, description="Display name, e.g. 'Grace Hopper'.")
    login: str | None = Field(default=None, description="Login name used to sign in.")
    email: str | None = Field(
        default=None,
        description="Email address; null when the authenticated account may not see it.",
    )
    admin: bool | None = Field(
        default=None,
        description="True for instance administrators. Null when the field is not visible to "
        "the caller — treat null as 'unknown', never as 'not an admin'.",
    )
    status: str | None = Field(
        default=None, description=f"Account status, one of: {', '.join(PRINCIPAL_STATUSES)}."
    )
    language: str | None = Field(
        default=None, description="Interface language code, e.g. 'en'. Absent on some instances."
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")


class MembershipRow(BaseModel):
    """One project membership: a principal holding roles in a project."""

    id: int | str | None = Field(
        default=None,
        description="Membership id. Pass it to update_membership or delete_membership; it is "
        "not the principal id and not the project id.",
    )
    project: Ref | None = Field(
        default=None, description="Project the membership grants access to."
    )
    principal: PrincipalRef | None = Field(
        default=None, description="Who holds the membership: user, group or placeholder user."
    )
    roles: list[Ref] = Field(
        default_factory=list[Ref],
        description="Roles held in this project; always a list. Role ids come from list_roles.",
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC timestamp.")
    notes: list[str] | None = Field(
        default=None, description="Markers about what this call did, e.g. notifications suppressed."
    )


class RoleRow(BaseModel):
    """One role definition (SPEC §6.11)."""

    id: int | str | None = Field(
        default=None, description="Role id; this is what create_membership.role_ids wants."
    )
    name: str | None = Field(
        default=None, description="Role name as configured on the instance, e.g. 'Member'."
    )
    permissions: list[str] | None = Field(
        default=None,
        description="Permission identifiers granted by the role; present only when "
        "include_permissions=true and the instance exposes them.",
    )


class MembershipDeletion(BaseModel):
    """Outcome of ``delete_membership``."""

    id: int = Field(description="Id of the membership that was revoked.")
    deleted: bool = Field(description="True once OpenProject accepted the deletion.")
    message: str = Field(description="Plain-language confirmation for the user.")


# --- projection helpers ---------------------------------------------------


def _kind_from_type(value: Any) -> PrincipalKind | None:
    return PRINCIPAL_KINDS.get(value) if isinstance(value, str) else None


def _kind_from_href(href: str | None) -> PrincipalKind | None:
    """Derive the principal kind from ``/api/v3/users/12`` and friends."""
    if not isinstance(href, str):
        return None
    for segment in reversed(urlsplit(href).path.strip("/").split("/")):
        kind = PRINCIPAL_PATH_KINDS.get(segment)
        if kind is not None:
            return kind
    return None


def _principal_row(element: Mapping[str, Any]) -> PrincipalRow:
    kind = _kind_from_type(element.get("_type")) or _kind_from_href(hal.self_href(element))
    email = element.get("email")
    login = element.get("login")
    status = element.get("status")
    return PrincipalRow(
        id=hal.self_id(element),
        name=element.get("name"),
        type=kind,
        email=email if isinstance(email, str) else None,
        login=login if isinstance(login, str) else None,
        status=status if isinstance(status, str) else None,
    )


def _principal_ref(element: Mapping[str, Any]) -> PrincipalRef | None:
    """Project ``_links.principal`` plus whatever the embedded copy reveals."""
    linked = hal.ref(element, "principal")
    inlined = hal.as_object(hal.embedded(element, "principal"))
    kind = _kind_from_type(inlined.get("_type")) if inlined is not None else None
    if kind is None:
        kind = _kind_from_href(linked.href if linked is not None else None)
    if linked is None and inlined is None:
        return None
    name = linked.name if linked is not None and linked.name else None
    if name is None and inlined is not None:
        inlined_name = inlined.get("name")
        name = inlined_name if isinstance(inlined_name, str) else None
    return PrincipalRef(
        id=linked.id if linked is not None else hal.self_id(inlined),
        name=name,
        type=kind,
    )


def _membership_row(element: Mapping[str, Any], *, notes: list[str] | None = None) -> MembershipRow:
    return MembershipRow(
        id=hal.self_id(element),
        project=Ref.from_hal(element, "project"),
        principal=_principal_ref(element),
        roles=Ref.list_from_hal(element, "roles"),
        created_at=element.get("createdAt"),
        updated_at=element.get("updatedAt"),
        notes=notes or None,
    )


def _role_row(element: Mapping[str, Any], *, include_permissions: bool) -> RoleRow:
    permissions: list[str] | None = None
    if include_permissions:
        raw = hal.as_array(element.get("permissions"))
        if raw is not None:
            permissions = [item for item in raw if isinstance(item, str)]
    return RoleRow(
        id=hal.self_id(element),
        name=element.get("name"),
        permissions=permissions,
    )


# --- input helpers --------------------------------------------------------


def _require_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise InputValidationError(
            f"{name}={value} is out of range.",
            hint=f"{name} must be between {low} and {high}.",
        )


def _positive_id(name: str, value: int) -> int:
    if value <= 0:
        raise InputValidationError(
            f"{name}={value} is not a valid id.",
            hint=f"{name} must be a positive integer. Ids come from the matching list_ tool.",
        )
    return value


def _role_ids(role_ids: Sequence[int]) -> list[int]:
    """Validate the role list locally: empty or bogus ids never reach the API."""
    if not role_ids:
        raise InputValidationError(
            "role_ids is empty.",
            hint=(
                "A membership needs at least one role. Call list_roles to see the roles this "
                "instance defines and pass their ids."
            ),
        )
    resolved: list[int] = []
    for value in role_ids:
        if isinstance(value, bool) or value <= 0:
            raise InputValidationError(
                f"role_ids contains {value!r}, which is not a role id.",
                hint="Pass positive integers only; list_roles returns the valid ids.",
            )
        if value not in resolved:
            resolved.append(value)
    return resolved


async def _project_numeric_id(ctx: ToolContext, value: int | str) -> int:
    """Resolve a project id or URL identifier to the numeric id the API links need.

    Project *hrefs* in membership payloads and the ``member`` principal filter are
    matched by numeric id, so an identifier like ``"demo"`` is looked up once here
    rather than being sent and silently mismatched.
    """
    if isinstance(value, bool):
        raise InputValidationError(
            "project_id must be a numeric id or the URL identifier string.",
            hint="Find both with list_projects.",
        )
    if isinstance(value, int):
        return _positive_id("project_id", value)
    text = value.strip()
    if not text:
        raise InputValidationError(
            "project_id is empty.",
            hint="Pass the numeric project id or its URL identifier; list_projects has both.",
        )
    if text.isdigit():
        return _positive_id("project_id", int(text))

    payload = await ctx.client.get_json(f"projects/{quote(text, safe='')}")
    raw = payload.get("id")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    resolved = hal.self_id(payload)
    if isinstance(resolved, int):
        return resolved
    raise UnexpectedResponseError(
        f"Project {text!r} was found but reported no numeric id.",
        hint="Pass the numeric project id from list_projects instead of the identifier.",
    )


# --- form flow (SPEC §4.5) ------------------------------------------------


def _raise_form_validation_errors(form: Mapping[str, Any], *, fallback_hint: str) -> None:
    """Turn a membership form's ``validationErrors`` into a typed 422.

    The form knows both what is wrong and which values would be accepted, so an
    unknown role comes back with the roles this instance actually defines.
    """
    _forms.raise_validation_errors(
        form,
        subject="membership",
        hints=_forms.allowed_value_hints,
        fallback_hint=fallback_hint,
    )


def _membership_meta(notify_message: str | None) -> dict[str, Any]:
    """``_meta`` for a membership write; quiet unless a message was supplied."""
    if notify_message is None or not notify_message.strip():
        return {"sendNotifications": False}
    return {
        "sendNotifications": True,
        "notificationMessage": {"raw": notify_message},
    }


def _register_filters() -> None:
    """Teach the filter validator the people-specific filter names we send."""
    register_filter_type("any_name_attribute", FilterType.TEXT, PRINCIPALS_RESOURCE)
    register_filter_type("member", FilterType.LIST, PRINCIPALS_RESOURCE)
    register_filter_type("project", FilterType.LIST, MEMBERSHIPS_RESOURCE)
    register_filter_type("principal", FilterType.LIST, MEMBERSHIPS_RESOURCE)


def _name_filter(query: str) -> Filter:
    """The ``/principals`` search filter.

    The wire name stays snake_case — camelCasing it to ``anyNameAttribute`` would
    400 — which is why ``WIRE_NAME_OVERRIDES`` maps it to itself.
    """
    return make_filter("any_name_attribute", Op.CONTAINS, [query], resource=PRINCIPALS_RESOURCE)


def register(mcp: FastMCP) -> None:
    """Register the people & access tools (SPEC §6.11)."""
    _register_filters()

    @mcp.tool(
        name="search_principals",
        tags=tool_tags(GROUP_PEOPLE, READ),
        annotations=read_annotations(title="Search users, groups and placeholder users"),
    )
    @tool_errors
    async def search_principals(
        query: Annotated[
            str | None,
            Field(
                description="Free text matched against name, login and email (substring, "
                "case-insensitive). Omit it to page through every visible principal."
            ),
        ] = None,
        type: Annotated[
            PrincipalKind | None,
            Field(
                description="Restrict to one kind of principal. 'user' = people who log in, "
                "'group' = user groups (assignable and grantable as a unit), 'placeholder' = "
                "licence-free stand-ins. Omit to search all three."
            ),
        ] = None,
        member_of_project: Annotated[
            int | str | None,
            Field(
                description="Only principals who are members of this project. Numeric project id "
                "or the URL identifier; an identifier costs one extra lookup. Use it before "
                "assigning work — a user who is not a member usually cannot be assigned."
            ),
        ] = None,
        status: Annotated[
            Literal["active", "invited", "registered", "locked"] | None,
            Field(
                description="Restrict to an account status. 'active' is the usual filter when "
                "looking for someone to assign work to; 'locked' accounts keep old assignments "
                "but take no new ones. Groups and placeholder users have no status and are "
                "excluded when this is set."
            ),
        ] = None,
        page: Annotated[int, Field(description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(description=f"Principals per page, 1-{MAX_PAGE_SIZE}.")
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[PrincipalRow]:
        """Find users, groups and placeholder users, and get their ids.

        This is **the** id-producing tool for every principal parameter in this
        server: `assignee`/`responsible` on work packages, watcher ids, the
        `user_id` of a time entry, and `principal_id` for `create_membership`.
        Names are never accepted where an id is wanted — resolve here first,
        and never guess a numeric id.

        Use it to answer "who is Grace Hopper's account", "which groups exist",
        "who is a member of the demo project". Returns the standard list
        envelope: `items` of `{id, name, type, email?, login?, status?}` plus
        `pagination{total,page,page_size,has_more}` and `notes`.

        Pitfalls. `email`, `login` and `status` are only returned for user
        principals the authenticated account may see — a null email means "not
        visible to you", not "no email". Group principals can hold memberships
        and be assigned work, so filter by `type` when you specifically need a
        person. Matching is substring-based, so a short `query` matches broadly;
        prefer the full name or the login. `member_of_project` filters by
        membership, not by whether the person ever touched the project.

        Cross-references: `get_user` for one user's full detail;
        `list_memberships` for who holds which roles in a project;
        `create_membership` to grant access; `list_roles` for the role ids that
        grant needs.
        """
        ctx = get_tool_context()
        _require_range("page", page, 1, 1_000_000)
        _require_range("page_size", page_size, 1, MAX_PAGE_SIZE)

        filters: list[Filter] = []
        if query is not None and query.strip():
            filters.append(_name_filter(query.strip()))
        if type is not None:
            filters.append(
                make_filter(
                    "type", Op.EQ, [PRINCIPAL_TYPE_VALUES[type]], resource=PRINCIPALS_RESOURCE
                )
            )
        if member_of_project is not None:
            project_id = await _project_numeric_id(ctx, member_of_project)
            filters.append(make_filter("member", Op.EQ, [project_id], resource=PRINCIPALS_RESOURCE))
        if status is not None:
            filters.append(make_filter("status", Op.EQ, [status], resource=PRINCIPALS_RESOURCE))

        payload = await ctx.client.get_json(
            "principals",
            params=query_params(filters=filters or None, page=page, page_size=page_size),
        )
        principals = hal.collection(payload)
        rows = [_principal_row(element) for element in principals]
        return envelope_from_collection(
            principals,
            rows,
            page=page,
            page_size=page_size,
            notes=[PRINCIPAL_VISIBILITY_NOTE],
        )

    @mcp.tool(
        name="get_user",
        tags=tool_tags(GROUP_PEOPLE, READ),
        annotations=read_annotations(title="Get user detail"),
    )
    @tool_errors
    async def get_user(
        id_or_me: Annotated[
            int | str,
            Field(
                description="Numeric user id from search_principals, or the literal string 'me' "
                "for the account this server authenticates as. Group and placeholder ids are "
                "not accepted here — this endpoint serves users only."
            ),
        ],
    ) -> UserDetail:
        """Read one user's profile: name, login, email, admin flag and status.

        Use it after `search_principals` when you need more than a name — to
        confirm an account is `active` before assigning work, to check whether
        somebody is an instance administrator, or to learn which account the
        server itself is acting as (`id_or_me='me'`).

        Returns `{id, name, login, email, admin, status, language, created_at,
        updated_at}`. The avatar URL is deliberately dropped: it costs tokens
        and cannot be rendered here.

        Pitfalls. `email`, `login` and `admin` are visibility-dependent — a null
        means the authenticated account may not see that field, never that the
        value is empty. `admin=true` says nothing about project permissions; use
        `list_permissions` for what the current user may actually do. A group or
        placeholder-user id returns 404 from this endpoint; those principals only
        appear in `search_principals`.

        Cross-references: `search_principals` to find the id;
        `list_memberships(principal_id=...)` for the projects and roles this
        person holds; `get_instance_info` also reports the current user.
        """
        ctx = get_tool_context()
        if isinstance(id_or_me, bool):
            raise InputValidationError(
                "id_or_me must be a numeric user id or the string 'me'.",
                hint="Find user ids with search_principals(type='user').",
            )
        if isinstance(id_or_me, str):
            key = id_or_me.strip()
            if key.lower() == "me":
                key = "me"
            elif key.isdigit():
                key = str(_positive_id("id_or_me", int(key)))
            else:
                raise InputValidationError(
                    f"{id_or_me!r} is not a user id.",
                    hint=(
                        "Pass a numeric id (search_principals(type='user') returns them) or the "
                        "literal string 'me'. Names and logins are not accepted here."
                    ),
                )
        else:
            key = str(_positive_id("id_or_me", id_or_me))

        payload = await ctx.client.get_json(f"users/{key}")
        admin = payload.get("admin")
        return UserDetail(
            id=hal.self_id(payload),
            name=payload.get("name"),
            login=payload.get("login"),
            email=payload.get("email"),
            admin=admin if isinstance(admin, bool) else None,
            status=payload.get("status"),
            language=payload.get("language"),
            created_at=payload.get("createdAt"),
            updated_at=payload.get("updatedAt"),
        )

    @mcp.tool(
        name="list_memberships",
        tags=tool_tags(GROUP_PEOPLE, READ),
        annotations=read_annotations(title="List project memberships"),
    )
    @tool_errors
    async def list_memberships(
        project_id: Annotated[
            int | str | None,
            Field(
                description="Only memberships in this project. Numeric id or the URL identifier "
                "(an identifier costs one extra lookup). Combine with principal_id to check one "
                "person's roles in one project."
            ),
        ] = None,
        principal_id: Annotated[
            int | None,
            Field(
                description="Only memberships held by this principal — a user, group or "
                "placeholder-user id from search_principals. Use it to answer 'which projects "
                "can this person see'."
            ),
        ] = None,
        page: Annotated[int, Field(description="1-based page number.")] = 1,
        page_size: Annotated[
            int, Field(description=f"Memberships per page, 1-{MAX_PAGE_SIZE}.")
        ] = DEFAULT_PAGE_SIZE,
    ) -> ListEnvelope[MembershipRow]:
        """List who has access to which project, and with which roles.

        Use it before granting or revoking access ("does she already have a
        role here?"), to audit a project's member list, or to see which projects
        a principal can reach. Called with no arguments it pages through every
        membership the authenticated account may see, which on a large instance
        is a lot — filter.

        Returns the standard list envelope: `items` of `{id, project, principal
        {id,name,type}, roles[], created_at, updated_at}` plus
        `pagination{total,page,page_size,has_more}`.

        Pitfalls. The `id` in each row is the **membership** id — the handle for
        `update_membership` and `delete_membership` — not the principal id and
        not the project id; mixing them up revokes the wrong access. A person
        can also reach a project through a **group** membership, so an empty
        result for `principal_id` does not prove they have no access. Memberships
        say who *may* act, not what they may do: roles carry the permissions.

        Cross-references: `list_roles` for role ids and their permissions;
        `create_membership` / `update_membership` / `delete_membership` to change
        access (admin-gated); `search_principals` for principal ids;
        `list_permissions` for what the current user may do.
        """
        ctx = get_tool_context()
        _require_range("page", page, 1, 1_000_000)
        _require_range("page_size", page_size, 1, MAX_PAGE_SIZE)

        filters: list[Filter] = []
        if project_id is not None:
            resolved = await _project_numeric_id(ctx, project_id)
            filters.append(make_filter("project", Op.EQ, [resolved], resource=MEMBERSHIPS_RESOURCE))
        if principal_id is not None:
            filters.append(
                make_filter(
                    "principal",
                    Op.EQ,
                    [_positive_id("principal_id", principal_id)],
                    resource=MEMBERSHIPS_RESOURCE,
                )
            )

        payload = await ctx.client.get_json(
            "memberships",
            params=query_params(filters=filters or None, page=page, page_size=page_size),
        )
        memberships = hal.collection(payload)
        rows = [_membership_row(element) for element in memberships]
        return envelope_from_collection(memberships, rows, page=page, page_size=page_size)

    @mcp.tool(
        name="create_membership",
        tags=tool_tags(GROUP_PEOPLE, WRITE, ADMIN),
        annotations=write_annotations(
            title="Grant project membership", requires_user_interaction=True
        ),
    )
    @tool_errors
    async def create_membership(
        project_id: Annotated[
            int | str,
            Field(
                description="Project to grant access to: numeric id or URL identifier from "
                "list_projects. An identifier costs one extra lookup."
            ),
        ],
        principal_id: Annotated[
            int,
            Field(
                description="Who gets access: a user, group or placeholder-user id from "
                "search_principals. Granting to a group gives every current and future member "
                "of that group the same roles — check the group before using one."
            ),
        ],
        role_ids: Annotated[
            list[int],
            Field(
                description="Roles to grant, by id from list_roles. At least one is required; "
                "the roles decide what the principal may do. Prefer the narrowest role that "
                "does the job."
            ),
        ],
        notify_message: Annotated[
            str | None,
            Field(
                description="Markdown message to send with an invitation email. Leave it unset "
                "(the default) and OpenProject sends NO email at all — a membership grant on a "
                "live instance must not surprise people. Set it only when the user asked for the "
                "principal to be notified."
            ),
        ] = None,
    ) -> MembershipRow:
        """Grant a principal one or more roles in a project (admin-gated).

        Use it to add somebody to a project, or to add a group so its members
        inherit access. The payload is validated through OpenProject's own
        membership form first, so a role id that does not exist or a principal
        that is already a member comes back as a typed validation error listing
        what would be accepted, instead of an opaque 422.

        Returns the created membership: `{id, project, principal, roles,
        created_at, notes}`. Keep the `id` — it is what `update_membership` and
        `delete_membership` take.

        Pitfalls. **No notification is sent unless `notify_message` is given.**
        This is deliberate: an unrequested invitation email is not undoable.
        Creating a membership for a principal who already has one fails —
        change the existing one with `update_membership` instead, after checking
        with `list_memberships(project_id=..., principal_id=...)`. Roles are
        replaced wholesale by `update_membership`, so grant every role the
        principal needs in one call. This tool is hidden unless the deployment
        sets OPENPROJECT_MCP_ADMIN_TOOLS=1.

        Cross-references: `search_principals` for `principal_id`, `list_roles`
        for `role_ids`, `list_projects` for `project_id`, `list_memberships` to
        check the current state first.
        """
        ctx = get_tool_context()
        roles = _role_ids(role_ids)
        resolved_project = await _project_numeric_id(ctx, project_id)
        _positive_id("principal_id", principal_id)

        links: dict[str, Any] = links_payload(project=resolved_project, principal=principal_id)
        links["roles"] = [link("roles", role_id) for role_id in roles]
        body: dict[str, Any] = {"_links": links, "_meta": _membership_meta(notify_message)}

        form = await ctx.client.post_json("memberships/form", json=body)
        _raise_form_validation_errors(
            form,
            fallback_hint=(
                "Fix the attributes listed in 'violations'. Role ids come from list_roles, "
                "principal ids from search_principals, and a principal can hold only one "
                "membership per project — use update_membership when one already exists."
            ),
        )

        created = await ctx.client.post_json("memberships", json=body)
        notes = None if notify_message else [NO_NOTIFICATION_NOTE]
        return _membership_row(created, notes=notes)

    @mcp.tool(
        name="update_membership",
        tags=tool_tags(GROUP_PEOPLE, WRITE, ADMIN),
        annotations=write_annotations(
            title="Change membership roles", idempotent=True, requires_user_interaction=True
        ),
    )
    @tool_errors
    async def update_membership(
        membership_id: Annotated[
            int,
            Field(
                description="Membership to change, from list_memberships. This is the membership "
                "id, not the principal id and not the project id."
            ),
        ],
        role_ids: Annotated[
            list[int],
            Field(
                description="The complete set of roles the principal should end up with, by id "
                "from list_roles. This REPLACES the current roles — include the ones to keep, or "
                "they are removed."
            ),
        ],
    ) -> MembershipRow:
        """Replace the roles of an existing membership (admin-gated).

        Use it to promote or demote somebody inside a project. The project and
        the principal of a membership cannot be changed: to move access to a
        different project, delete this membership and create the other one.

        Runs through the membership form first, so an unknown or non-assignable
        role comes back with the roles this instance actually offers. Returns
        the updated membership `{id, project, principal, roles, updated_at}`.

        Pitfalls. `role_ids` is a **replacement set**, not an addition — read the
        current roles with `list_memberships` and send the full list you want.
        Sending an empty list is refused locally, because a membership without a
        role is not a thing OpenProject accepts. No notification email is sent.
        Memberships carry no lock version, so there is no optimistic-locking
        retry here: if two admins edit the same membership, the last write wins.
        Hidden unless OPENPROJECT_MCP_ADMIN_TOOLS=1.

        Cross-references: `list_memberships` for the id and the current roles;
        `list_roles` for role ids; `delete_membership` to revoke access entirely;
        `create_membership` for a principal who has no membership yet.
        """
        ctx = get_tool_context()
        roles = _role_ids(role_ids)
        _positive_id("membership_id", membership_id)

        body: dict[str, Any] = {
            "_links": {"roles": [link("roles", role_id) for role_id in roles]},
            "_meta": _membership_meta(None),
        }
        path = f"memberships/{membership_id}"

        form = await ctx.client.post_json(f"{path}/form", json=body)
        _raise_form_validation_errors(
            form,
            fallback_hint=(
                "Fix the attributes listed in 'violations'. Role ids come from list_roles; only "
                "roles that apply to this membership's project can be assigned."
            ),
        )

        # No lockVersion on this resource, so a plain PATCH — not patch_with_lock.
        updated = await ctx.client.patch_json(path, json=body)
        return _membership_row(updated)

    @mcp.tool(
        name="delete_membership",
        tags=tool_tags(GROUP_PEOPLE, WRITE, DESTRUCTIVE, ADMIN),
        annotations=destructive_annotations(title="Revoke project membership"),
    )
    @tool_errors
    async def delete_membership(
        membership_id: Annotated[
            int,
            Field(
                description="Membership to revoke, from list_memberships. Read the row first: "
                "this id identifies one principal's access to one project."
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description="Must be true. Ask the user to confirm first — revoking access is "
                "immediate and the API offers no undo. Calling with confirm=false returns a "
                "confirmation_required error rather than removing anything."
            ),
        ] = False,
    ) -> MembershipDeletion:
        """Revoke a principal's access to a project (admin-gated, destructive).

        Use only on explicit user instruction. The principal immediately loses
        the roles this membership granted, along with any project visibility
        that depended on them; OpenProject offers no API-side undo. Work
        packages they authored, commented on or are assigned to are **not**
        deleted, but an assignee who is no longer a member may need to be
        reassigned.

        Returns a small confirmation object once OpenProject accepts the
        deletion.

        Pitfalls. If the principal also belongs to a group that is a member of
        the project, they keep access through the group — check
        `list_memberships(project_id=...)` for group rows before concluding that
        access is gone. Removing a *group* membership removes it for every
        member of that group at once. When you only want to reduce someone's
        rights, `update_membership` with a narrower role is the better answer.
        Hidden unless OPENPROJECT_MCP_ADMIN_TOOLS=1.

        Cross-references: `list_memberships` for the membership id;
        `update_membership` to demote instead of revoke; `search_principals` to
        confirm which principal a membership belongs to.
        """
        require_confirmation(
            confirm,
            action="revoke membership",
            target=f"#{membership_id}",
            consequence=(
                "The principal loses the roles this membership granted and any project access "
                "that depended on them, immediately and without an undo."
            ),
        )
        ctx = get_tool_context()
        _positive_id("membership_id", membership_id)
        await ctx.client.delete(f"memberships/{membership_id}")
        return MembershipDeletion(
            id=membership_id,
            deleted=True,
            message=f"Membership #{membership_id} was revoked.",
        )

    @mcp.tool(
        name="list_roles",
        tags=tool_tags(GROUP_PEOPLE, READ),
        annotations=read_annotations(title="List roles"),
    )
    @tool_errors
    async def list_roles(
        include_permissions: Annotated[
            bool,
            Field(
                description="Add each role's full permission identifier array. Off by default "
                "because those arrays are long — a dozen roles can run to thousands of tokens. "
                "Turn it on only when the question is genuinely 'what does this role allow'."
            ),
        ] = False,
    ) -> ListEnvelope[RoleRow]:
        """List the roles this instance defines, with their ids.

        This is the id-producing tool for `create_membership.role_ids` and
        `update_membership.role_ids` — role names are never accepted there.
        Roles are instance-wide definitions ('Member', 'Reader', 'Project
        admin'); a membership binds one principal to one project with a set of
        them.

        Returns the standard list envelope with `has_more: false`: the role list
        is small and fetched in full. Each item is `{id, name}`, plus
        `permissions` when `include_permissions=true`.

        Pitfalls. Role names are configurable per instance, so do not assume
        'Member' exists — read the list. Some roles are not assignable to a
        project membership (global and work-package roles live in the same
        collection); the membership form rejects those with the assignable set
        listed. Not every OpenProject version exposes permission arrays on this
        endpoint: when `include_permissions=true` returns none, `notes` says so
        rather than pretending the roles grant nothing.

        Cross-references: `create_membership` / `update_membership` consume these
        ids; `list_memberships` shows which roles are in use; `list_permissions`
        answers what the *current user* may do, which is the more useful question
        when a call just failed with 403.
        """
        ctx = get_tool_context()
        payload = await ctx.client.get_json(
            "roles", params=query_params(page=1, page_size=MAX_PAGE_SIZE)
        )
        roles = hal.collection(payload)
        rows = [_role_row(element, include_permissions=include_permissions) for element in roles]

        notes: list[str] = []
        if include_permissions:
            notes.append(ROLE_TOKEN_NOTE)
            if not any(row.permissions for row in rows):
                notes.append(ROLE_PERMISSIONS_MISSING_NOTE)
        total = max(roles.total, len(rows))
        if total > len(rows):
            notes.append(
                f"this instance defines {total} roles; the first {len(rows)} are returned."
            )
        return build_envelope(
            rows,
            total=total,
            page=1,
            page_size=max(len(rows), 1),
            notes=notes or None,
        )
