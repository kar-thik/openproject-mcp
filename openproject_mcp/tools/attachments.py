"""Attachment tools (SPEC §6.4, semantics in §7).

Lands here:

=============================  ======  ===============================================
Tool                           Phase   Endpoint(s)
=============================  ======  ===============================================
🔍 ``list_attachments``        1       ``GET /{container}/{id}/attachments``
🔍 ``download_attachment``     1       ``GET /attachments/{id}`` + ``/content``
✏️ ``upload_attachment``       1       ``POST /{container}/{id}/attachments``
🗑 ``delete_attachment``       2       ``DELETE /attachments/{id}``
🔍Ⓜ ``list_file_links``        3       ``GET /work_packages/{id}/file_links``
=============================  ======  ===============================================

Non-negotiables for this module:

* ``GET /attachments/{id}`` is metadata only; bytes come from
  ``/attachments/{id}/content``, which either streams or **302s to a presigned
  object-store URL**. Use :meth:`OpenProjectClient.stream`; the client strips
  ``Authorization`` on cross-origin redirects, so never re-add it by hand.
* Pre-flight uploads against the cached ``/configuration``
  ``maximumAttachmentFileSize`` and fail locally before burning the transfer.
* Multipart upload has exactly two parts: ``metadata`` (JSON with ``fileName``,
  optional ``description``) and ``file``. The server re-detects the content type
  from the bytes; only ``metadata.fileName`` drives naming.
* Quarantined attachments produce a structured error, not a download attempt.
* Sanitize ``fileName`` before touching the filesystem, uniquify collisions, and
  honor ``OPENPROJECT_MCP_MAX_DOWNLOAD_MB``.
* In HTTP (multi-user) mode these tools switch to resource/blob mode and uploads
  are refused with an explanatory error — check ``ctx.settings``.

:func:`upload_uncontainered_attachment` is exported for
``create_work_package(attachment_paths=…)``: files are uploaded uncontainered
and claimed through ``_links.attachments`` on create, because posting to
``…/{id}/attachments`` needs *edit* permission a fresh author may not have
(SPEC §7.2).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent
from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    AttachmentQuarantinedError,
    AttachmentTooLargeError,
    AuthenticationError,
    InputValidationError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    UnexpectedResponseError,
    ValidationFailedError,
)
from openproject_mcp.config import Settings
from openproject_mcp.projections import ListEnvelope, Ref
from openproject_mcp.tools._shared import (
    DESTRUCTIVE,
    GROUP_ATTACHMENTS,
    READ,
    WRITE,
    ToolContext,
    build_envelope,
    destructive_annotations,
    envelope_from_collection,
    get_configuration,
    get_tool_context,
    read_annotations,
    report_progress,
    require_confirmation,
    tool_errors,
    tool_tags,
    write_annotations,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "AttachmentDeletionResult",
    "AttachmentRow",
    "DownloadResult",
    "FileLinkRow",
    "register",
    "upload_uncontainered_attachment",
]

#: Container kind → API collection. ``comment`` is an activity: OpenProject
#: mounts comment attachments under ``/activities/{id}/attachments``.
CONTAINER_PATHS: dict[str, str] = {
    "work_package": "work_packages",
    "wiki_page": "wiki_pages",
    "meeting": "meetings",
    "document": "documents",
    "budget": "budgets",
    "comment": "activities",
}

ContainerType = Literal["work_package", "wiki_page", "meeting", "document", "budget", "comment"]

#: ``status`` value of an attachment the virus scanner rejected.
QUARANTINED_STATUS = "quarantined"
#: ``status`` values that mean the bytes are readable by everyone who may see
#: the container; anything else is mid-scan and 401s for non-uploaders.
READABLE_STATUSES = frozenset({"uploaded", "scanned", "rescanned"})

DOWNLOAD_CHUNK_BYTES = 64 * 1024
#: Wall-clock cap for one download (SPEC §7.1); streamed bodies have no read
#: timeout, so this is what stops a stalled transfer from hanging the call.
DOWNLOAD_DEADLINE_SECONDS = 900.0
#: Largest image returned as an inline MCP image block (SPEC §7.1 step 4).
IMAGE_INLINE_MAX_BYTES = 1024 * 1024
DEFAULT_DOWNLOAD_DIRNAME = "openproject-downloads"
MAX_FILE_NAME_CHARS = 200

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# --- output models ---------------------------------------------------------


class AttachmentRow(BaseModel):
    """One attached file."""

    id: int | str | None = Field(
        default=None, description="Attachment id; pass it to download_attachment."
    )
    file_name: str | None = Field(default=None, description="Stored file name including extension.")
    size_bytes: int | None = Field(default=None, description="File size in bytes.")
    content_type: str | None = Field(
        default=None, description="MIME type detected by OpenProject from the bytes."
    )
    description: str | None = Field(default=None, description="Caption stored with the file.")
    author: Ref | None = Field(default=None, description="User who uploaded the file.")
    created_at: str | None = Field(default=None, description="ISO 8601 UTC upload timestamp.")
    status: str | None = Field(
        default=None,
        description=(
            "Virus-scan state: 'uploaded'/'scanned' are downloadable, 'quarantined' is not, "
            "anything else is still being scanned."
        ),
    )


class AttachmentDeletionResult(BaseModel):
    """Outcome of ``delete_attachment``."""

    id: int = Field(description="Id of the attachment that was deleted.")
    deleted: bool = Field(description="True once OpenProject accepted the deletion.")
    file_name: str | None = Field(
        default=None, description="Name of the file that was removed, read before deleting it."
    )
    container: Ref | None = Field(
        default=None,
        description="Object the file was attached to (work package, wiki page, meeting, ...); "
        "null for an uploaded file that was never claimed by one.",
    )
    message: str = Field(description="Human-readable confirmation.")


class DownloadResult(BaseModel):
    """Where the bytes landed and what they were."""

    path: str = Field(description="Absolute path of the saved file on the MCP server's machine.")
    file_name: str = Field(
        description="Name the file was saved under; may differ from the attachment's own name "
        "when it collided with an existing file."
    )
    size_bytes: int = Field(description="Bytes actually written to disk.")
    content_type: str | None = Field(default=None, description="MIME type reported by OpenProject.")
    sha256: str = Field(description="SHA-256 of the downloaded bytes, hex encoded.")
    notes: list[str] | None = Field(
        default=None,
        description="Degradation markers: renamed target, image not shown inline, …",
    )


# --- small helpers ---------------------------------------------------------


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _attachment_row(payload: Mapping[str, Any]) -> AttachmentRow:
    return AttachmentRow(
        id=hal.self_id(payload),
        file_name=_str_or_none(payload.get("fileName")),
        size_bytes=_int_or_none(payload.get("fileSize")),
        content_type=_str_or_none(payload.get("contentType")),
        description=hal.formattable(payload.get("description")),
        author=Ref.from_hal(payload, "author"),
        created_at=_str_or_none(payload.get("createdAt")),
        status=_str_or_none(payload.get("status")),
    )


def _container_path(container_type: str, container_id: int | str) -> str:
    collection = CONTAINER_PATHS.get(container_type)
    if collection is None:
        raise InputValidationError(
            f"Unknown container_type {container_type!r}.",
            hint=f"Valid container types: {', '.join(sorted(CONTAINER_PATHS))}.",
        )
    return f"{collection}/{container_id}/attachments"


def _sanitize_file_name(raw: str | None, *, fallback: str) -> str:
    """Reduce a server-supplied file name to a safe single path segment.

    Directory separators, traversal and control characters are the attack
    surface here — an attachment named ``../../.ssh/authorized_keys`` must land
    in the download directory as ``authorized_keys`` (SPEC §7.1, §11).
    """
    candidate = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    candidate = _CONTROL_CHARS.sub("", candidate).strip()
    # Trailing dots and spaces are stripped by Windows and hide extensions.
    candidate = candidate.rstrip(" .")
    if not candidate or set(candidate) <= {"."}:
        return fallback
    if len(candidate) > MAX_FILE_NAME_CHARS:
        suffix = Path(candidate).suffix[:20]
        candidate = candidate[: MAX_FILE_NAME_CHARS - len(suffix)] + suffix
    return candidate


def _unique_path(directory: Path, file_name: str) -> Path:
    """``report.pdf`` → ``report (2).pdf`` when the name is already taken."""
    target = directory / file_name
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _resolve_download_dir(save_dir: str | None, settings: Settings) -> Path:
    """Pick and create the target directory (SPEC §7.1 step 3)."""
    if save_dir:
        directory = Path(save_dir).expanduser()
        if not directory.is_absolute():
            raise InputValidationError(
                f"save_dir must be an absolute path (got {save_dir!r}).",
                hint=(
                    "The MCP server has its own working directory, so a relative path is "
                    "ambiguous. Pass a full path, or omit save_dir to use the configured "
                    "download directory."
                ),
            )
    elif settings.download_dir is not None:
        directory = Path(settings.download_dir).expanduser()
    else:
        directory = Path.cwd() / DEFAULT_DOWNLOAD_DIRNAME

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InputValidationError(
            f"Could not create the download directory {directory}: {exc.strerror}.",
            hint="Pass a save_dir the server process may write to.",
        ) from exc
    if not directory.is_dir():
        raise InputValidationError(
            f"The download target {directory} is not a directory.",
            hint="Pass a save_dir that is a directory, not a file.",
        )
    return directory


def _pending_scan_error(
    attachment_id: int, file_name: str, status: str | None, exc: AuthenticationError
) -> AuthenticationError:
    """Turn the bare 401 on ``/content`` into an answerable explanation."""
    if status is not None and status not in READABLE_STATUSES:
        hint = (
            f"The antivirus scan of this attachment has not finished (status {status!r}). "
            "Until it does, only the user who uploaded the file may download it. Wait and "
            "retry, or ask the uploader for the file."
        )
    else:
        hint = (
            "OpenProject refused the download. This is either a credential problem (check "
            "OPENPROJECT_API_KEY) or an attachment whose virus scan is still running, which "
            "only its uploader may fetch until the scan completes."
        )
    return AuthenticationError(
        f"Downloading attachment {attachment_id} ({file_name}) was refused with HTTP 401.",
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        hint=hint,
    )


# --- upload plumbing (shared by the tool and the uncontainered helper) ------


def _maximum_attachment_bytes(configuration: Mapping[str, Any]) -> int | None:
    """``maximumAttachmentFileSize`` in bytes, or ``None`` when unreported."""
    raw = configuration.get("maximumAttachmentFileSize")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def _resolve_upload_file(file_path: str) -> Path:
    source = Path(file_path).expanduser()
    if not source.exists():
        raise InputValidationError(
            f"No such file: {source}.",
            hint=(
                "Pass an absolute path to a file on the machine running this MCP server. "
                "Nothing was uploaded."
            ),
        )
    if not source.is_file():
        raise InputValidationError(
            f"{source} is not a regular file.",
            hint="Attachments are single files; archive a directory before uploading it.",
        )
    return source


def _allowlist_error(exc: ValidationFailedError, file_name: str) -> ValidationFailedError:
    """Re-raise a 422 with the attachment-specific allowlist hint (SPEC §7.2)."""
    return ValidationFailedError(
        exc.message,
        http_status=exc.http_status,
        error_identifier=exc.error_identifier,
        violations=exc.violations,
        hint=(
            f"OpenProject rejected {file_name!r}. If the message mentions the file type or an "
            "allowlist, this instance restricts uploads through the 'attachment_whitelist' "
            "setting (Administration → Files): ask an administrator to allow the extension, or "
            "convert the file. Nothing was stored."
        ),
    )


async def _post_attachment(
    ctx: ToolContext,
    path: str,
    *,
    file_path: str,
    file_name: str | None,
    description: str | None,
) -> dict[str, Any]:
    """Pre-flight and POST one multipart attachment (SPEC §7.2).

    The body carries exactly two parts: ``metadata`` (JSON) and ``file``. The
    bytes are read into memory deliberately — the upload is already capped by
    ``maximumAttachmentFileSize`` and a materialized body keeps the request
    replay-free and inspectable.
    """
    source = _resolve_upload_file(file_path)
    size = source.stat().st_size
    limit = _maximum_attachment_bytes(await get_configuration(ctx))
    if limit is not None and size > limit:
        raise AttachmentTooLargeError(
            f"{source.name} is {size} bytes; this instance accepts at most {limit} bytes.",
            hint=(
                "Nothing was uploaded. Compress or split the file, or ask an administrator to "
                "raise the maximum attachment size. get_instance_info reports the current limit."
            ),
        )

    name = _sanitize_file_name(file_name or source.name, fallback="upload")
    metadata: dict[str, Any] = {"fileName": name}
    if description:
        # The upload representer reads the description as a formattable; sending
        # only ``raw`` lets OpenProject keep its own default format.
        metadata["description"] = {"raw": description}

    payload = source.read_bytes()
    files = {
        "metadata": (None, json.dumps(metadata).encode("utf-8"), "application/json"),
        "file": (name, payload, "application/octet-stream"),
    }
    try:
        return await ctx.client.post_json(path, files=files)
    except ValidationFailedError as exc:
        raise _allowlist_error(exc, name) from exc


async def upload_uncontainered_attachment(
    ctx: ToolContext,
    file_path: str,
    file_name: str | None = None,
    description: str | None = None,
) -> int:
    """Upload a file with no container yet and return its attachment id.

    This is the API-correct path for attaching files to a work package that
    does not exist yet (SPEC §7.2): ``create_work_package`` uploads here and
    claims the ids through ``_links.attachments`` in the create payload, because
    posting to ``work_packages/{id}/attachments`` requires *edit* permission
    that the author of a brand-new work package may not hold. Unclaimed uploads
    are purged by OpenProject after roughly 180 minutes, so a failed create
    leaves no junk behind.

    Not an MCP tool — it is imported by the work-package tools.

    Args:
        ctx: the calling tool's :class:`ToolContext`.
        file_path: path to a readable file on the server's machine.
        file_name: name to store the file under; defaults to the path's basename.
        description: optional caption stored with the attachment.

    Returns:
        The new attachment's numeric id.

    Raises:
        InputValidationError: the file is missing or is not a regular file.
        AttachmentTooLargeError: the file exceeds ``maximumAttachmentFileSize``.
        OpenProjectError: any upstream failure, unwrapped for the caller's own
            ``@tool_errors`` decorator to shape.
    """
    payload = await _post_attachment(
        ctx,
        "attachments",
        file_path=file_path,
        file_name=file_name,
        description=description,
    )
    attachment_id = hal.self_id(payload)
    if not isinstance(attachment_id, int):
        raise UnexpectedResponseError(
            "OpenProject accepted the upload but returned no attachment id.",
            hint=(
                "The file may still have been stored; check the work package's attachments "
                "before uploading it again."
            ),
        )
    return attachment_id


# --- file links (SPEC §6.4, §18 — storages module Ⓜ) -----------------------

FILE_LINKS_MISSING_NOTE = (
    "No file links could be read (404): either work package {id} does not exist, or the "
    "storages module is not installed/enabled here — remote file links need an administrator to "
    "connect a Nextcloud or OneDrive storage and enable it in the project. This is NOT proof "
    "that the work package has no linked files."
)
FILE_LINKS_FORBIDDEN_NOTE = (
    "No file links could be read (403): this account may not view the file links of work "
    "package {id}. The storage is connected, the permission is missing — ask for the 'view file "
    "links' permission. This is NOT proof that the work package has no linked files."
)
FILE_LINKS_EMPTY_NOTE = (
    "The endpoint answered with no rows. That usually means nothing is linked, but it is not "
    "proof: an account without the 'view file links' permission gets the same empty answer with "
    "the same 200, so absence here cannot be reported as certainty. Files uploaded straight into "
    "OpenProject are attachments, not file links — list them with list_attachments."
)


class FileLinkRow(BaseModel):
    """One file on an external storage linked to a work package."""

    id: int | str | None = Field(
        default=None, description="File-link id inside OpenProject; not the file's own id."
    )
    file_name: str | None = Field(
        default=None, description="Name of the file as the storage reports it."
    )
    storage: Ref | None = Field(
        default=None,
        description="The external storage the file lives on ({id, name}), e.g. a Nextcloud "
        "instance.",
    )
    origin_id: str | None = Field(
        default=None,
        description="The file's id INSIDE that storage (Nextcloud/OneDrive), not an OpenProject "
        "id.",
    )
    mime_type: str | None = Field(
        default=None, description="MIME type reported by the storage, when it reports one."
    )
    open_url: str | None = Field(
        default=None,
        description="Absolute OpenProject URL that opens the file: it redirects to the storage "
        "after resolving the link server-side. Give it to the user — it needs their own "
        "OpenProject login, and this server cannot fetch the bytes.",
    )
    download_url: str | None = Field(
        default=None,
        description="Absolute OpenProject URL that redirects to the file's download on the "
        "storage. Also an OpenProject endpoint needing the user's login, so "
        "download_attachment cannot use it.",
    )
    permission: str | None = Field(
        default=None,
        description="What the storage said about this account's access to the file: 'View "
        "allowed', 'View not allowed', 'Not found' or 'Error'. Null means the storage did not "
        "report a status, not that access is fine.",
    )
    creator: Ref | None = Field(
        default=None, description="OpenProject user who linked the file to the work package."
    )
    created_at: str | None = Field(
        default=None, description="ISO 8601 UTC time the link was created."
    )


def _link_href(payload: Mapping[str, Any], key: str) -> str | None:
    """A raw href from ``_links`` — the exception to 'hrefs stop at hal.py'.

    ``staticOriginOpen``/``staticOriginDownload`` are endpoints to hand to a
    person rather than resources to parse an id out of, so the href *is* the
    useful value.
    """
    resolved = hal.ref(payload, key)
    return resolved.href if resolved is not None else None


def _absolute_url(base_url: str | None, href: str | None) -> str | None:
    """Make a file-link href clickable.

    ``staticOriginOpen``/``staticOriginDownload`` render as instance-relative API
    paths (``/api/v3/file_links/601/open``). Those are OpenProject endpoints that
    resolve the storage URL and 303 to it, so the value only becomes usable once
    it carries the configured instance URL.
    """
    if href is None or href.startswith(("http://", "https://")):
        return href
    if not base_url:
        return href
    return f"{base_url.rstrip('/')}/{href.lstrip('/')}"


def _origin_value(raw: Any) -> str | None:
    """A value from ``originData``: storages send ids as strings, some as ints."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return str(raw)
    return raw if isinstance(raw, str) else None


