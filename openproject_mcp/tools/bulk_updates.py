"""Preview and apply bounded batches using the individual update's form and locking flow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    ConflictError,
    InputValidationError,
    NetworkError,
    OpenProjectError,
    UnexpectedResponseError,
    UpstreamServerError,
)
from openproject_mcp.projections import ErrorDetail, Ref
from openproject_mcp.tools import _forms, _shared
from openproject_mcp.tools.work_packages import (
    PreparedWorkPackageUpdate,
    WorkPackageChanges,
    apply_work_package_update,
    prepare_work_package_update,
    story_points_dropped_note,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


class BulkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(gt=0, strict=True, description="Numeric work package id.")
    changes: WorkPackageChanges
    lock_version: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description="Required when applying: copy this item's lock_version from its preview.",
    )


class FieldChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class BulkItemResult(BaseModel):
    id: int
    status: Literal["ready", "invalid", "blocked", "updated", "conflict", "failed", "unknown"]
    lock_version: int | None = None
    changes: list[FieldChange] = Field(default_factory=list[FieldChange])
    error: ErrorDetail | None = None
    notes: list[str] | None = None


class BulkUpdateResult(BaseModel):
    dry_run: bool
    total: int
    applied: int
    partial_failure: bool
    items: list[BulkItemResult]
    notes: list[str]


def _value(payload: Mapping[str, Any], key: str, *, linked: bool) -> Any:
    if linked:
        links = hal.as_object(payload.get("_links")) or {}
        if isinstance(links.get(key), list):
            return [ref.model_dump() for ref in Ref.list_from_hal(payload, key)]
        ref = Ref.from_hal(payload, key)
        return ref.model_dump() if ref is not None else None
    value = payload.get(key)
    obj = hal.as_object(value)
    if obj is not None and "raw" in obj:
        return hal.formattable(obj)
    return value


def _diff(prepared: PreparedWorkPackageUpdate) -> list[FieldChange]:
    # Form echoes can supply readable names; only the caller's fields enter the diff.
    proposed = _forms.merge_form_payload(_forms.form_payload(prepared.form) or {}, prepared.payload)
    changes: list[FieldChange] = []
    for key in prepared.payload:
        if key == "_links":
            continue
        changes.append(
            FieldChange(
                field=key,
                before=_value(prepared.current, key, linked=False),
                after=_value(proposed, key, linked=False),
            )
        )
    for key in hal.as_object(prepared.payload.get("_links")) or {}:
        changes.append(
            FieldChange(
                field=key,
                before=_value(prepared.current, key, linked=True),
                after=_value(proposed, key, linked=True),
            )
        )
    return changes


def _preview_notes(prepared: PreparedWorkPackageUpdate) -> list[str] | None:
    # The form leaves out a storyPoints it will drop, so the preview can warn before writing.
    form_payload = _forms.form_payload(prepared.form)
    if form_payload is None:
        return None
    note = story_points_dropped_note(prepared.payload, form_payload, committed=False)
    return [note] if note else None


def _error(exc: OpenProjectError) -> ErrorDetail:
    return ErrorDetail.model_validate(exc.to_envelope()["error"])


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="bulk_update_work_packages",
        tags=_shared.tool_tags(_shared.GROUP_WORK_PACKAGES, _shared.WRITE),
        annotations=_shared.write_annotations(title="Preview or apply work package updates"),
    )
    @_shared.tool_errors
    async def bulk_update_work_packages(
        updates: Annotated[list[BulkUpdate], Field(min_length=1, max_length=50)],
        dry_run: Annotated[
            bool, Field(description="Defaults to preview: validate and show changes without PATCH.")
        ] = True,
        notify: Annotated[
            bool, Field(description="Send notification emails when applying updates.")
        ] = True,
    ) -> BulkUpdateResult:
        """Preview or apply up to 50 work-package updates, each with its own changes.

        First call with dry_run=true (default). Review each before/after diff and validation
        error, then submit the same updates with dry_run=false and each preview's lock_version.
        Omitted fields remain untouched; null clears clearable fields; target_versions=[] clears
        version assignments. Status/type/priority accept names or ids, as in update_work_package.

        All items are validated before any update. If any preflight fails, nothing is applied;
        correct those items or submit a valid subset. Execution is sequential and NOT atomic:
        concurrent edits can still conflict after validation. Each item reports its result;
        partial_failure is true when some apply and others fail. Item notes flag values
        OpenProject will ignore or ignored, such as story_points without Backlogs. An unknown
        outcome means a connection/server failure may have happened after committing: re-read
        that item before retrying. Writes are never automatically retried or rolled back. Never
        rerun successful items. Notifications use the same notify flag for the whole batch.
        """
        ids = [item.id for item in updates]
        if len(set(ids)) != len(ids):
            raise InputValidationError(
                "Duplicate work-package ids in batch.",
                hint="Combine changes for each id into one item.",
            )
        if not dry_run and any(item.lock_version is None for item in updates):
            raise InputValidationError(
                "Every applied item needs its preview lock_version.",
                hint="Run dry_run=true and review the returned changes first.",
            )
        ctx = _shared.get_tool_context()
        prepared_items: list[PreparedWorkPackageUpdate | None] = []
        results: list[BulkItemResult] = []
        for index, item in enumerate(updates):
            await _shared.report_progress(index, len(updates), "validating batch")
            try:
                prepared = await prepare_work_package_update(
                    ctx,
                    item.id,
                    item.changes,
                    item.lock_version,
                    include_current=True,
                )
                results.append(
                    BulkItemResult(
                        id=item.id,
                        status="ready",
                        lock_version=prepared.lock_version,
                        changes=_diff(prepared),
                        notes=_preview_notes(prepared),
                    )
                )
                prepared_items.append(prepared)
            except OpenProjectError as exc:
                results.append(BulkItemResult(id=item.id, status="invalid", error=_error(exc)))
                prepared_items.append(None)
        invalid = any(item.status == "invalid" for item in results)
        notes = ["Preview only; no work packages were updated."] if dry_run else []
        if invalid and not dry_run:
            notes.append("No updates applied: at least one item failed preflight.")
            for result in results:
                if result.status == "ready":
                    result.status = "blocked"
        if not dry_run and not invalid:
            for index, (prepared, result) in enumerate(zip(prepared_items, results, strict=True)):
                assert prepared is not None
                await _shared.report_progress(index, len(updates), "applying batch")
                try:
                    updated = await apply_work_package_update(ctx, prepared, notify=notify)
                    result.status = "updated"
                    result.lock_version = updated.lock_version
                    result.notes = updated.notes
                except OpenProjectError as exc:
                    result.status = (
                        "conflict"
                        if isinstance(exc, ConflictError)
                        else "unknown"
                        if isinstance(
                            exc, (NetworkError, UpstreamServerError, UnexpectedResponseError)
                        )
                        else "failed"
                    )
                    result.error = _error(exc)
                except Exception:
                    # Preserve earlier successes even if an unexpected response cannot be parsed.
                    result.status = "unknown"
                    result.error = _error(
                        UnexpectedResponseError(
                            "Could not determine this update's outcome.",
                            hint="Re-read before retrying; the update may already be committed.",
                        )
                    )
            notes.append(
                "Batch writes are not atomic; inspect every item before retrying failures."
            )
        await _shared.report_progress(len(updates), len(updates), "batch complete")
        applied = sum(result.status == "updated" for result in results)
        return BulkUpdateResult(
            dry_run=dry_run,
            total=len(updates),
            applied=applied,
            partial_failure=0 < applied < len(updates),
            items=results,
            notes=notes,
        )
