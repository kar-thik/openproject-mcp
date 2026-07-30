"""Git and development activity tools (SPEC §6.5, §8 — Phase 2).

Lands here:

=========================================  ======  ===========================================
Tool                                       Phase   Endpoint(s)
=========================================  ======  ===========================================
🔍Ⓜ ``get_work_package_git_activity``      2       ``GET /work_packages/{id}``
                                                   + ``…/revisions``
                                                   + ``…/github_pull_requests``
                                                   + ``…/gitlab_merge_requests``
                                                   + ``…/gitlab_issues``
🔍Ⓜ ``get_github_pull_request``            2       ``GET /github_pull_requests/{id}``
=========================================  ======  ===========================================

Non-negotiables for this module:

* **Availability detection (SPEC §8, corrected against source).** The
  ``github_pull_requests`` / ``gitlab_merge_requests`` / ``gitlab_issues``
  collection links on a work package are rendered **unconditionally** whenever
  the bundled plugin is loaded — they say nothing about permission. The
  permission-gated signals are the ``github`` / ``gitlab`` **tab** links
  (``show_github_content`` / ``show_gitlab_content``) and, for SCM, the
  ``revisions`` link (``view_changesets``). Both signals are used: the gated
  link decides ``available``, and the unconditional collection link tells a
  missing permission ("module installed, you cannot see it") apart from a
  missing module ("not available on this instance").
* **One failing source never fails the tool** (G5). A 403 on a source becomes a
  "no permission" note, a 404 a "module not installed" note. Only failures that
  are instance-wide rather than source-specific (auth, rate limit, network)
  propagate as errors — degrading those would hide a broken connection.
* Every PR/MR carries the **OpenProject-internal ``id``** (the input to
  ``get_github_pull_request``) *and* the GitHub/GitLab-side ``number``. They are
  different numbers and confusing them fetches the wrong resource.
* Sources are fetched concurrently; the work package itself is fetched first
  because its links drive everything else.

Not promised by API v3 (verified, SPEC §8): repository/file/diff/branch
browsing, per-project commit lists, revision→changed-files, creating PR↔WP
links, standalone check-run/pipeline resources, GitLab push history.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from openproject_mcp.client import hal
from openproject_mcp.client.errors import (
    InputValidationError,
    NotFoundError,
    PermissionDeniedError,
    UnexpectedResponseError,
    UpstreamServerError,
    ValidationFailedError,
)
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.projections import Ref
from openproject_mcp.tools import _shared

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "CheckRun",
    "GitActivity",
    "GitlabIssueRow",
    "MergeRequestRow",
    "Pipeline",
    "PullRequestDetail",
    "PullRequestRow",
    "Revision",
    "SourceAvailability",
    "register",
]

SourceName = Literal["revisions", "github", "gitlab"]

#: The three availability groups, in the order their notes are reported.
SOURCE_NAMES: tuple[SourceName, ...] = ("revisions", "github", "gitlab")

#: Permission-gated links on the work package — the primary availability signal.
GATED_LINKS: dict[SourceName, tuple[str, ...]] = {
    "revisions": ("revisions",),
    "github": ("github",),
    "gitlab": ("gitlab",),
}

#: Links the bundled plugins render unconditionally. Their presence proves the
#: module is installed; it proves nothing about permission. Both spellings are
#: accepted because the link names are declared as Ruby symbols upstream and
#: have been rendered snake_case and camelCase across versions.
PLUGIN_LINKS: dict[SourceName, tuple[str, ...]] = {
    "revisions": (),
    "github": ("github_pull_requests", "githubPullRequests"),
    "gitlab": (
        "gitlab_merge_requests",
        "gitlabMergeRequests",
        "gitlab_issues",
        "gitlabIssues",
    ),
}

MODULE_NAMES: dict[SourceName, str] = {
    "revisions": "repository",
    "github": "GitHub",
    "gitlab": "GitLab",
}


# --- projections ----------------------------------------------------------


class Revision(BaseModel):
    """One SCM commit that references this work package."""

    id: int | str | None = Field(default=None, description="OpenProject revision id.")
    identifier: str | None = Field(default=None, description="Full commit SHA.")
    formatted_identifier: str | None = Field(
        default=None, description="Short SHA as OpenProject displays it, e.g. '0f2e1c9'."
    )
    author_name: str | None = Field(
        default=None,
        description="Committer name as recorded in the repository; not an OpenProject user.",
    )
    message: str | None = Field(
        default=None, description="Commit message, raw and complete (no html)."
    )
    committed_at: str | None = Field(default=None, description="ISO 8601 UTC commit timestamp.")
    show_url: str | None = Field(
        default=None, description="OpenProject UI URL for this revision; null when not rendered."
    )


class CheckRun(BaseModel):
    """One GitHub check run — the CI status of a pull request."""

    id: int | str | None = Field(default=None, description="OpenProject check-run id.")
    name: str | None = Field(default=None, description="Check name, e.g. 'ci/test'.")
    status: str | None = Field(
        default=None, description="Lifecycle: 'queued', 'in_progress' or 'completed'."
    )
    conclusion: str | None = Field(
        default=None,
        description="Outcome once completed: 'success', 'failure', 'cancelled', "
        "'skipped', 'timed_out', 'action_required', 'neutral'. Null while running.",
    )
    html_url: str | None = Field(default=None, description="GitHub URL of the check run.")
    completed_at: str | None = Field(
        default=None, description="ISO 8601 UTC finish time; null while running."
    )


class Pipeline(BaseModel):
    """One GitLab pipeline — the CI status of a merge request."""

    id: int | str | None = Field(default=None, description="OpenProject pipeline id.")
    name: str | None = Field(default=None, description="Pipeline name, when GitLab reports one.")
    status: str | None = Field(
        default=None,
        description="GitLab status: 'success', 'failed', 'running', 'pending', "
        "'canceled', 'skipped'.",
    )
    commit_id: str | None = Field(default=None, description="Commit the pipeline ran against.")
    html_url: str | None = Field(default=None, description="GitLab URL of the pipeline.")
    started_at: str | None = Field(default=None, description="ISO 8601 UTC start time.")
    completed_at: str | None = Field(
        default=None, description="ISO 8601 UTC finish time; null while running."
    )


class _CodeRequest(BaseModel):
    """Fields shared by GitHub pull requests and GitLab merge requests."""

    id: int | str | None = Field(
        default=None,
        description="OpenProject-internal id. This is what get_github_pull_request takes — "
        "NOT the GitHub/GitLab number.",
    )
    number: int | None = Field(
        default=None,
        description="The number humans use on GitHub/GitLab (the '#481' in the PR title). "
        "Never pass it to get_github_pull_request.",
    )
    title: str | None = Field(default=None, description="Pull/merge request title.")
    state: str | None = Field(
        default=None,
        description="Provider state verbatim: GitHub 'open'/'closed', GitLab "
        "'opened'/'closed'/'merged'/'locked'. Check 'merged' for the merge fact.",
    )
    draft: bool = Field(default=False, description="True while marked draft / work in progress.")
    merged: bool = Field(
        default=False, description="True once merged; a closed request may never have merged."
    )
    merged_at: str | None = Field(default=None, description="ISO 8601 UTC merge time, if merged.")
    html_url: str | None = Field(default=None, description="Provider URL of the request.")
    repository: str | None = Field(default=None, description="Repository slug, e.g. 'acme/web'.")
    labels: list[str] = Field(
        default_factory=list[str], description="Label names; always a list, empty when unlabelled."
    )
    author: Ref | None = Field(
        default=None,
        description="Provider account that opened it ({id, name}); a GitHub/GitLab user, "
        "not an OpenProject user.",
    )


class PullRequestRow(_CodeRequest):
    """A GitHub pull request linked to a work package, with its CI status."""

    check_runs: list[CheckRun] = Field(
        default_factory=list[CheckRun],
        description="CI check runs GitHub reported for this pull request; always a list.",
    )


class PullRequestDetail(PullRequestRow):
    """Full pull-request detail (``GET /github_pull_requests/{id}``)."""

    body: str | None = Field(
        default=None, description="Pull-request description as markdown (raw); html is dropped."
    )
    additions: int | None = Field(default=None, description="Lines added across the diff.")
    deletions: int | None = Field(default=None, description="Lines removed across the diff.")
    changed_files: int | None = Field(default=None, description="Number of files touched.")
    comments_count: int | None = Field(default=None, description="Issue-style comments.")
    review_comments_count: int | None = Field(default=None, description="Inline review comments.")
    merged_by: Ref | None = Field(default=None, description="Provider account that merged it.")
    work_packages: list[Ref] = Field(
        default_factory=list[Ref],
        description="Work packages this pull request is linked to; a PR may reference several.",
    )
    created_at: str | None = Field(default=None, description="ISO 8601 UTC creation time.")
    updated_at: str | None = Field(
        default=None, description="ISO 8601 UTC time OpenProject last synced this record."
    )


class MergeRequestRow(_CodeRequest):
    """A GitLab merge request linked to a work package, with its pipelines."""

    pipelines: list[Pipeline] = Field(
        default_factory=list[Pipeline],
        description="GitLab pipelines for this merge request; always a list. GitLab's "
        "equivalent of GitHub check runs.",
    )


class GitlabIssueRow(BaseModel):
    """A GitLab issue linked to a work package."""

    id: int | str | None = Field(default=None, description="OpenProject-internal id.")
    number: int | None = Field(default=None, description="GitLab issue number (iid).")
    title: str | None = Field(default=None, description="Issue title.")
    state: str | None = Field(default=None, description="GitLab state: 'opened' or 'closed'.")
    html_url: str | None = Field(default=None, description="GitLab URL of the issue.")
    repository: str | None = Field(default=None, description="Repository slug.")
    labels: list[str] = Field(default_factory=list[str], description="Label names; always a list.")
    author: Ref | None = Field(default=None, description="GitLab account that opened it.")
    created_at: str | None = Field(default=None, description="ISO 8601 UTC creation time.")
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC last-update time.")


class SourceAvailability(BaseModel):
    """Which development sources this work package can actually answer for.

    False means "do not conclude there is no code": either the module is absent
    or this account may not see it. ``notes`` says which.
    """

    revisions: bool = Field(
        default=False, description="SCM commits are readable (repository linked + permission)."
    )
    github: bool = Field(default=False, description="GitHub module readable for this work package.")
    gitlab: bool = Field(default=False, description="GitLab module readable for this work package.")


class GitActivity(BaseModel):
    """Everything OpenProject knows about the code behind one work package."""

    work_package: Ref | None = Field(
        default=None, description="The work package these results belong to ({id, name})."
    )
    available: SourceAvailability = Field(
        description="Per-source availability, derived from the work package's own links."
    )
    revisions: list[Revision] = Field(
        default_factory=list[Revision],
        description="Commits referencing this work package; always a list.",
    )
    github_pull_requests: list[PullRequestRow] = Field(
        default_factory=list[PullRequestRow],
        description="Linked GitHub pull requests; always a list.",
    )
    gitlab_merge_requests: list[MergeRequestRow] = Field(
        default_factory=list[MergeRequestRow],
        description="Linked GitLab merge requests; always a list.",
    )
    gitlab_issues: list[GitlabIssueRow] = Field(
        default_factory=list[GitlabIssueRow], description="Linked GitLab issues; always a list."
    )
    notes: list[str] = Field(
        default_factory=list[str],
        description="Degradation markers (G5): which sources were skipped and why. Read them "
        "before reporting 'there are no pull requests'.",
    )


# --- payload helpers ------------------------------------------------------


def _links_of(payload: Mapping[str, Any] | None) -> set[str]:
    links = hal.as_object(payload.get("_links")) if payload is not None else None
    return set(links) if links is not None else set()


def _labels(raw: Any) -> list[str]:
    """Label names from ``[{name, color}]`` or a bare list of strings."""
    names: list[str] = []
    for item in hal.as_array(raw) or ():
        if isinstance(item, str):
            names.append(item)
            continue
        entry = hal.as_object(item)
        if entry is None:
            continue
        name = entry.get("name") or entry.get("title")
        if isinstance(name, str):
            names.append(name)
    return names


def _embedded_elements(payload: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    """Embedded sub-resources, whether sent as a bare list or a HAL collection."""
    for key in keys:
        raw = hal.embedded(payload, key)
        wrapped = hal.as_object(raw)
        if wrapped is not None:
            return hal.collection(wrapped).elements
        if hal.as_array(raw) is not None:
            return [dict(item) for item in hal.as_objects(raw)]
    return []


def _first_ref(payload: Mapping[str, Any], *keys: str) -> Ref | None:
    for key in keys:
        resolved = Ref.from_hal(payload, key)
        if resolved is not None:
            return resolved
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _first_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _revision(element: Mapping[str, Any]) -> Revision:
    show = hal.ref(element, "showRevision")
    return Revision(
        id=hal.self_id(element),
        identifier=element.get("identifier"),
        formatted_identifier=element.get("formattedIdentifier"),
        author_name=element.get("authorName"),
        message=hal.formattable(element.get("message")),
        committed_at=_first_value(element, "createdAt", "committedAt"),
        show_url=show.href if show is not None else None,
    )


def _check_run(element: Mapping[str, Any]) -> CheckRun:
    return CheckRun(
        id=hal.self_id(element),
        name=element.get("name"),
        status=element.get("status"),
        conclusion=element.get("conclusion"),
        html_url=_first_value(element, "htmlUrl", "detailsUrl"),
        completed_at=_first_value(element, "completedAt", "finishedAt"),
    )


def _pipeline(element: Mapping[str, Any]) -> Pipeline:
    return Pipeline(
        id=hal.self_id(element),
        name=element.get("name"),
        status=element.get("status"),
        commit_id=element.get("commitId"),
        html_url=element.get("htmlUrl"),
        started_at=element.get("startedAt"),
        completed_at=_first_value(element, "completedAt", "finishedAt"),
    )


def _code_request_fields(element: Mapping[str, Any]) -> dict[str, Any]:
    """Fields shared by pull requests and merge requests, as constructor kwargs."""
    merged_at = _first_value(element, "mergedAt", "merged_at")
    state = element.get("state")
    merged = element.get("merged")
    if not isinstance(merged, bool):
        # GitLab has no boolean: 'merged' is a state, and mergedAt is set with it.
        merged = bool(merged_at) or (isinstance(state, str) and state.lower() == "merged")
    draft = _first_value(element, "draft", "workInProgress")
    return {
        "id": hal.self_id(element),
        "number": _int_or_none(_first_value(element, "number", "iid")),
        "title": element.get("title"),
        "state": state if isinstance(state, str) else None,
        "draft": bool(draft),
        "merged": merged,
        "merged_at": merged_at,
        "html_url": _first_value(element, "htmlUrl", "webUrl"),
        "repository": element.get("repository"),
        "labels": _labels(element.get("labels")),
        "author": _first_ref(element, "githubUser", "gitlabUser", "author", "user"),
    }


def _pull_request_row(element: Mapping[str, Any]) -> PullRequestRow:
    return PullRequestRow(
        **_code_request_fields(element),
        check_runs=[_check_run(item) for item in _embedded_elements(element, "checkRuns")],
    )


def _pull_request_detail(element: Mapping[str, Any]) -> PullRequestDetail:
    return PullRequestDetail(
        **_code_request_fields(element),
        check_runs=[_check_run(item) for item in _embedded_elements(element, "checkRuns")],
        body=hal.formattable(element.get("body")),
        additions=_int_or_none(element.get("additions")),
        deletions=_int_or_none(element.get("deletions")),
        changed_files=_int_or_none(element.get("changedFiles")),
        comments_count=_int_or_none(element.get("commentsCount")),
        review_comments_count=_int_or_none(
            _first_value(element, "reviewCommentsCount", "reviewComments")
        ),
        merged_by=Ref.from_hal(element, "mergedBy"),
        work_packages=Ref.list_from_hal(element, "workPackages"),
        created_at=element.get("createdAt"),
        updated_at=_first_value(element, "updatedAt", "githubUpdatedAt"),
    )


def _merge_request_row(element: Mapping[str, Any]) -> MergeRequestRow:
    return MergeRequestRow(
        **_code_request_fields(element),
        pipelines=[_pipeline(item) for item in _embedded_elements(element, "pipelines")],
    )


def _gitlab_issue_row(element: Mapping[str, Any]) -> GitlabIssueRow:
    state = element.get("state")
    return GitlabIssueRow(
        id=hal.self_id(element),
        number=_int_or_none(_first_value(element, "number", "iid")),
        title=element.get("title"),
        state=state if isinstance(state, str) else None,
        html_url=_first_value(element, "htmlUrl", "webUrl"),
        repository=element.get("repository"),
        labels=_labels(element.get("labels")),
        author=_first_ref(element, "gitlabUser", "author", "user"),
        created_at=element.get("createdAt"),
        updated_at=_first_value(element, "updatedAt", "gitlabUpdatedAt"),
    )


# --- availability and fan-out (SPEC §8) -----------------------------------


@dataclass(frozen=True, slots=True)
class _Source:
    """One upstream collection to fetch, and where its result belongs."""

    field: str
    group: SourceName
    label: str
    path: str


@dataclass(frozen=True, slots=True)
class _Fetched:
    """The outcome of one source fetch — never an exception (G5)."""

    source: _Source
    elements: list[dict[str, Any]]
    note: str | None = None
    ok: bool = True


def _availability(work_package: Mapping[str, Any]) -> tuple[dict[SourceName, bool], list[str]]:
    """Decide per-source availability from the work package's links (SPEC §8).

    The gated tab/revisions link is the verdict; the unconditional collection
    link only sharpens the note ("no permission" vs "module absent").
    """
    links = _links_of(work_package)
    available: dict[SourceName, bool] = {}
    notes: list[str] = []
    for name in SOURCE_NAMES:
        gated = bool(links & set(GATED_LINKS[name]))
        available[name] = gated
        if gated:
            continue
        module = MODULE_NAMES[name]
        if links & set(PLUGIN_LINKS[name]):
            notes.append(
                f"{name}: the {module} module is installed but this account may not view "
                f"{module} content on this work package (no permission)"
            )
        elif name == "revisions":
            notes.append(
                "revisions: not available for this work package (no repository is linked to "
                "the project, or this account lacks the 'view changesets' permission)"
            )
        else:
            notes.append(f"{name}: not available on this instance (module absent)")
    return available, notes


def _sources_for(work_package_id: int, group: SourceName) -> list[_Source]:
    base = f"work_packages/{work_package_id}"
    if group == "revisions":
        return [_Source("revisions", group, "revisions", f"{base}/revisions")]
    if group == "github":
        return [
            _Source(
                "github_pull_requests",
                group,
                "github pull requests",
                f"{base}/github_pull_requests",
            )
        ]
    return [
        _Source(
            "gitlab_merge_requests",
            group,
            "gitlab merge requests",
            f"{base}/gitlab_merge_requests",
        ),
        _Source("gitlab_issues", group, "gitlab issues", f"{base}/gitlab_issues"),
    ]


async def _fetch_source(client: OpenProjectClient, source: _Source) -> _Fetched:
    """Fetch one source; downgrade a source-specific failure to a note (G5).

    Authentication, rate-limit and network failures are deliberately *not*
    caught: they are instance-wide, and reporting "no pull requests" when the
    connection is broken would be a lie.
    """
    try:
        payload = await client.get_json(source.path)
    except PermissionDeniedError:
        return _Fetched(
            source,
            [],
            f"{source.label}: no permission (403) — the module is installed but this account "
            "may not read it here",
            ok=False,
        )
    except NotFoundError:
        return _Fetched(
            source,
            [],
            f"{source.label}: module not installed on this instance (404)",
            ok=False,
        )
    except (ValidationFailedError, UpstreamServerError, UnexpectedResponseError) as exc:
        return _Fetched(source, [], f"{source.label}: unavailable ({exc.message})", ok=False)
    return _Fetched(source, list(hal.collection(payload)))


async def _fan_out(client: OpenProjectClient, sources: Sequence[_Source]) -> list[_Fetched]:
    """Fetch every source concurrently, preserving the requested order."""
    if not sources:
        return []
    results = await asyncio.gather(
        *(_fetch_source(client, source) for source in sources), return_exceptions=True
    )
    fetched: list[_Fetched] = []
    for result in results:
        if isinstance(result, BaseException):
            raise result
        fetched.append(result)
    return fetched


def register(mcp: FastMCP) -> None:
    """Register the git / development-activity tools (SPEC §6.5, §8)."""

    @mcp.tool(
        name="get_work_package_git_activity",
        tags=_shared.tool_tags(_shared.GROUP_GIT, _shared.READ),
        annotations=_shared.read_annotations(title="Get work package git activity"),
    )
    @_shared.tool_errors
    async def get_work_package_git_activity(
        work_package_id: Annotated[
            int,
            Field(
                description="Work package id. Comes from search_work_packages, "
                "list_work_packages or get_work_package — never guess it."
            ),
        ],
        include: Annotated[
            list[SourceName] | None,
            Field(
                description="Which sources to fetch: any of 'revisions' (SCM commits), "
                "'github' (pull requests + CI check runs), 'gitlab' (merge requests, "
                "issues + pipelines). Omit for all three — they are fetched concurrently, "
                "so narrowing this saves little. Availability is reported for all three "
                "regardless of what was fetched."
            ),
        ] = None,
    ) -> GitActivity:
        """Show the code behind a work package: commits, pull/merge requests and CI status.

        Use this for "is this ticket implemented", "what shipped for it", "did CI pass",
        "which branch/PR is this in". It returns, in one call: `revisions` (commits whose
        message references the work package, with full SHA, short SHA, author, message and
        commit time), `github_pull_requests` (title, state, draft, merged/merged_at, labels,
        author, URL and the CI `check_runs` with status and conclusion),
        `gitlab_merge_requests` (the same, with `pipelines` instead of check runs) and
        `gitlab_issues`.

        `available` says, per source, whether this instance and this account can answer at
        all, and `notes` explains every false — "module absent" and "no permission" are
        different answers and neither means "no code was written". Report the notes rather
        than concluding a ticket has no development activity.

        Pitfalls. Every pull/merge request carries **two** numbers: `id` is the
        OpenProject-internal id (the only thing `get_github_pull_request` accepts) and
        `number` is the '#481' humans quote on GitHub/GitLab. A `state` of 'closed' does not
        mean merged — check `merged`. A source that 403s or 404s is reported in `notes`, not
        raised, so a missing GitLab module never hides GitHub results.

        Nothing appears here by magic. Links are created by text, not by the API: a commit
        message must mention the work package ('refs #123', or 'fixes #123' / 'closes #123'
        to also close it), and a pull or merge request must mention 'OP#123' or the full
        work-package URL in its description or a comment. OpenProject cannot browse
        repositories, list branches or diffs, or create these links through the API.

        Cross-references: full pull-request detail (body, diff counts, all check runs) via
        `get_github_pull_request(github_pull_request_id=<the id field>)`; the ticket itself
        via `get_work_package`; the discussion via `list_work_package_comments`.
        """
        context = _shared.get_tool_context()
        requested: list[SourceName]
        if include is None:
            requested = list(SOURCE_NAMES)
        else:
            requested = [name for name in SOURCE_NAMES if name in include]
            if not requested:
                raise InputValidationError(
                    "include is empty.",
                    hint="Pass any of 'revisions', 'github', 'gitlab', or omit include for all.",
                )

        work_package = await context.client.get_json(f"work_packages/{work_package_id}")
        available, notes = _availability(work_package)

        sources: list[_Source] = []
        for name in SOURCE_NAMES:
            if not available[name]:
                continue
            if name not in requested:
                notes.append(f"{name}: available but not fetched (not listed in include)")
                continue
            sources.extend(_sources_for(work_package_id, name))

        collected: dict[str, list[dict[str, Any]]] = {}
        for fetched in await _fan_out(context.client, sources):
            collected[fetched.source.field] = fetched.elements
            if fetched.note:
                notes.append(fetched.note)
            if not fetched.ok:
                # The gated link promised access; the endpoint disagreed (G5).
                available[fetched.source.group] = False

        return GitActivity(
            work_package=Ref(id=hal.self_id(work_package), name=work_package.get("subject")),
            available=SourceAvailability(**available),
            revisions=[_revision(item) for item in collected.get("revisions", [])],
            github_pull_requests=[
                _pull_request_row(item) for item in collected.get("github_pull_requests", [])
            ],
            gitlab_merge_requests=[
                _merge_request_row(item) for item in collected.get("gitlab_merge_requests", [])
            ],
            gitlab_issues=[_gitlab_issue_row(item) for item in collected.get("gitlab_issues", [])],
            notes=notes,
        )

    @mcp.tool(
        name="get_github_pull_request",
        tags=_shared.tool_tags(_shared.GROUP_GIT, _shared.READ),
        annotations=_shared.read_annotations(title="Get GitHub pull request"),
    )
    @_shared.tool_errors
    async def get_github_pull_request(
        github_pull_request_id: Annotated[
            int,
            Field(
                description="The **OpenProject-internal** pull-request id — the 'id' field of "
                "an entry in get_work_package_git_activity's github_pull_requests. It is NOT "
                "the GitHub PR number ('number' in that same entry, the '#481' on github.com); "
                "passing the GitHub number fetches the wrong record or 404s."
            ),
        ],
    ) -> PullRequestDetail:
        """Read one linked GitHub pull request in full, including its CI check runs.

        Use it after `get_work_package_git_activity` when the summary is not enough: this
        adds the pull-request `body` (markdown), the diff size (`additions`, `deletions`,
        `changed_files`), comment counts, who merged it, and every work package the PR is
        linked to — plus the same `check_runs` with status and conclusion.

        Pitfalls. `github_pull_request_id` is OpenProject's id, never the GitHub number; the
        two are unrelated and there is no lookup by GitHub number. The record is a mirror
        that OpenProject refreshes from GitHub webhooks, so `updated_at` is when OpenProject
        last synced, not when GitHub changed. A 404 usually means the id came from the wrong
        field or the GitHub module is not installed on this instance.

        A pull request appears in OpenProject only when its description or a comment
        mentions 'OP#123' or the full work-package URL; commits link separately via
        'refs #123' in the commit message. Neither link can be created through the API.

        Cross-references: find the id with `get_work_package_git_activity(work_package_id=…)`;
        GitLab merge requests have no per-id tool — they come back in full from that same
        call.
        """
        context = _shared.get_tool_context()
        payload = await context.client.get_json(f"github_pull_requests/{github_pull_request_id}")
        return _pull_request_detail(payload)
