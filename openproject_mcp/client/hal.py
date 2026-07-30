"""HAL parsing — the single implementation (SPEC §4.3).

OpenProject speaks HAL+JSON: resources carry ``_links`` (references to other
resources) and ``_embedded`` (inlined resources and collection elements). This
module is the *only* place that knows about hrefs. Tool output uses
``projections.Ref`` (``{id, name}``); hrefs stop here.

Three jobs:

* :func:`ref` / :func:`id_from_href` — turn a link into ``{id, name, href}``.
* :func:`collection` — unwrap ``_embedded.elements`` plus ``total``, ``count``,
  ``pageSize``, ``offset``.
* :func:`formattable` — ``{format, raw, html}`` fields surface as ``raw``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import unquote, urlsplit

__all__ = [
    "HalCollection",
    "HalRef",
    "as_array",
    "as_object",
    "as_objects",
    "collection",
    "duration_hours",
    "embedded",
    "formattable",
    "id_from_href",
    "ref",
    "refs",
    "self_href",
    "self_id",
]

_DURATION_RE = re.compile(
    r"^(?P<sign>-)?P"
    r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


@dataclass(frozen=True, slots=True)
class HalRef:
    """A resolved ``_links`` entry. ``href`` is internal and never surfaced."""

    id: int | str | None = None
    name: str | None = None
    href: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.id is None and self.name is None and self.href is None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "href": self.href}


@dataclass(frozen=True, slots=True)
class HalCollection:
    """An unwrapped HAL collection.

    ``offset`` is OpenProject's 1-based *page number*, not a record offset
    (SPEC §9.1) — it is surfaced verbatim here and translated in
    ``filters.pagination_params`` / the list envelope builder.
    """

    elements: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    total: int = 0
    count: int = 0
    page_size: int | None = None
    offset: int | None = None
    groups: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    total_sums: dict[str, Any] = field(default_factory=dict[str, Any])

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.elements)

    def __len__(self) -> int:
        return len(self.elements)


def id_from_href(href: str | None) -> int | str | None:
    """Parse the resource id out of an API href.

    ``/api/v3/work_packages/1234`` → ``1234`` (int)
    ``/api/v3/projects/my-project`` → ``"my-project"`` (identifier)
    ``/api/v3/work_packages/{id}``  → ``None`` (URI template, no id yet)
    ``None`` / ``""`` / query-only hrefs → ``None``

    Numeric tails become ints so ids round-trip into API paths and filters
    without string/int ambiguity.
    """
    if not href:
        return None
    path = urlsplit(href).path.rstrip("/")
    if not path:
        return None
    tail = unquote(path.rsplit("/", 1)[-1])
    if not tail or (tail.startswith("{") and tail.endswith("}")):
        return None
    # Only plain ASCII digits are ids; anything else (including a leading sign
    # or non-ASCII digits) is a project identifier and stays a string.
    if tail.isascii() and tail.isdigit():
        return int(tail)
    return tail


def as_object(value: Any) -> Mapping[str, Any] | None:
    """Narrow an untyped JSON value to an object; ``None`` for anything else.

    A bare ``isinstance(value, Mapping)`` leaves the key and value types
    unknown, and that spreads through every ``.get`` downstream. This is the one
    place that pins them down, so parsing code stays both readable and typed.
    """
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[str, Any], value)


def as_array(value: Any) -> Sequence[Any] | None:
    """Narrow an untyped JSON value to an array; strings and bytes are not one."""
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        return None
    return cast(Sequence[Any], value)


def as_objects(value: Any) -> list[Mapping[str, Any]]:
    """Every object inside an untyped JSON array; anything else is skipped."""
    array = as_array(value)
    if array is None:
        return []
    return [item for item in array if isinstance(item, Mapping)]


def _link_entry(payload: Mapping[str, Any] | None, key: str) -> Any:
    links = as_object(payload.get("_links") if payload is not None else None)
    return links.get(key) if links is not None else None


def ref(payload: Mapping[str, Any] | None, key: str) -> HalRef | None:
    """Extract ``payload["_links"][key]`` as a :class:`HalRef`.

    Returns ``None`` when the link is absent or explicitly null
    (``{"href": null}`` means "no value", e.g. an unassigned work package).
    Falls back to ``_embedded[key]`` when the server inlined the resource.
    """
    raw = _link_entry(payload, key)
    if raw is None:
        return _ref_from_embedded(payload, key)
    # A link may also be an array (``_links.customField9``); the first object wins.
    entry = next(iter(as_objects(raw)), None) if as_array(raw) is not None else as_object(raw)
    if entry is None:
        return None
    raw_href = entry.get("href")
    if raw_href is None and not entry.get("title"):
        return None
    href = raw_href if isinstance(raw_href, str) else None
    title = entry.get("title")
    resolved = HalRef(
        id=id_from_href(href),
        name=title if isinstance(title, str) else None,
        href=href,
    )
    if resolved.is_empty:
        return None
    if resolved.name is None:
        embedded_ref = _ref_from_embedded(payload, key)
        if embedded_ref is not None and embedded_ref.name is not None:
            return HalRef(id=resolved.id, name=embedded_ref.name, href=resolved.href)
    return resolved


def _ref_from_embedded(payload: Mapping[str, Any] | None, key: str) -> HalRef | None:
    inlined = as_object(embedded(payload, key))
    if inlined is None:
        return None
    href = self_href(inlined)
    name = inlined.get("name") or inlined.get("subject") or inlined.get("title")
    resolved = HalRef(
        id=self_id(inlined),
        name=name if isinstance(name, str) else None,
        href=href,
    )
    return None if resolved.is_empty else resolved


def refs(payload: Mapping[str, Any] | None, key: str) -> list[HalRef]:
    """Extract a link *array* (e.g. ``_links.children``) as a list of refs."""
    resolved: list[HalRef] = []
    for item in as_objects(_link_entry(payload, key)):
        href = item.get("href")
        title = item.get("title")
        candidate = HalRef(
            id=id_from_href(href if isinstance(href, str) else None),
            name=title if isinstance(title, str) else None,
            href=href if isinstance(href, str) else None,
        )
        if not candidate.is_empty:
            resolved.append(candidate)
    return resolved


def embedded(payload: Mapping[str, Any] | None, key: str) -> Any:
    """Return ``payload["_embedded"][key]`` or ``None``."""
    inner = as_object(payload.get("_embedded") if payload is not None else None)
    return inner.get(key) if inner is not None else None


def self_href(payload: Mapping[str, Any] | None) -> str | None:
    entry = as_object(_link_entry(payload, "self"))
    if entry is not None:
        href = entry.get("href")
        if isinstance(href, str):
            return href
    return None


def self_id(payload: Mapping[str, Any] | None) -> int | str | None:
    """The resource's own id: from ``_links.self.href``, else the ``id`` field.

    Every projection carries its own id so sibling tools can consume it
    (activity ids, relation ids, …) — SPEC §4.3.
    """
    resolved = id_from_href(self_href(payload))
    if resolved is not None:
        return resolved
    if payload is not None:
        raw = payload.get("id")
        if isinstance(raw, int | str):
            return raw
    return None


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return default


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def collection(payload: Mapping[str, Any] | Sequence[Any] | None) -> HalCollection:
    """Unwrap a HAL collection response.

    Tolerates every shape OpenProject actually returns: a proper Collection
    with ``_embedded.elements``, a bare list under ``_embedded.elements``, or a
    payload that is itself a list of resources. ``total`` falls back to the
    element count so callers never report a total of 0 for a non-empty page.
    """
    if payload is None:
        return HalCollection()
    if not isinstance(payload, Mapping):
        # Some endpoints answer with a bare array of resources.
        elements = [dict(item) for item in as_objects(payload)]
        return HalCollection(elements=elements, total=len(elements), count=len(elements))

    raw_elements = embedded(payload, "elements")
    if raw_elements is None:
        raw_elements = payload.get("elements")
    elements = [dict(item) for item in as_objects(raw_elements)]

    raw_groups = payload.get("groups")
    if raw_groups is None:
        raw_groups = embedded(payload, "groups")
    groups = [dict(item) for item in as_objects(raw_groups)]

    sums = as_object(payload.get("totalSums"))

    return HalCollection(
        elements=elements,
        total=_int_or(payload.get("total"), len(elements)),
        count=_int_or(payload.get("count"), len(elements)),
        page_size=_optional_int(payload.get("pageSize")),
        offset=_optional_int(payload.get("offset")),
        groups=groups,
        total_sums=dict(sums) if sums is not None else {},
    )


def formattable(value: Any) -> str | None:
    """Surface a formattable field's ``raw`` text; ``html`` is always dropped.

    Accepts the ``{"format": "markdown", "raw": "...", "html": "..."}`` object,
    a plain string (some endpoints send one), or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    mapping = as_object(value)
    if mapping is not None:
        raw = mapping.get("raw")
        if isinstance(raw, str):
            return raw
    return None


def duration_hours(value: Any) -> float | None:
    """Convert an ISO-8601 duration to float hours.

    ``"PT7H30M"`` → ``7.5``. Tool output always uses float hours, never the
    wire duration string (SPEC §5.8). Numbers pass through unchanged.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    parts = match.groupdict()
    total = (
        float(parts["days"] or 0) * 24
        + float(parts["hours"] or 0)
        + float(parts["minutes"] or 0) / 60
        + float(parts["seconds"] or 0) / 3600
    )
    return -total if parts["sign"] else total
