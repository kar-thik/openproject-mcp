"""Golden HAL payloads for the Phase 3 project/query/file-link operations.

Trimmed from OpenProject 16.x/17.x responses and kept as Python literals so the
suite stays offline and diffable. Each shape is derived from the representer or
request spec that produces it upstream, not from what the tools happen to send:

* the copy form's ``_meta`` block carries instance defaults the tool must not
  drop;
* a finished copy job nests its result in ``payload._links.project`` next to the
  UI ``redirect`` (``CopyProjectJob#successful_status_update``), and the job
  document itself renders no project link at the root;
* a query's resource-valued filters live in ``_links.values`` as hrefs, the
  shape OpenProject's own create-query request spec sends;
* a file link's access state rides on the ``status`` link with the storage's
  wording, and its ``staticOrigin*`` hrefs are instance-relative API paths.
"""

from __future__ import annotations

from typing import Any

# --- project copy ---------------------------------------------------------

SOURCE_PROJECT_ID = 7
SOURCE_PROJECT_IDENTIFIER = "apollo-migration"
COPY_JOB_ID = "9f4c1d5e-0e2a-4f2b-9a11-2f1b3c4d5e6f"
COPY_NAME = "Apollo migration (2027)"
COPY_IDENTIFIER = "apollo-migration-2027"

#: The copy form echoes the payload it would commit: the identifier it derived
#: from the name, plus every ``_meta`` copy flag defaulted by the instance.
COPY_FORM: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {
            "name": COPY_NAME,
            "identifier": COPY_IDENTIFIER,
            "_meta": {
                "copyMembers": True,
                "copyVersions": True,
                "copyWiki": True,
                "copyWorkPackages": True,
                "sendNotifications": True,
            },
        },
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
    "_links": {"self": {"href": f"/api/v3/projects/{SOURCE_PROJECT_ID}/copy/form"}},
}

COPY_FORM_NAME_TAKEN: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"name": COPY_NAME, "identifier": COPY_IDENTIFIER},
        "schema": {"_type": "Schema"},
        "validationErrors": {
            "identifier": {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Identifier has already been taken.",
                "_embedded": {"details": {"attribute": "identifier"}},
            }
        },
    },
}

#: What ``POST /projects/{id}/copy`` answers: the queued background job.
COPY_JOB_ACCEPTED: dict[str, Any] = {
    "_type": "JobStatus",
    "status": "in_queue",
    "message": "Project copy scheduled.",
    "_links": {"self": {"href": f"/api/v3/job_statuses/{COPY_JOB_ID}"}},
}

#: Some instances answer the copy without naming a job at all.
COPY_ACCEPTED_WITHOUT_JOB: dict[str, Any] = {
    "_type": "JobStatus",
    "status": "in_queue",
    "message": "Project copy scheduled.",
}


# --- job statuses ---------------------------------------------------------

JOB_IN_PROCESS: dict[str, Any] = {
    "_type": "JobStatus",
    "status": "in_process",
    "message": "Copying work packages (140/900).",
    "_links": {"self": {"href": f"/api/v3/job_statuses/{COPY_JOB_ID}"}},
}

COPY_PROJECT_NUMERIC_ID = 9

#: A finished copy merges the redirect payload with the HAL links of the new
#: project, so both the UI URL and the authoritative link sit *inside* payload.
#: The JobStatus representer renders only self/jobId/status/message/payload —
#: there is no project link at the root.
JOB_SUCCESS: dict[str, Any] = {
    "_type": "JobStatus",
    "status": "success",
    "message": "Project copied successfully.",
    "payload": {
        "redirect": f"https://openproject.test/projects/{COPY_IDENTIFIER}",
        "_links": {
            "project": {
                "href": f"/api/v3/projects/{COPY_PROJECT_NUMERIC_ID}",
                "title": COPY_NAME,
            }
        },
    },
    "_links": {"self": {"href": f"/api/v3/job_statuses/{COPY_JOB_ID}"}},
}

