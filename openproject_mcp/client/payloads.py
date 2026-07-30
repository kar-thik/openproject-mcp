"""Typed payload builders for writes (SPEC §4.5, §6.2.1).

OpenProject writes split into two halves: plain attributes (``subject``,
``startDate``, …) and ``_links`` (everything that points at another resource —
assignee, status, type, parent, version, custom fields backed by options).

Everything that produces an href lives here, so no tool ever hand-builds one.
That is what fixes the two classic bugs:

* clearing a value means ``{"href": null}``, not ``/api/v3/users/None``;
* a custom field written by display name is resolved against the cached schema
  and fails loudly when unknown, instead of being silently dropped (G2).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from openproject_mcp.client.errors import InputValidationError
from openproject_mcp.client.hal import id_from_href

__all__ = [
    "build_write_payload",
    "custom_field_payload",
    "formattable_field",
    "href_for",
    "link",
    "links_payload",
    "resolve_custom_field_key",
]

API_ROOT = "/api/v3"

#: Custom-field schema types whose values are written as ``_links`` entries.
LINK_CUSTOM_FIELD_TYPES: dict[str, str] = {
    "User": "users",
    "Group": "groups",
    "Principal": "principals",
    "Version": "versions",
    "CustomOption": "custom_options",
    "Project": "projects",
    "Category": "categories",
}

#: Named link builders exposed to tools: parameter name → API collection.
LINK_RESOURCES: dict[str, str] = {
    "assignee": "users",
    "responsible": "users",
    "author": "users",
    "principal": "principals",
    "user": "users",
    "status": "statuses",
    "type": "types",
    "priority": "priorities",
    "version": "versions",
    "category": "categories",
    "parent": "work_packages",
    "project": "projects",
    "work_package": "work_packages",
    "activity": "time_entries/activities",
    "role": "roles",
}


def href_for(resource: str, resource_id: int | str) -> str:
    """Build an API href: ``("users", 12)`` → ``/api/v3/users/12``."""
    return f"{API_ROOT}/{resource.strip('/')}/{resource_id}"


def link(resource: str, resource_id: int | str | None) -> dict[str, str | None]:
    """Build one ``_links`` entry.

    ``resource_id=None`` produces ``{"href": null}``, which is how OpenProject
    clears a link-valued field (unassign, remove parent, clear version).
    """
    if resource_id is None:
        return {"href": None}
    return {"href": href_for(resource, resource_id)}


def links_payload(**values: Any) -> dict[str, dict[str, Any]]:
    """Build a ``_links`` object from named parameters.

    Keys must be known link parameters (see :data:`LINK_RESOURCES`). A value of
    ``None`` clears the link; omit the key entirely to leave it untouched. Use
    the sentinel-free style::

        links_payload(assignee=12, status="7", parent=None)
        # {"assignee": {"href": "/api/v3/users/12"},
        #  "status": {"href": "/api/v3/statuses/7"},
        #  "parent": {"href": None}}
    """
    payload: dict[str, dict[str, Any]] = {}
    for key, value in values.items():
        resource = LINK_RESOURCES.get(key)
        if resource is None:
            raise InputValidationError(
                f"Unknown link field {key!r}.",
                hint=f"Known link fields: {', '.join(sorted(LINK_RESOURCES))}.",
            )
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            payload[key] = [link(resource, item) for item in value]  # type: ignore[assignment]
        else:
            payload[key] = link(resource, value)
    return payload


def formattable_field(text: str | None, *, fmt: str = "markdown") -> dict[str, Any] | None:
    """Wrap markdown text for a formattable field (``description``, ``comment``).

    ``None`` passes through as ``None`` so callers can distinguish "clear this"
    from "leave alone" at the payload-assembly level.
    """
    if text is None:
        return None
    return {"format": fmt, "raw": text}


def build_write_payload(
    attributes: Mapping[str, Any] | None = None,
    links: Mapping[str, Any] | None = None,
    *,
    lock_version: int | None = None,
) -> dict[str, Any]:
    """Merge attributes and ``_links`` into one request body."""
    payload: dict[str, Any] = {key: value for key, value in (attributes or {}).items()}
    if lock_version is not None:
        payload["lockVersion"] = lock_version
    if links:
        payload["_links"] = dict(links)
    return payload


# --- custom fields --------------------------------------------------------


def _schema_entries(schema: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Custom-field entries of a schema, keyed by ``customFieldN``."""
    return {
        key: value
        for key, value in schema.items()
        if key.startswith("customField") and isinstance(value, Mapping)
    }


