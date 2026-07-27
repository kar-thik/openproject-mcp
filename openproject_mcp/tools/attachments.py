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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]


def register(mcp: FastMCP) -> None:
    """Register the attachment tools. Phase 1 fills in list/download/upload."""