#: …and a job that stored only the redirect (an export, or a copy from before
#: the links were merged in) leaves the project to be read out of the URL.
JOB_SUCCESS_URL_ONLY: dict[str, Any] = {
    "_type": "JobStatus",
    "status": "success",
    "message": "Project copied successfully.",
    "payload": {"redirect": f"https://openproject.test/projects/{COPY_IDENTIFIER}"},
    "_links": {"self": {"href": f"/api/v3/job_statuses/{COPY_JOB_ID}"}},
}

JOB_FAILURE: dict[str, Any] = {
    "_type": "JobStatus",
    "status": "failure",
    "message": "Copying failed: Identifier has already been taken.",
    "_links": {"self": {"href": f"/api/v3/job_statuses/{COPY_JOB_ID}"}},
}


# --- saved queries --------------------------------------------------------

QUERY_ID = 91
QUERY_NAME = "Overdue in Platform"

#: The form echoes the filters the way OpenProject renders them: object-backed
#: filters carry no plain ``values`` at all (the property is skipped for them),
#: only ``_links.values`` hrefs, and the ids point at the collection the value
#: objects belong to (``/api/v3/users/12`` for an assignee).
QUERY_FORM: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {
            "name": QUERY_NAME,
            "public": False,
            "filters": [
                {
                    "_type": "StatusQueryFilter",
                    "name": "Status",
                    "_links": {
                        "filter": {"href": "/api/v3/queries/filters/status"},
                        "operator": {"href": "/api/v3/queries/operators/o"},
                        "schema": {"href": "/api/v3/queries/filter_instance_schemas/status"},
                        "values": [],
                    },
                },
                {
                    "_type": "AssigneeQueryFilter",
                    "name": "Assignee",
                    "_links": {
                        "filter": {"href": "/api/v3/queries/filters/assignee"},
                        "operator": {"href": "/api/v3/queries/operators/%3D"},
                        "schema": {"href": "/api/v3/queries/filter_instance_schemas/assignee"},
                        "values": [{"href": "/api/v3/users/12"}],
                    },
                },
            ],
            "_links": {
                "project": {"href": "/api/v3/projects/5"},
                "groupBy": {"href": "/api/v3/queries/group_bys/status"},
                "sortBy": [{"href": "/api/v3/queries/sort_bys/dueDate-asc"}],
            },
        },
        "schema": {"_type": "Schema"},
        "validationErrors": {},
    },
}

QUERY_FORM_INVALID_FILTER: dict[str, Any] = {
    "_type": "Form",
    "_embedded": {
        "payload": {"name": QUERY_NAME},
        "schema": {"_type": "Schema"},
        "validationErrors": {
            "filters": {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
                "message": "Version filter does not exist.",
                "_embedded": {"details": {"attribute": "filters"}},
            }
        },
    },
}

STORED_QUERY_FILTERS: list[dict[str, Any]] = [
    {
        "_type": "StatusQueryFilter",
        "_links": {
            "filter": {"href": "/api/v3/queries/filters/status", "title": "Status"},
            "operator": {"href": "/api/v3/queries/operators/o", "title": "open"},
            "values": [],
        },
    },
    {
        "_type": "AssigneeQueryFilter",
        "_links": {
            "filter": {"href": "/api/v3/queries/filters/assignee", "title": "Assignee"},
            "operator": {"href": "/api/v3/queries/operators/%3D", "title": "is (OR)"},
            "values": [{"href": "/api/v3/users/12", "title": "Grace Hopper"}],
        },
    },
]

CREATED_QUERY: dict[str, Any] = {
    "_type": "Query",
    "id": QUERY_ID,
    "name": QUERY_NAME,
    "public": False,
    "starred": False,
    "sums": False,
    "filters": STORED_QUERY_FILTERS,
    "updatedAt": "2026-07-26T09:40:00Z",
    "_links": {
        "self": {"href": f"/api/v3/queries/{QUERY_ID}", "title": QUERY_NAME},
        "project": {"href": "/api/v3/projects/5", "title": "Platform"},
        "groupBy": {"href": "/api/v3/queries/group_bys/status", "title": "Status"},
        "sortBy": [{"href": "/api/v3/queries/sort_bys/dueDate-asc", "title": "Finish date asc"}],
    },
}

