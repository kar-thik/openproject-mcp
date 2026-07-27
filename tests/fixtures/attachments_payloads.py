"""Golden attachment payloads, trimmed from OpenProject 17.x responses.

Kept as Python literals so the suite stays offline and diffable (SPEC §13.3).
``PNG_1X1`` is a real, minimal PNG so the inline-image path is exercised with
bytes a decoder would accept rather than a placeholder string.
"""

from __future__ import annotations

import base64
from typing import Any

ATTACHMENT: dict[str, Any] = {
    "_type": "Attachment",
    "id": 42,
    "fileName": "spec.pdf",
    "fileSize": 12,
    "contentType": "application/pdf",
    "description": {"format": "plain", "raw": "The signed spec", "html": "<p>The signed spec</p>"},
    "digest": {"algorithm": "md5", "hash": "0a1b2c3d"},
    "createdAt": "2026-07-20T09:15:00Z",
    "status": "uploaded",
    "_links": {
        "self": {"href": "/api/v3/attachments/42"},
        "author": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        "container": {"href": "/api/v3/work_packages/1234", "title": "Ship the client layer"},
        "downloadLocation": {"href": "/api/v3/attachments/42/content"},
        "staticDownloadLocation": {"href": "/api/v3/attachments/42/content"},
    },
}

ATTACHMENT_IMAGE: dict[str, Any] = {
    "_type": "Attachment",
    "id": 43,
    "fileName": "screenshot.png",
    "fileSize": 70,
    "contentType": "image/png",
    "description": {"format": "plain", "raw": ""},
    "createdAt": "2026-07-21T11:00:00Z",
    "status": "scanned",
    "_links": {
        "self": {"href": "/api/v3/attachments/43"},
        "author": {"href": "/api/v3/users/13", "title": "Alan Turing"},
        "container": {"href": "/api/v3/work_packages/1234"},
    },
}

ATTACHMENT_QUARANTINED: dict[str, Any] = {
    "_type": "Attachment",
    "id": 44,
    "fileName": "invoice.exe",
    "fileSize": 900,
    "contentType": "application/octet-stream",
    "createdAt": "2026-07-22T08:00:00Z",
    "status": "quarantined",
    "_links": {
        "self": {"href": "/api/v3/attachments/44"},
        "author": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
    },
}

ATTACHMENT_PENDING_SCAN: dict[str, Any] = {
    "_type": "Attachment",
    "id": 45,
    "fileName": "draft.docx",
    "fileSize": 500,
    "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "createdAt": "2026-07-23T08:00:00Z",
    "status": "prescan",
    "_links": {"self": {"href": "/api/v3/attachments/45"}},
}

ATTACHMENT_TRAVERSAL: dict[str, Any] = {
    "_type": "Attachment",
    "id": 46,
    "fileName": "../../etc/passwd",
    "fileSize": 5,
    "contentType": "text/plain",
    "createdAt": "2026-07-24T08:00:00Z",
    "status": "uploaded",
    "_links": {"self": {"href": "/api/v3/attachments/46"}},
}

ATTACHMENT_HUGE: dict[str, Any] = {
    "_type": "Attachment",
    "id": 47,
    "fileName": "backup.tar.gz",
    "fileSize": 512 * 1024 * 1024,
    "contentType": "application/gzip",
    "createdAt": "2026-07-25T08:00:00Z",
    "status": "uploaded",
    "_links": {"self": {"href": "/api/v3/attachments/47"}},
}

ATTACHMENT_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 2,
    "count": 2,
    "_links": {"self": {"href": "/api/v3/work_packages/1234/attachments"}},
    "_embedded": {"elements": [ATTACHMENT, ATTACHMENT_IMAGE]},
}

COMMENT_ATTACHMENT_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 1,
    "count": 1,
    "_links": {"self": {"href": "/api/v3/activities/9001/attachments"}},
    "_embedded": {"elements": [ATTACHMENT]},
}

EMPTY_ATTACHMENT_COLLECTION: dict[str, Any] = {
    "_type": "Collection",
    "total": 0,
    "count": 0,
    "_embedded": {"elements": []},
}

#: ``GET /configuration`` — maximumAttachmentFileSize is in bytes.
CONFIGURATION: dict[str, Any] = {
    "_type": "Configuration",
    "maximumAttachmentFileSize": 1024,
    "perPageOptions": [20, 100],
    "hostName": "openproject.test",
    "_links": {"self": {"href": "/api/v3/configuration"}},
}

#: 422 raised when the instance's attachment_whitelist rejects the file type.
ALLOWLIST_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "File is not of an allowed content type.",
    "_embedded": {"details": {"attribute": "file"}},
}

#: A real 1x1 transparent PNG (70 bytes) for the inline-image path.
PNG_1X1: bytes = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

PDF_BYTES: bytes = b"%PDF-1.7\n%%\n"
