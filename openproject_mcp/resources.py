"""MCP resource templates: ``openproject://…`` URIs (SPEC §10).

Resources give resource-aware clients (``@openproject:`` mentions in Claude
Code) direct reads of work packages, projects and attachment bytes without a
tool call.

Three templates land here:

============================================  ==============================================
URI template                                  Content
============================================  ==============================================
``openproject://work_package/{id}``           work-package detail projection (JSON)
``openproject://project/{identifier}``        project overview (JSON)
``openproject://attachment/{id}``             attachment bytes (blob, real MIME type)
============================================  ==============================================

Non-negotiables for this module:

* **Same shapes as the tools.** The JSON is the shared
  :class:`~openproject_mcp.projections.WorkPackageDetail` and the
  :class:`~openproject_mcp.tools.projects.ProjectDetail` that ``get_work_package``
  and ``get_project`` return, so a resource read and a tool call can be compared
  field by field. What a resource cannot carry is stated in-band: the work-package
  projection leaves ``custom_fields`` empty and says so in ``notes``, because
  resolving names and types costs a second schema request that a mention-time
  read should not pay.
* **Errors are data** (G4). A failed read raises ``ResourceError`` whose message
  *is* the SPEC §4.2 ``{"error": {...}}`` envelope, so a 404 reaches the client
  with a type, a status and a hint rather than a stack trace.
* **The attachment path is the tool's path.** Bytes come from
  ``/attachments/{id}/content`` through ``client.stream`` (which strips
  ``Authorization`` on the cross-origin redirect to object storage), the
  configured ``OPENPROJECT_MCP_MAX_DOWNLOAD_MB`` cap is enforced both from the
  declared size and while streaming, the tool's wall-clock deadline is enforced
  too (a streamed body has no read timeout, so a stalled transfer would
  otherwise hang the session), and a quarantined attachment is refused before a
  byte is fetched.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ResourceError
from fastmcp.resources import ResourceContent, ResourceResult

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    AttachmentQuarantinedError,
    AttachmentTooLargeError,
    NetworkError,
    OpenProjectError,
)
from openproject_mcp.projections import Ref, WorkPackageDetail, WorkPackageRow
from openproject_mcp.tools import _shared
from openproject_mcp.tools.attachments import (
    DOWNLOAD_CHUNK_BYTES,
    DOWNLOAD_DEADLINE_SECONDS,
    QUARANTINED_STATUS,
    AttachmentRow,
)
from openproject_mcp.tools.projects import ProjectDetail

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "ATTACHMENT_URI",
    "PROJECT_URI",
    "WORK_PACKAGE_URI",
    "register",
]

WORK_PACKAGE_URI = "openproject://work_package/{id}"
PROJECT_URI = "openproject://project/{identifier}"
ATTACHMENT_URI = "openproject://attachment/{id}"

#: MIME type used when OpenProject reports none for an attachment.
FALLBACK_MIME_TYPE = "application/octet-stream"

#: MIME type of the JSON templates. FastMCP's resource *templates* do not carry
#: their declared type onto a plain string return (unlike static resources), so
#: every read builds its own :class:`ResourceContent` and states the type there.
JSON_MIME_TYPE = "application/json"

CUSTOM_FIELDS_NOTE = (
    "custom fields are not resolved in the resource projection (their names and types need a "
    "second schema request); read them with get_work_package(id=...)"
)


def _json_result(payload: str) -> ResourceResult:
    """One JSON text content, typed at read time rather than at declaration."""
    return ResourceResult(contents=[ResourceContent(payload, mime_type=JSON_MIME_TYPE)])


def _resource_error(exc: OpenProjectError) -> ResourceError:
    """Wrap a taxonomy error so the client receives the §4.2 JSON envelope."""
    return ResourceError(exc.to_json())


def _availability(payload: dict[str, Any]) -> dict[str, bool]:
    """Which optional surfaces this work package exposes (SPEC §6.2, G5)."""
    links = hal.as_object(payload.get("_links"))
    keys: set[str] = set(links) if links is not None else set()
    return {
        "dev_links": bool(keys & {"revisions", "github", "gitlab"}),
        "meetings": bool(keys & {"meetings", "meetingAgendaItems"}),
        "files": "fileLinks" in keys,
    }


def _work_package_detail(payload: dict[str, Any]) -> WorkPackageDetail:
    """The shared detail projection, minus the schema-resolved custom fields."""
    return WorkPackageDetail(
        **_row_fields(payload),
        description=hal.formattable(payload.get("description")),
        author=Ref.from_hal(payload, "author"),
        responsible=Ref.from_hal(payload, "responsible"),
        version=Ref.from_hal(payload, "version"),
        category=Ref.from_hal(payload, "category"),
        parent=Ref.from_hal(payload, "parent"),
        estimated_hours=hal.duration_hours(payload.get("estimatedTime")),
        spent_hours=hal.duration_hours(payload.get("spentTime")),
        created_at=payload.get("createdAt"),
        lock_version=payload.get("lockVersion"),
        custom_fields=[],
        available=_availability(payload),
        notes=[CUSTOM_FIELDS_NOTE],
    )


def _row_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """The compact-row half of the projection, from the one shared constructor."""
    return WorkPackageRow.from_hal(payload).model_dump()


def _status_code(payload: dict[str, Any]) -> str | None:
    """The project's status code from ``_links.status`` (or the pre-13 object)."""
    linked = hal.ref(payload, "status")
    if linked is not None and linked.id is not None:
        return str(linked.id)
    raw = payload.get("status")
    inlined = hal.as_object(raw)
    if inlined is not None:
        code = inlined.get("code")
        return code if isinstance(code, str) else None
    return raw if isinstance(raw, str) else None


