"""Golden payloads for the git-activity and saved-query tools (SPEC §6.5, §6.7, §8).

Trimmed from real OpenProject 16.x responses:

* ``GET /work_packages/{id}`` — only the ``_links`` that matter for availability
  detection (§8): the permission-gated ``revisions``/``github``/``gitlab`` tab
  links versus the ``*_pull_requests``/``*_merge_requests`` collection links,
  which the bundled plugins render unconditionally.
* ``GET /work_packages/{id}/revisions``, ``…/github_pull_requests``,
  ``…/gitlab_merge_requests``, ``…/gitlab_issues``, ``GET /github_pull_requests/{id}``.
* ``GET /queries`` and ``GET /queries/{id}`` (results embedded — queries run on read).
"""

from __future__ import annotations

from typing import Any

WORK_PACKAGE_ID = 1234
PULL_REQUEST_ID = 17
QUERY_ID = 15


def hal_collection(elements: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Wrap elements in the HAL collection envelope OpenProject returns."""
    return {
        "_type": "Collection",
        "total": extra.pop("total", len(elements)),
        "count": len(elements),
        "_embedded": {"elements": elements},
        **extra,
    }


# --- work packages: availability signals (SPEC §8) ------------------------

_SELF_LINKS: dict[str, Any] = {
    "self": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}", "title": "Ship the client layer"},
}

#: SCM + GitHub + GitLab all readable: every gated tab link is rendered.
WORK_PACKAGE_ALL_SOURCES: dict[str, Any] = {
    "_type": "WorkPackage",
    "id": WORK_PACKAGE_ID,
    "subject": "Ship the client layer",
    "_links": {
        **_SELF_LINKS,
        "revisions": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/revisions"},
        "github": {"href": f"/work_packages/{WORK_PACKAGE_ID}/tabs/github", "title": "GitHub"},
        "github_pull_requests": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/github_pull_requests"
        },
        "gitlab": {"href": f"/work_packages/{WORK_PACKAGE_ID}/tabs/gitlab", "title": "GitLab"},
        "gitlab_merge_requests": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/gitlab_merge_requests"
        },
        "gitlab_issues": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/gitlab_issues"},
    },
}

#: The corrected case (§8): the GitLab plugin is loaded — its collection links
#: render — but the account may not see the GitLab tab, so it has no permission.
#: GitHub is fully readable; no repository is attached, so no revisions link.
WORK_PACKAGE_GITHUB_ONLY: dict[str, Any] = {
    "_type": "WorkPackage",
    "id": WORK_PACKAGE_ID,
    "subject": "Ship the client layer",
    "_links": {
        **_SELF_LINKS,
        "github": {"href": f"/work_packages/{WORK_PACKAGE_ID}/tabs/github", "title": "GitHub"},
        "github_pull_requests": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/github_pull_requests"
        },
        "gitlab_merge_requests": {
            "href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/gitlab_merge_requests"
        },
        "gitlab_issues": {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}/gitlab_issues"},
    },
}

#: Neither plugin is loaded and no repository is attached.
WORK_PACKAGE_NO_SOURCES: dict[str, Any] = {
    "_type": "WorkPackage",
    "id": WORK_PACKAGE_ID,
    "subject": "Ship the client layer",
    "_links": dict(_SELF_LINKS),
}


# --- revisions ------------------------------------------------------------

REVISION_ELEMENTS: list[dict[str, Any]] = [
    {
        "_type": "Revision",
        "id": 91,
        "identifier": "0f2e1c9a6b8d4f31a7c5e0b9d8f7a6c5e4d3b2a1",
        "formattedIdentifier": "0f2e1c9",
        "authorName": "Grace Hopper",
        "message": {
            "format": "plain",
            "raw": "refs #1234 pool the httpx client\n\nAdds retries and backoff.",
            "html": "<p>refs #1234 pool the httpx client</p>",
        },
        "createdAt": "2026-07-03T08:12:44Z",
        "_links": {
            "self": {"href": "/api/v3/revisions/91"},
            "project": {"href": "/api/v3/projects/5", "title": "Platform"},
            "author": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
            "showRevision": {
                "href": "/projects/platform/repository/revision/0f2e1c9a6b8d4f31a7c5e0b9d8f7a6c5"
            },
        },
    },
    {
        "_type": "Revision",
        "id": 92,
        "identifier": "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b",
        "formattedIdentifier": "9a8b7c6",
        "authorName": "Ada Lovelace",
        "message": {"format": "plain", "raw": "fixes #1234 drop the sentinel dates", "html": ""},
        "createdAt": "2026-07-04T16:40:02Z",
        "_links": {
            "self": {"href": "/api/v3/revisions/92"},
            "showRevision": {
                "href": "/projects/platform/repository/revision/9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d"
            },
        },
    },
]

REVISION_COLLECTION = hal_collection(REVISION_ELEMENTS)


# --- GitHub ---------------------------------------------------------------

PULL_REQUEST_ELEMENT: dict[str, Any] = {
    "_type": "GithubPullRequest",
    "id": PULL_REQUEST_ID,
    "number": 481,
    "title": "Pool the httpx client",
    "state": "closed",
    "draft": False,
    "merged": True,
    "mergedAt": "2026-07-05T10:02:31Z",
    "htmlUrl": "https://github.com/acme/platform/pull/481",
    "repository": "acme/platform",
    "labels": [
        {"name": "backend", "color": "#0e8a16"},
        {"name": "needs-review", "color": "#d93f0b"},
    ],
    "commentsCount": 4,
    "reviewCommentsCount": 2,
    "additions": 210,
    "deletions": 38,
    "changedFiles": 7,
    "githubUpdatedAt": "2026-07-05T10:02:31Z",
    "createdAt": "2026-07-03T09:00:00Z",
    "updatedAt": "2026-07-05T10:03:00Z",
    "_links": {
        "self": {"href": f"/api/v3/github_pull_requests/{PULL_REQUEST_ID}"},
        "githubUser": {"href": "/api/v3/github_users/3", "title": "ghopper"},
        "mergedBy": {"href": "/api/v3/github_users/1", "title": "alovelace"},
        "workPackages": [
            {"href": f"/api/v3/work_packages/{WORK_PACKAGE_ID}", "title": "Ship the client layer"}
        ],
    },
    "_embedded": {
        "checkRuns": [
            {
                "_type": "GithubCheckRun",
                "id": 55,
                "name": "ci/test",
                "status": "completed",
                "conclusion": "success",
                "htmlUrl": "https://github.com/acme/platform/runs/55",
                "startedAt": "2026-07-03T09:05:00Z",
                "completedAt": "2026-07-03T09:19:00Z",
            },
            {
                "_type": "GithubCheckRun",
                "id": 56,
                "name": "ci/lint",
                "status": "completed",
                "conclusion": "failure",
                "htmlUrl": "https://github.com/acme/platform/runs/56",
                "completedAt": "2026-07-03T09:11:00Z",
            },
        ]
    },
}

#: A second PR, still open, with the check-runs collection sent as a HAL
#: collection rather than a bare list — both shapes occur in the wild.
OPEN_PULL_REQUEST_ELEMENT: dict[str, Any] = {
    "_type": "GithubPullRequest",
    "id": 18,
    "number": 492,
    "title": "Drop sentinel dates",
    "state": "open",
    "draft": True,
    "merged": False,
    "mergedAt": None,
    "htmlUrl": "https://github.com/acme/platform/pull/492",
    "repository": "acme/platform",
    "labels": ["chore"],
    "_links": {
        "self": {"href": "/api/v3/github_pull_requests/18"},
        "githubUser": {"href": "/api/v3/github_users/3", "title": "ghopper"},
    },
    "_embedded": {
        "checkRuns": hal_collection(
            [
                {
                    "_type": "GithubCheckRun",
                    "id": 57,
                    "name": "ci/test",
                    "status": "in_progress",
                    "conclusion": None,
                    "htmlUrl": "https://github.com/acme/platform/runs/57",
                    "startedAt": "2026-07-06T07:00:00Z",
                }
            ]
        )
    },
}

PULL_REQUEST_COLLECTION = hal_collection([PULL_REQUEST_ELEMENT, OPEN_PULL_REQUEST_ELEMENT])

PULL_REQUEST_DETAIL: dict[str, Any] = {
    **PULL_REQUEST_ELEMENT,
    "body": {
        "format": "markdown",
        "raw": "Fixes OP#1234.\n\nPools the client and adds retries.",
        "html": "<p>Fixes OP#1234.</p>",
    },
}


# --- GitLab ---------------------------------------------------------------

MERGE_REQUEST_ELEMENT: dict[str, Any] = {
    "_type": "GitlabMergeRequest",
    "id": 44,
    "number": 12,
    "title": "Retry policy for reads",
    "state": "merged",
    "draft": False,
    "mergedAt": "2026-07-06T12:00:00Z",
    "htmlUrl": "https://gitlab.com/acme/platform/-/merge_requests/12",
    "repository": "acme/platform",
    "labels": [{"name": "backend"}],
    "gitlabUpdatedAt": "2026-07-06T12:00:00Z",
    "_links": {
        "self": {"href": "/api/v3/gitlab_merge_requests/44"},
        "gitlabUser": {"href": "/api/v3/gitlab_users/8", "title": "ghopper"},
    },
    "_embedded": {
        "pipelines": [
            {
                "_type": "GitlabPipeline",
                "id": 71,
                "name": "build",
                "status": "success",
                "commitId": "9a8b7c6",
                "htmlUrl": "https://gitlab.com/acme/platform/-/pipelines/71",
                "startedAt": "2026-07-06T11:30:00Z",
                "finishedAt": "2026-07-06T11:48:00Z",
            }
        ]
    },
}

MERGE_REQUEST_COLLECTION = hal_collection([MERGE_REQUEST_ELEMENT])

GITLAB_ISSUE_ELEMENT: dict[str, Any] = {
    "_type": "GitlabIssue",
    "id": 66,
    "number": 3,
    "title": "Flaky retry test",
    "state": "opened",
    "htmlUrl": "https://gitlab.com/acme/platform/-/issues/3",
    "repository": "acme/platform",
    "labels": ["flaky"],
    "createdAt": "2026-07-02T07:00:00Z",
    "updatedAt": "2026-07-06T09:00:00Z",
    "_links": {
        "self": {"href": "/api/v3/gitlab_issues/66"},
        "gitlabUser": {"href": "/api/v3/gitlab_users/8", "title": "ghopper"},
    },
}

GITLAB_ISSUE_COLLECTION = hal_collection([GITLAB_ISSUE_ELEMENT])


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


# --- saved queries --------------------------------------------------------

STATUS_FILTER: dict[str, Any] = {
    "_type": "QueryFilterInstance",
    "name": "Status",
    "_links": {
        "self": {"href": "/api/v3/queries/filter_instance_schemas/status"},
        "filter": {"href": "/api/v3/queries/filters/status", "title": "Status"},
        "operator": {"href": "/api/v3/queries/operators/o", "title": "open"},
        "values": [],
    },
}

ASSIGNEE_FILTER: dict[str, Any] = {
    "_type": "QueryFilterInstance",
    "name": "Assignee",
    "_links": {
        "filter": {"href": "/api/v3/queries/filters/assignee", "title": "Assignee"},
        "operator": {"href": "/api/v3/queries/operators/%3D", "title": "is (OR)"},
        "values": [
            {"href": "/api/v3/users/12", "title": "Grace Hopper"},
            {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
        ],
    },
}

DUE_DATE_FILTER: dict[str, Any] = {
    "_type": "QueryFilterInstance",
    "name": "Finish date",
    "values": ["", "2026-08-01"],
    "_links": {
        "filter": {"href": "/api/v3/queries/filters/dueDate", "title": "Finish date"},
        "operator": {"href": "/api/v3/queries/operators/%3C%3Ed", "title": "between"},
    },
}

PROJECT_QUERY: dict[str, Any] = {
    "_type": "Query",
    "id": QUERY_ID,
    "name": "Sprint board",
    "createdAt": "2026-05-02T10:00:00Z",
    "updatedAt": "2026-07-01T11:30:00Z",
    "public": True,
    "starred": True,
    "sums": True,
    "hidden": False,
    "timelineVisible": False,
    "filters": [STATUS_FILTER, ASSIGNEE_FILTER, DUE_DATE_FILTER],
    "_links": {
        "self": {"href": f"/api/v3/queries/{QUERY_ID}"},
        "project": {"href": "/api/v3/projects/5", "title": "Platform"},
        "user": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
        "groupBy": {"href": "/api/v3/queries/group_bys/status", "title": "Status"},
        "sortBy": [{"href": "/api/v3/queries/sort_bys/dueDate-asc", "title": "Finish date asc"}],
    },
}

GLOBAL_QUERY: dict[str, Any] = {
    "_type": "Query",
    "id": 16,
    "name": "Everything assigned to me",
    "createdAt": "2026-06-01T08:00:00Z",
    "updatedAt": "2026-06-30T09:15:00Z",
    "public": False,
    "starred": False,
    "sums": False,
    "filters": [STATUS_FILTER],
    "_links": {
        "self": {"href": "/api/v3/queries/16"},
        "project": {"href": None},
        "user": {"href": "/api/v3/users/1", "title": "Ada Lovelace"},
    },
}

QUERY_COLLECTION = hal_collection([PROJECT_QUERY, GLOBAL_QUERY], total=2, pageSize=20, offset=1)


def work_package_element(
    work_package_id: int, subject: str, *, status: str = "In progress"
) -> dict[str, Any]:
    """A work-package element as the embedded query results carry it."""
    return {
        "_type": "WorkPackage",
        "id": work_package_id,
        "subject": subject,
        "startDate": "2026-07-01",
        "dueDate": "2026-07-31",
        "percentageDone": 40,
        "updatedAt": "2026-07-06T09:00:00Z",
        "_links": {
            "self": {"href": f"/api/v3/work_packages/{work_package_id}"},
            "type": {"href": "/api/v3/types/1", "title": "Task"},
            "status": {"href": "/api/v3/statuses/7", "title": status},
            "priority": {"href": "/api/v3/priorities/8", "title": "Normal"},
            "assignee": {"href": "/api/v3/users/12", "title": "Grace Hopper"},
            "project": {"href": "/api/v3/projects/5", "title": "Platform"},
        },
    }


QUERY_RESULT_ELEMENTS: list[dict[str, Any]] = [
    work_package_element(1234, "Ship the client layer"),
    work_package_element(1235, "Drop sentinel dates", status="New"),
]

QUERY_RESULTS: dict[str, Any] = {
    "_type": "WorkPackageCollection",
    "total": 37,
    "count": 2,
    "pageSize": 2,
    "offset": 1,
    "groups": [
        {"value": "In progress", "count": 12, "sums": {"estimatedTime": "PT41H30M"}},
        {
            "value": {"href": "/api/v3/statuses/1", "title": "New"},
            "count": 25,
            "sums": {"estimatedTime": "PT100H"},
        },
    ],
    "totalSums": {"estimatedTime": "PT141H30M", "storyPoints": 55},
    "_embedded": {"elements": QUERY_RESULT_ELEMENTS},
}

QUERY_WITH_RESULTS: dict[str, Any] = {
    **PROJECT_QUERY,
    "_embedded": {"results": QUERY_RESULTS},
}

QUERY_INVALID_FILTER_ERROR: dict[str, Any] = {
    "_type": "Error",
    "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
    "message": "Filters Assignee filter has invalid values.",
    "_embedded": {
        "errors": [
            {
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:InvalidQuery",
                "message": "Assignee filter has invalid values.",
                "_embedded": {"details": {"attribute": "filters"}},
            }
        ]
    },
}