def _file_link_row(payload: Mapping[str, Any], base_url: str | None) -> FileLinkRow:
    origin = hal.as_object(payload.get("originData"))
    # The access state rides on the ``status`` link, whose title is the storage's
    # own wording ("View allowed", "View not allowed", "Not found", "Error"). The
    # link is omitted entirely when the storage reported nothing, and that stays
    # null rather than being read as permission granted.
    status = hal.ref(payload, "status")
    return FileLinkRow(
        id=hal.self_id(payload),
        file_name=_origin_value(origin.get("name")) if origin is not None else None,
        storage=Ref.from_hal(payload, "storage"),
        origin_id=_origin_value(origin.get("id")) if origin is not None else None,
        mime_type=_origin_value(origin.get("mimeType")) if origin is not None else None,
        open_url=_absolute_url(base_url, _link_href(payload, "staticOriginOpen")),
        download_url=_absolute_url(base_url, _link_href(payload, "staticOriginDownload")),
        permission=status.name if status is not None else None,
        creator=Ref.from_hal(payload, "creator"),
        created_at=_str_or_none(payload.get("createdAt")),
    )


# --- registration ----------------------------------------------------------


def register(mcp: FastMCP) -> None:
    """Register the Phase 1 attachment tools."""

    @mcp.tool(
        name="list_attachments",
        tags=tool_tags(GROUP_ATTACHMENTS, READ),
        annotations=read_annotations(title="List attachments"),
    )
    @tool_errors
    async def list_attachments(
        container_type: Annotated[
            ContainerType,
            Field(
                description=(
                    "Kind of object that owns the files. Use 'comment' for files attached to a "
                    "work-package comment — those live on the activity, so pass the activity id "
                    "from list_work_package_comments as container_id."
                )
            ),
        ],
        container_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric id of the container itself: the work package id, wiki page id, "
                    "meeting id, document id, budget id, or activity id. Never an attachment id."
                )
            ),
        ],
    ) -> ListEnvelope[AttachmentRow]:
        """List the files attached to one container.

        Containers are work packages, wiki pages, meetings, documents, budgets and comments. Use
        this to discover attachment ids before calling download_attachment, or to check what a
        work package already carries. The upstream collection is not paginated, so it is fetched
        in full: the envelope always reports has_more=false and a total equal to the row count.

        Returns the standard list envelope; each row has id, file_name, size_bytes, content_type,
        description, author, created_at and status. status is the virus-scan state — 'uploaded'
        and 'scanned' are downloadable, 'quarantined' files are not, and anything else is still
        being scanned and is readable only by its uploader.

        Pitfalls: container_id identifies the container, not the file. A 404 means the container
        does not exist or the module providing it (meetings, budgets, documents) is not enabled on
        this instance. Forum posts are a valid API container but have no discovery path here.

        Related: download_attachment fetches the bytes for one row, upload_attachment adds a file
        to the same containers, and get_work_package(include=['attachments']) returns these rows
        inline for a single work package.
        """
        ctx = get_tool_context()
        payload = await ctx.client.get_json(_container_path(container_type, container_id))
        unwrapped = hal.collection(payload)
        rows = [_attachment_row(element) for element in unwrapped]
        return envelope_from_collection(unwrapped, rows, page=1, page_size=max(len(rows), 1))

    @mcp.tool(
        name="download_attachment",
        tags=tool_tags(GROUP_ATTACHMENTS, READ),
        annotations=read_annotations(title="Download attachment", idempotent=False),
    )
    @tool_errors
    async def download_attachment(
        attachment_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric attachment id from list_attachments or "
                    "get_work_package(include=['attachments']). Not a work package id."
                )
            ),
        ],
        save_dir: Annotated[
            str | None,
            Field(
                description=(
                    "Absolute directory to save into; it is created when missing. Defaults to "
                    "OPENPROJECT_MCP_DOWNLOAD_DIR, and otherwise to an 'openproject-downloads' "
                    "folder beside the server's working directory. Relative paths are rejected "
                    "because the server's working directory is not the user's."
                )
            ),
        ] = None,
        return_image: Annotated[
            bool,
            Field(
                description=(
                    "Also return the file as an inline image so the model can look at it. "
                    "Honored only for image/* content of at most 1 MB; otherwise the file is "
                    "still saved and a note explains why nothing was shown."
                )
            ),
        ] = False,
    ) -> DownloadResult:
        """Download an attachment's bytes to a file on the machine running this server.

        Use it once list_attachments (or get_work_package(include=['attachments'])) has given you
        an attachment_id. Metadata is read first, then the bytes are streamed to disk in chunks
        with progress notifications, so a large file neither stalls the call nor buffers in memory.

        Returns path, file_name, size_bytes, content_type and the SHA-256 of the bytes (use it to
        verify or de-duplicate). With return_image=true an image of at most 1 MB comes back as an
        inline image block as well.

        Pitfalls: the file is written on the server's machine, which is the user's machine only in
        a local (stdio) deployment — tell the user the returned path rather than assuming they can
        see it. Quarantined attachments fail with attachment_quarantined and are never fetched. An
        attachment whose virus scan is unfinished answers 401 for everyone except its uploader.
        Transfers above OPENPROJECT_MCP_MAX_DOWNLOAD_MB (default 100) are refused up front and
        aborted mid-stream, leaving no partial file. A name collision in the target directory saves
        as 'name (2).ext' and says so in notes.

        Related: list_attachments produces attachment_id; upload_attachment is the reverse
        direction.
        """
        ctx = get_tool_context()
        metadata = await ctx.client.get_json(f"attachments/{attachment_id}")

        status = _str_or_none(metadata.get("status"))
        content_type = _str_or_none(metadata.get("contentType"))
        declared_size = _int_or_none(metadata.get("fileSize"))
        file_name = _sanitize_file_name(
            _str_or_none(metadata.get("fileName")), fallback=f"attachment-{attachment_id}"
        )

        if status == QUARANTINED_STATUS:
            raise AttachmentQuarantinedError(
                f"Attachment {attachment_id} ({file_name}) is quarantined by the virus scanner.",
                hint=(
                    "OpenProject will not serve the bytes of a quarantined file and neither will "
                    "this tool. An administrator must review and release it before it can be "
                    "downloaded."
                ),
                extra={"attachment_id": attachment_id, "file_name": file_name},
            )

        cap = ctx.settings.max_download_bytes
        if declared_size is not None and declared_size > cap:
            raise AttachmentTooLargeError(
                f"Attachment {attachment_id} ({file_name}) is {declared_size} bytes, above the "
                f"{cap} byte download limit.",
                hint=(
                    "Nothing was downloaded. Raise OPENPROJECT_MCP_MAX_DOWNLOAD_MB (currently "
                    f"{ctx.settings.max_download_mb}) if this file really is wanted, or fetch it "
                    "from the OpenProject web UI."
                ),
                extra={"attachment_id": attachment_id, "size_bytes": declared_size},
            )

        directory = _resolve_download_dir(save_dir, ctx.settings)
        target = _unique_path(directory, file_name)

        digest = hashlib.sha256()
        written = 0
        deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
        try:
            async with ctx.client.stream("GET", f"attachments/{attachment_id}/content") as response:
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                        written += len(chunk)
                        if written > cap:
                            raise AttachmentTooLargeError(
                                f"Attachment {attachment_id} ({file_name}) exceeded the {cap} "
                                "byte download limit while streaming.",
                                hint=(
                                    "The transfer was aborted and the partial file removed. The "
                                    "attachment's reported size was smaller than what it actually "
                                    "sent; raise OPENPROJECT_MCP_MAX_DOWNLOAD_MB to fetch it."
                                ),
                            )
                        if time.monotonic() > deadline:
                            raise NetworkError(
                                f"Downloading attachment {attachment_id} ({file_name}) exceeded "
                                f"the {DOWNLOAD_DEADLINE_SECONDS:.0f}s limit after {written} "
                                "bytes.",
                                hint=(
                                    "The connection stalled; the partial file was removed. Retry, "
                                    "or download the file from the OpenProject web UI."
                                ),
                            )
                        handle.write(chunk)
                        digest.update(chunk)
                        await report_progress(
                            written,
                            declared_size if declared_size else None,
                            f"{file_name}: {written} bytes",
                        )
        except AuthenticationError as exc:
            target.unlink(missing_ok=True)
            raise _pending_scan_error(attachment_id, file_name, status, exc) from exc
        except BaseException:
            target.unlink(missing_ok=True)
            raise

        notes: list[str] = []
        if target.name != file_name:
            notes.append(
                f"{file_name} already existed in {directory}; saved as {target.name} instead."
            )
        inline_image = None
        if return_image:
            if content_type is None or not content_type.startswith("image/"):
                notes.append(
                    f"return_image was ignored: the content type is {content_type or 'unknown'}, "
                    "not an image."
                )
            elif written > IMAGE_INLINE_MAX_BYTES:
                notes.append(
                    f"return_image was ignored: {written} bytes exceeds the "
                    f"{IMAGE_INLINE_MAX_BYTES} byte inline limit. The file is saved at {target}."
                )
            else:
                inline_image = Image(data=target.read_bytes()).to_image_content(
                    mime_type=content_type
                )

        result = DownloadResult(
            path=str(target),
            file_name=target.name,
            size_bytes=written,
            content_type=content_type,
            sha256=digest.hexdigest(),
            notes=notes or None,
        )
        if inline_image is None:
            return result

        # FastMCP passes a ToolResult straight through (Tool.convert_result), so
        # the declared outputSchema still validates against structured_content
        # while the extra image block rides along in content.
        return cast(
            "DownloadResult",
            ToolResult(
                content=[
                    TextContent(type="text", text=result.model_dump_json(exclude_none=True)),
                    inline_image,
                ],
                structured_content=result.model_dump(mode="json"),
            ),
        )

    @mcp.tool(
        name="upload_attachment",
        tags=tool_tags(GROUP_ATTACHMENTS, WRITE),
        annotations=write_annotations(title="Upload attachment"),
    )
    @tool_errors
    async def upload_attachment(
        container_type: Annotated[
            ContainerType,
            Field(
                description=(
                    "Kind of object to attach the file to. Use 'comment' to attach to a "
                    "work-package comment and pass its activity id as container_id."
                )
            ),
        ],
        container_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric id of the container: work package id, wiki page id, meeting id, "
                    "document id, budget id, or activity id for 'comment'."
                )
            ),
        ],
        file_path: Annotated[
            str,
            Field(
                description=(
                    "Absolute path of the file to upload, on the machine running this server. "
                    "Existence and size are checked locally before anything is transferred."
                )
            ),
        ],
        file_name: Annotated[
            str | None,
            Field(
                description=(
                    "Name to store the file under, with extension. This is the only thing that "
                    "decides the stored name: OpenProject ignores the multipart filename and "
                    "re-detects the content type from the bytes. Defaults to the basename of "
                    "file_path."
                )
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Optional caption shown next to the file in OpenProject."),
        ] = None,
    ) -> AttachmentRow:
        """Attach a local file to a work package, wiki page, meeting, document, budget or comment.

        Use it when a file that already exists on the server's machine should be added to an
        existing container. The file's existence and its size against this instance's
        maximumAttachmentFileSize are checked locally first, so an oversized file fails instantly
        instead of after the transfer.

        Returns the created attachment row (id, file_name, size_bytes, content_type, description,
        author, created_at, status) — the id feeds download_attachment.

        Pitfalls: uploading to a container needs *edit* permission on that container, so to give a
        brand-new work package its files use create_work_package(attachment_paths=[...]) instead,
        which uploads the files unattached and claims them on create. Instances may restrict
        extensions; a rejected type comes back as validation_failed with the allowlist hint and
        nothing is stored. The stored name comes from file_name (or the path's basename), never
        from the multipart part.

        Related: list_attachments shows what a container already holds; download_attachment is the
        reverse direction.
        """
        ctx = get_tool_context()
        payload = await _post_attachment(
            ctx,
            _container_path(container_type, container_id),
            file_path=file_path,
            file_name=file_name,
            description=description,
        )
        return _attachment_row(payload)

    @mcp.tool(
        name="delete_attachment",
        tags=tool_tags(GROUP_ATTACHMENTS, WRITE, DESTRUCTIVE),
        annotations=destructive_annotations(title="Delete attachment"),
    )
    @tool_errors
    async def delete_attachment(
        attachment_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric attachment id from list_attachments or "
                    "get_work_package(include=['attachments']). Never a work package or container "
                    "id — deleting the wrong id cannot be undone."
                )
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be true. Ask the user to confirm first: OpenProject offers no undo and "
                    "no trash. Calling with confirm=false returns a confirmation_required error "
                    "and nothing is read or deleted."
                )
            ),
        ] = False,
    ) -> AttachmentDeletionResult:
        """Permanently delete one attached file from OpenProject.

        Use it only on explicit user instruction, for example to remove a file uploaded to the
        wrong work package or a superseded document. The attachment's metadata is read first so
        the result names the file and the container it was attached to, and so an unknown id fails
        before anything is removed.

        Returns the attachment id, the file name, its container and a confirmation message.

        Pitfalls: this deletes the file itself, not a link to it — every work package, wiki page
        or comment that embedded it loses the image or download. Deleting needs *edit* permission
        on the container (or authorship for a file that has no container yet), so a 403 can follow
        a successful read. A 404 means the id is unknown or already deleted; a second call on the
        same id answers 404 rather than succeeding. Removing a file does not remove the comment or
        work package that referenced it.

        Related: list_attachments shows the ids and file names of everything a container holds;
        upload_attachment adds a replacement; download_attachment saves a copy first if the bytes
        are still wanted.
        """
        require_confirmation(
            confirm,
            action="delete attachment",
            target=f"#{attachment_id}",
            consequence=(
                "The file is removed from OpenProject permanently and every work package, wiki "
                "page or comment that embedded it loses it."
            ),
        )
        ctx = get_tool_context()
        metadata = await ctx.client.get_json(f"attachments/{attachment_id}")
        row = _attachment_row(metadata)
        container = Ref.from_hal(metadata, "container")

        await ctx.client.delete(f"attachments/{attachment_id}")

        where = f" from {container.name}" if container and container.name else ""
        return AttachmentDeletionResult(
            id=attachment_id,
            deleted=True,
            file_name=row.file_name,
            container=container,
            message=f"Attachment {row.file_name or attachment_id}{where} was deleted permanently.",
        )

    @mcp.tool(
        name="list_file_links",
        tags=tool_tags(GROUP_ATTACHMENTS, READ),
        annotations=read_annotations(title="List file links"),
    )
    @tool_errors
    async def list_file_links(
        work_package_id: Annotated[
            int,
            Field(
                description=(
                    "Numeric work package id whose linked storage files to list. It comes from "
                    "search_work_packages, list_work_packages or get_work_package — never an "
                    "attachment id and never a project id."
                )
            ),
        ],
    ) -> ListEnvelope[FileLinkRow]:
        """List the external-storage files (Nextcloud, OneDrive/SharePoint) linked to a work
        package.

        File links are OpenProject's other kind of file: instead of living inside OpenProject
        like an attachment, the document stays in a connected storage and the work package
        points at it. Use this to answer "which documents belong to this ticket" — and pair it
        with list_attachments, because the two lists are disjoint and neither implies the other.

        Returns the standard list envelope, fetched in full (has_more is always false). Each
        row carries file_name, the storage it lives on, the file's origin_id inside that
        storage, mime_type, the creator and — the useful part — open_url and download_url.
        Those are absolute OpenProject URLs that redirect to the storage once OpenProject has
        resolved the link, so hand them to the user: they need the user's own OpenProject
        login, this server cannot fetch the bytes, and download_attachment does not work on
        them.

        Pitfalls: this needs the storages module and a storage connected to the project. When
        it is missing (404) or this account may not read the links (403) the call still
        succeeds with an EMPTY list and a note explaining which — read notes before saying a
        ticket has no documents. An empty list is never proof either: an account lacking the
        'view file links' permission gets an empty 200 rather than a 403, which is exactly what
        that note says. permission carries the storage's own wording — 'View allowed' means the
        URLs will work, 'View not allowed', 'Not found' and 'Error' mean they will not, and
        null means the storage said nothing. Creating and deleting file links, and browsing the
        remote storage, are out of scope for this server — do them in the OpenProject UI.

        Related: list_attachments covers files stored inside OpenProject, download_attachment
        fetches those bytes, and get_work_package gives the ticket the links belong to.
        """
        ctx = get_tool_context()
        try:
            payload = await ctx.client.get_json(f"work_packages/{work_package_id}/file_links")
        except NotFoundError:
            # Ⓜ module absent — a note, never an empty answer that reads as "none" (G5).
            return build_envelope(
                [],
                total=0,
                page=1,
                page_size=1,
                notes=[FILE_LINKS_MISSING_NOTE.format(id=work_package_id)],
            )
        except PermissionDeniedError:
            return build_envelope(
                [],
                total=0,
                page=1,
                page_size=1,
                notes=[FILE_LINKS_FORBIDDEN_NOTE.format(id=work_package_id)],
            )

        unwrapped = hal.collection(payload)
        rows = [_file_link_row(element, ctx.settings.url) for element in unwrapped]
        notes = [FILE_LINKS_EMPTY_NOTE] if not rows else None
        return envelope_from_collection(
            unwrapped, rows, page=1, page_size=max(len(rows), 1), notes=notes
        )