def _project_detail(payload: dict[str, Any]) -> ProjectDetail:
    identifier = payload.get("identifier")
    return ProjectDetail(
        id=hal.self_id(payload),
        identifier=identifier if isinstance(identifier, str) else None,
        name=payload.get("name"),
        active=payload.get("active"),
        public=payload.get("public"),
        parent=Ref.from_hal(payload, "parent"),
        status_code=_status_code(payload),
        description=hal.formattable(payload.get("description")),
        status_explanation=hal.formattable(payload.get("statusExplanation")),
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
    )


def register(mcp: FastMCP) -> None:
    """Register the three resource templates (SPEC §10)."""

    @mcp.resource(
        WORK_PACKAGE_URI,
        name="openproject_work_package",
        title="OpenProject work package",
        description=(
            "One work package as JSON: subject, description (markdown), type, status, "
            "priority, assignee, dates, progress, estimate, spent time and lock_version. "
            "The id is the #1234 number; find it with search_work_packages. Custom fields "
            "need get_work_package."
        ),
        mime_type=JSON_MIME_TYPE,
        tags={_shared.GROUP_WORK_PACKAGES, _shared.READ},
    )
    async def work_package_resource(id: str) -> ResourceResult:
        """Read one work package as the shared detail projection."""
        ctx = _shared.get_tool_context()
        try:
            payload = await ctx.client.get_json(f"work_packages/{id}")
        except OpenProjectError as exc:
            raise _resource_error(exc) from exc
        return _json_result(_work_package_detail(payload).model_dump_json(exclude_none=False))

    @mcp.resource(
        PROJECT_URI,
        name="openproject_project",
        title="OpenProject project",
        description=(
            "One project as JSON: name, identifier, description (markdown), active/public "
            "flags, parent and status. The identifier is the URL slug (/projects/<identifier>); "
            "the numeric id works too. Find both with list_projects."
        ),
        mime_type=JSON_MIME_TYPE,
        tags={_shared.GROUP_PROJECTS, _shared.READ},
    )
    async def project_resource(identifier: str) -> ResourceResult:
        """Read one project overview."""
        ctx = _shared.get_tool_context()
        try:
            payload = await ctx.client.get_json(f"projects/{identifier}")
        except OpenProjectError as exc:
            raise _resource_error(exc) from exc
        return _json_result(_project_detail(payload).model_dump_json(exclude_none=False))

    @mcp.resource(
        ATTACHMENT_URI,
        name="openproject_attachment",
        title="OpenProject attachment",
        description=(
            "The bytes of one attachment, served with the MIME type OpenProject detected. "
            "Attachment ids come from list_attachments or "
            "get_work_package(include=['attachments']). Quarantined files and files above "
            "OPENPROJECT_MCP_MAX_DOWNLOAD_MB are refused with a structured error."
        ),
        mime_type=FALLBACK_MIME_TYPE,
        tags={_shared.GROUP_ATTACHMENTS, _shared.READ},
    )
    async def attachment_resource(id: str) -> ResourceResult:
        """Stream one attachment's bytes as a blob with its real MIME type."""
        ctx = _shared.get_tool_context()
        try:
            metadata = await ctx.client.get_json(f"attachments/{id}")
            row = _attachment_row(metadata)
            _guard_attachment(row, ctx.settings.max_download_bytes)
            payload = await _download(ctx, id, cap=ctx.settings.max_download_bytes, row=row)
        except OpenProjectError as exc:
            raise _resource_error(exc) from exc
        return ResourceResult(
            contents=[ResourceContent(payload, mime_type=row.content_type or FALLBACK_MIME_TYPE)],
            meta={"file_name": row.file_name, "size_bytes": len(payload)},
        )