STARRED_QUERY: dict[str, Any] = {**CREATED_QUERY, "starred": True}

#: A query OpenProject stored with fewer filters than were sent.
CREATED_QUERY_ONE_FILTER: dict[str, Any] = {
    **CREATED_QUERY,
    "filters": STORED_QUERY_FILTERS[:1],
}

QUERY_STAR_FORBIDDEN: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:MissingPermission",
    "message": "You are not authorized to access this resource.",
}


# --- file links (storages module) -----------------------------------------

FILE_LINK_WORK_PACKAGE_ID = 1234

#: Two of the four states ``PERMISSION_LINKS`` can render, verbatim (the others
#: are "Not found" and "Error"). The link is named ``status``, not
#: ``permission``, and is omitted entirely when the storage reported nothing.
FILE_LINK_STATUS_VIEW_ALLOWED: dict[str, str] = {
    "href": "urn:openproject-org:api:v3:file-links:permission:ViewAllowed",
    "title": "View allowed",
}
FILE_LINK_STATUS_VIEW_NOT_ALLOWED: dict[str, str] = {
    "href": "urn:openproject-org:api:v3:file-links:permission:ViewNotAllowed",
    "title": "View not allowed",
}

FILE_LINK: dict[str, Any] = {
    "_type": "FileLink",
    "id": 601,
    "createdAt": "2026-07-20T08:15:00Z",
    "updatedAt": "2026-07-20T08:15:00Z",
    "originData": {
        "id": "5503",
        "name": "architecture-review.pdf",
        "mimeType": "application/pdf",
        "createdByName": "Grace Hopper",
    },
    "_links": {
        "self": {"href": "/api/v3/file_links/601"},
        "storage": {"href": "/api/v3/storages/3", "title": "Acme Nextcloud"},
        "container": {"href": f"/api/v3/work_packages/{FILE_LINK_WORK_PACKAGE_ID}"},
        "creator": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
        "status": FILE_LINK_STATUS_VIEW_ALLOWED,
        # OpenProject endpoints that 303 to the storage, not storage URLs, and
        # relative to the instance root.
        "staticOriginOpen": {"href": "/api/v3/file_links/601/open"},
        "staticOriginOpenLocation": {"href": "/api/v3/file_links/601/open?location=true"},
        "staticOriginDownload": {"href": "/api/v3/file_links/601/download"},
    },
}

#: A file this account may not open: the storage says so and the open endpoint
#: is still rendered, which is exactly the trap the row explains.
FILE_LINK_NO_PERMISSION: dict[str, Any] = {
    "_type": "FileLink",
    "id": 602,
    "createdAt": "2026-07-21T10:00:00Z",
    "originData": {"id": "5504", "name": "budget.xlsx"},
    "_links": {
        "self": {"href": "/api/v3/file_links/602"},
        "storage": {"href": "/api/v3/storages/3", "title": "Acme Nextcloud"},
        "status": FILE_LINK_STATUS_VIEW_NOT_ALLOWED,
        "staticOriginOpen": {"href": "/api/v3/file_links/602/open"},
    },
}

#: ``origin_status`` was nil, so no status link is rendered at all — null must
#: keep meaning "the storage did not say", never "access is fine".
FILE_LINK_UNKNOWN_STATUS: dict[str, Any] = {
    "_type": "FileLink",
    "id": 603,
    "createdAt": "2026-07-22T11:30:00Z",
    "originData": {"id": "5505", "name": "roadmap.md"},
    "_links": {
        "self": {"href": "/api/v3/file_links/603"},
        "storage": {"href": "/api/v3/storages/3", "title": "Acme Nextcloud"},
        "staticOriginOpen": {"href": "/api/v3/file_links/603/open"},
        "staticOriginDownload": {"href": "/api/v3/file_links/603/download"},
    },
}


def file_link_collection(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "_type": "Collection",
        "total": len(elements),
        "count": len(elements),
        "_embedded": {"elements": elements},
    }


# --- errors ---------------------------------------------------------------

NOT_FOUND_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:NotFound",
    "message": "The requested resource could not be found.",
}

FORBIDDEN_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:MissingPermission",
    "message": "You are not authorized to access this resource.",
}
