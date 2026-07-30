"""The form pre-flight every write shares (SPEC §4.5).

``POST /<collection>/form`` answers 200 with three things a write needs: the
``payload`` OpenProject would commit (instance defaults filled in), the
``schema`` (including allowed values), and ``validationErrors`` keyed by
attribute. Asking the form first is what turns "422, no" into "422, and these
are the statuses you may actually move to".

The flow is the same for work packages, projects, versions, memberships and
time entries, so it is written once here::

    form = await ctx.client.post_json("versions/form", json=payload)
    _raise_form_validation_errors(form)   # domain wrapper, see below
    body = merge_form_payload(form_payload(form) or {}, payload)
    created = await ctx.client.post_json("versions", json=body)

What stays in the tool module is the domain half: building ``payload``, and a
one-line wrapper around :func:`raise_validation_errors` that supplies the
subject ("work package", "version") and the fallback hint pointing at the tool
that lists the valid values.

Nothing here imports the server or a tool module, so any module may use it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from openproject_mcp.client.errors import ValidationFailedError, violations_from_form
from openproject_mcp.client.hal import as_object, as_objects, embedded

__all__ = [
    "HintBuilder",
    "allowed_titles",
    "allowed_value_hints",
    "form_payload",
    "form_schema",
    "merge_form_payload",
    "raise_validation_errors",
]

#: Builds extra hints from ``(form, validation_errors)``. Pass one to
#: :func:`raise_validation_errors` when the domain knows something the schema
#: does not ("identifiers must be unique", "sharing must be one of …").
type HintBuilder = Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[str]]

#: Allowed values longer than this are cut in a hint, to protect the context.
MAX_LISTED_VALUES = 25


def form_payload(form: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The form's ``_embedded.payload``: our attributes plus instance defaults."""
    return as_object(embedded(form, "payload"))


def form_schema(form: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The form's ``_embedded.schema``, which carries the allowed values."""
    return as_object(embedded(form, "schema"))


def allowed_titles(entry: Any) -> list[str]:
    """Display names of a schema attribute's ``allowedValues``, wherever they sit.

    The API puts them under ``_links.allowedValues`` (link objects) or
    ``_embedded.allowedValues`` (full resources) depending on the attribute; a
    lone link object means "fetch them yourself" and yields no titles.
    """
    attribute = as_object(entry)
    if attribute is None:
        return []
    for container_key in ("_links", "_embedded"):
        container = as_object(attribute.get(container_key))
        if container is None:
            continue
        titles: list[str] = []
        for item in as_objects(container.get("allowedValues")):
            title = item.get("title") or item.get("name") or item.get("value")
            if isinstance(title, str):
                titles.append(title)
        if titles:
            return titles
    return []


def allowed_value_hints(form: Mapping[str, Any], validation_errors: Mapping[str, Any]) -> list[str]:
    """One "Allowed values for X: …" hint per rejected attribute the schema lists.

    This is the half the old server threw away: the form response knows both
    what is wrong *and* which values would be accepted, so an invalid status
    transition names the statuses that are actually reachable.
    """
    schema = form_schema(form)
    hints: list[str] = []
    for attribute in validation_errors:
        titles = allowed_titles(schema.get(attribute) if schema is not None else None)
        if titles:
            listed = ", ".join(titles[:MAX_LISTED_VALUES])
            if len(titles) > MAX_LISTED_VALUES:
                listed += "…"
            hints.append(f"Allowed values for {attribute}: {listed}.")
    return hints


def raise_validation_errors(
    form: Mapping[str, Any],
    *,
    subject: str,
    fallback_hint: str,
    hints: HintBuilder | None = None,
) -> None:
    """Turn a form's ``validationErrors`` into a typed 422, or return quietly.

    Args:
        form: the parsed form response.
        subject: what was rejected, for the fallback message ("work package").
        fallback_hint: used when neither ``hints`` nor the schema produced one.
        hints: builds the domain hints; :func:`allowed_value_hints` covers the
            common "the schema lists what is allowed" case.

    Raises:
        ValidationFailedError: with per-attribute ``violations``, the same shape
            a real 422 body produces, so callers handle one error only.
    """
    errors = as_object(embedded(form, "validationErrors"))
    if not errors:
        return

    violations = violations_from_form(errors)
    collected = list(hints(form, errors)) if hints is not None else []
    if not collected:
        collected.append(fallback_hint)

    identifier: str | None = None
    first = next((entry for value in errors.values() if (entry := as_object(value))), None)
    if first is not None:
        raw = first.get("errorIdentifier")
        identifier = raw if isinstance(raw, str) else None

    raise ValidationFailedError(
        violations[0]["message"] if violations else f"OpenProject rejected the {subject}.",
        http_status=422,
        error_identifier=identifier,
        hint=" ".join(collected),
        violations=violations,
    )


def _links_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    links = as_object(payload.get("_links"))
    return dict(links) if links is not None else {}


def merge_form_payload(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the form's defaulted payload with ours; ours wins, ``_links`` merge.

    Merging matters in both directions: the form fills in what OpenProject
    derives (a generated identifier, the default status, scheduling flags) while
    our payload carries the caller's intent *and* claims the form never echoes,
    such as ``_links.attachments``. ``self`` is dropped — a create payload never
    echoes one back.
    """
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