def resolve_custom_field_key(key_or_name: str, schema: Mapping[str, Any]) -> str:
    """Resolve ``"Severity"`` or ``"customField12"`` to the wire key.

    Raises:
        InputValidationError: unknown or ambiguous name, listing the valid
            keys and their display names.
    """
    entries = _schema_entries(schema)
    if key_or_name in entries:
        return key_or_name

    lowered = key_or_name.strip().lower()
    matches = [
        key
        for key, entry in entries.items()
        if isinstance(entry.get("name"), str) and entry["name"].strip().lower() == lowered
    ]
    if len(matches) == 1:
        return matches[0]

    catalog = ", ".join(f"{key} ({entry.get('name')})" for key, entry in sorted(entries.items()))
    if len(matches) > 1:
        raise InputValidationError(
            f"Custom field name {key_or_name!r} is ambiguous on this schema.",
            hint=f"Use the explicit key instead. Candidates: {', '.join(matches)}.",
        )
    raise InputValidationError(
        f"Unknown custom field {key_or_name!r}.",
        hint=(
            f"Valid custom fields for this project/type: {catalog or '(none)'}. "
            "Use get_work_package_schema to list them."
        ),
    )


def _base_type(type_name: str) -> str:
    return type_name[2:] if type_name.startswith("[]") else type_name


def _is_multi_value(type_name: str) -> bool:
    return type_name.startswith("[]")


def _allowed_values(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Allowed values as a list of ``{href, title}``-ish mappings.

    Handles both ``_links.allowedValues`` (link objects) and
    ``_embedded.allowedValues`` (full resources). A single link object (a URL to
    fetch the values from) yields an empty list — resolution by name is then
    impossible locally and the caller is told to pass ids.
    """
    for container_key, inner_key in (("_links", "allowedValues"), ("_embedded", "allowedValues")):
        container = entry.get(container_key)
        if isinstance(container, Mapping):
            values = container.get(inner_key)
            if isinstance(values, Sequence) and not isinstance(values, str | bytes):
                return [item for item in values if isinstance(item, Mapping)]
    return []


def _option_id_and_name(option: Mapping[str, Any]) -> tuple[int | str | None, str | None]:
    href = option.get("href")
    if not isinstance(href, str):
        links = option.get("_links")
        if isinstance(links, Mapping):
            self_link = links.get("self")
            if isinstance(self_link, Mapping) and isinstance(self_link.get("href"), str):
                href = self_link["href"]
    name = option.get("title") or option.get("value") or option.get("name")
    return id_from_href(href if isinstance(href, str) else None), (
        name if isinstance(name, str) else None
    )


def _resolve_option(
    value: Any,
    entry: Mapping[str, Any],
    field_key: str,
    resource: str,
) -> dict[str, Any]:
    """Resolve one link-valued custom-field value to an ``_links`` entry."""
    if value is None:
        return {"href": None}
    if isinstance(value, Mapping) and "href" in value:
        return dict(value)
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return {"href": href_for(resource, value)}

    options = _allowed_values(entry)
    lowered = str(value).strip().lower()
    matches = [
        option
        for option in options
        if (name := _option_id_and_name(option)[1]) and name.strip().lower() == lowered
    ]
    if len(matches) == 1:
        option_id, _ = _option_id_and_name(matches[0])
        if option_id is not None:
            return {"href": href_for(resource, option_id)}
    names = [name for option in options if (name := _option_id_and_name(option)[1])]
    listed = ", ".join(names[:25]) + ("…" if len(names) > 25 else "")
    raise InputValidationError(
        f"Value {value!r} is not a valid option for custom field {field_key!r}.",
        hint=(
            f"Allowed values: {listed or '(not listed in the schema; pass a numeric id)'}. "
            "Use get_work_package_schema to see them."
        ),
    )


def custom_field_payload(
    values: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn ``{"<key or name>": value}`` into write attributes and links.

    Args:
        values: user-facing custom-field writes; keys may be ``customField12``
            or the display name (SPEC §6.2.1).
        schema: a work-package schema body (``/work_packages/schemas/{p}-{t}``).

    Returns:
        ``(attributes, links)`` ready to be merged by :func:`build_write_payload`.

    Raises:
        InputValidationError: unknown/ambiguous key, non-writable field, or an
            option name that is not in the schema's allowed values.
    """
    attributes: dict[str, Any] = {}
    links: dict[str, Any] = {}

    for raw_key, raw_value in values.items():
        key = resolve_custom_field_key(raw_key, schema)
        entry = schema[key]
        if entry.get("writable") is False:
            raise InputValidationError(
                f"Custom field {raw_key!r} ({key}) is not writable on this schema.",
                hint="Remove it from custom_fields; the schema marks it read-only.",
            )
        type_name = entry.get("type") if isinstance(entry.get("type"), str) else ""
        base = _base_type(type_name)
        resource = LINK_CUSTOM_FIELD_TYPES.get(base)

        if resource is not None:
            if _is_multi_value(type_name):
                items: Iterable[Any] = (
                    raw_value
                    if isinstance(raw_value, Sequence) and not isinstance(raw_value, str | bytes)
                    else [raw_value]
                )
                links[key] = [_resolve_option(item, entry, raw_key, resource) for item in items]
            else:
                links[key] = _resolve_option(raw_value, entry, raw_key, resource)
        elif base == "Formattable":
            attributes[key] = formattable_field(raw_value)
        else:
            attributes[key] = raw_value

    return attributes, links