def _attachment_row(payload: dict[str, Any]) -> AttachmentRow:
    """The same compact attachment projection ``list_attachments`` returns."""
    file_size = payload.get("fileSize")
    return AttachmentRow(
        id=hal.self_id(payload),
        file_name=payload.get("fileName") if isinstance(payload.get("fileName"), str) else None,
        size_bytes=(
            file_size if isinstance(file_size, int) and not isinstance(file_size, bool) else None
        ),
        content_type=(
            payload.get("contentType") if isinstance(payload.get("contentType"), str) else None
        ),
        description=hal.formattable(payload.get("description")),
        author=Ref.from_hal(payload, "author"),
        created_at=payload.get("createdAt") if isinstance(payload.get("createdAt"), str) else None,
        status=payload.get("status") if isinstance(payload.get("status"), str) else None,
    )


def _guard_attachment(row: AttachmentRow, cap: int) -> None:
    """Refuse a quarantined or oversized attachment before fetching bytes (SPEC §7.1)."""
    name = row.file_name or f"attachment-{row.id}"
    if row.status == QUARANTINED_STATUS:
        raise AttachmentQuarantinedError(
            f"Attachment {row.id} ({name}) is quarantined by the virus scanner.",
            hint=(
                "OpenProject will not serve the bytes of a quarantined file and neither will "
                "this resource. An administrator must review and release it first."
            ),
            extra={"attachment_id": row.id, "file_name": name},
        )
    if row.size_bytes is not None and row.size_bytes > cap:
        raise AttachmentTooLargeError(
            f"Attachment {row.id} ({name}) is {row.size_bytes} bytes, above the {cap} byte "
            "download limit.",
            hint=(
                "Nothing was downloaded. Raise OPENPROJECT_MCP_MAX_DOWNLOAD_MB, or fetch the "
                "file with download_attachment, which writes it to disk instead of inlining it."
            ),
            extra={"attachment_id": row.id, "size_bytes": row.size_bytes},
        )


async def _download(
    ctx: _shared.ToolContext, attachment_id: str, *, cap: int, row: AttachmentRow
) -> bytes:
    """Stream the bytes, aborting on the size cap or the wall clock mid-flight.

    ``client.stream`` deliberately sets no read timeout, so both caps are the
    caller's job (SPEC §4.1/§7.1): without the deadline a trickling transfer from
    object storage would hang the resource read — and on stdio, the session.
    """
    name = row.file_name or f"attachment-{attachment_id}"
    chunks: list[bytes] = []
    written = 0
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    async with ctx.client.stream("GET", f"attachments/{attachment_id}/content") as response:
        async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
            written += len(chunk)
            if written > cap:
                raise AttachmentTooLargeError(
                    f"Attachment {attachment_id} ({name}) exceeded the {cap} byte download "
                    "limit while streaming.",
                    hint=(
                        "The transfer was aborted. The attachment's reported size was smaller "
                        "than what it actually sent; raise OPENPROJECT_MCP_MAX_DOWNLOAD_MB or "
                        "use download_attachment."
                    ),
                )
            if time.monotonic() > deadline:
                raise NetworkError(
                    f"Reading attachment {attachment_id} ({name}) exceeded the "
                    f"{DOWNLOAD_DEADLINE_SECONDS:.0f}s limit after {written} bytes.",
                    hint=(
                        "The connection stalled and nothing was returned. Retry, or fetch the "
                        "file with download_attachment, which writes it to disk."
                    ),
                )
            chunks.append(chunk)
            await _shared.report_progress(written, row.size_bytes or None, f"{name}: {written} B")
    return b"".join(chunks)
