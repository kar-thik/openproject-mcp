# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is 0.x, MINOR releases may change the MCP tool surface
(tools renamed or removed, parameter or result-envelope semantics changed);
PATCH releases are fixes and strictly additive changes only.

## [Unreleased]

### Fixed

- `server.json` description within the MCP registry's 100-character limit;
  the 0.3.2 registry publish failed on it after PyPI and the GitHub release
  had gone out. A test now pins the limit, and `release.yml` can be dispatched
  by hand with a version to re-publish just the registry entry.

## [0.3.2] - 2026-09-24

### Added

- `OPENPROJECT_MCP_PROFILE=core` hides the module-backed tool groups
  (meetings, news, documents, wiki, budgets, git activity, reporting) and the
  two reporting prompts at startup, for a smaller tool list in work-package
  sessions. The default, `full`, changes nothing.
- `enable_tool_group` brings a group hidden by the `core` profile back for the
  calling session only. It never calls OpenProject and cannot lift
  `OPENPROJECT_MCP_READ_ONLY`, the admin gate or `OPENPROJECT_MCP_DISABLE`.
- The six recurring-meeting tools also carry a `meetings_recurring` tag, so
  `OPENPROJECT_MCP_DISABLE=meetings_recurring` drops just them (useful before
  OpenProject 17.4); `meetings` still drops the whole family.
- A startup warning when `OPENPROJECT_MCP_DISABLE` names a group that does not
  exist, instead of ignoring the typo silently.

### Changed

- The server now emits `tools/list_changed`, but only after
  `enable_tool_group` changes a session's tool list, and only to that session.
  Under the default `full` profile the tool list stays fixed.
- Tighter, less repetitive descriptions on the seven largest tools —
  `list_work_packages`, `save_query`, `create_recurring_meeting`,
  `list_time_entries`, `create_work_package`, `update_work_package` and
  `update_meeting` — plus a size-budget test (`test_description_budget.py`)
  that fails the build if a tool's or the whole surface's description text
  regrows past its current size.

### Fixed

- Descriptions that name the admin-gated membership tools now say they are
  hidden unless `OPENPROJECT_MCP_ADMIN_TOOLS=1`, and the server instructions
  tell the model what to do when a named tool is absent; a test pins it.
- `server.json` lists `OPENPROJECT_OAUTH_TOKEN` and describes the surface
  switches.

## [0.3.1] - 2026-09-23

### Added

- `story_points` and `remaining_hours` on `create_work_package`,
  `update_work_package` and `bulk_update_work_packages`, surfaced by
  `get_work_package` and the `openproject://work_package/{id}` resource.
  Omitted means untouched; zero is written as zero.
  ([#19](https://github.com/kar-thik/openproject-mcp/pull/19), thanks
  [@sinasadeghi83](https://github.com/sinasadeghi83))
- Work-package writes flag a `story_points` that OpenProject ignores: 14.x
  accepts it without the Backlogs module (or on a type Backlogs does not treat
  as a story) and drops it silently. The result gets a note with the fix, and
  `bulk_update_work_packages` previews warn per item before anything is
  written.

## [0.3.0] - 2026-09-07

### Added

- Opt-in disposable-instance compatibility tests and a CI matrix for OpenProject
  14.6, 15.5, 16.6 and 17.8, covering custom fields, target versions, batch
  preview/apply, stale locks, permissions and meeting API variants.

- `bulk_update_work_packages`: preview up to 50 updates with before/after diffs,
  then apply with reviewed lock versions. Whole-batch preflight and per-item
  execution results expose conflicts, partial failures and uncertain outcomes.

- Target-version lists in work-package details and create/update calls, with
  schema-driven compatibility for OpenProject 17.8 and older instances.
  The existing `version` argument remains a single-assignment alias.

### Changed

- Reporting renames `closed` to `closed_updated`: currently closed work updated
  in the window, not verified completions. Weekly and standup wording now
  reflects that distinction. Sprint health is marked unassessed instead of
  inferred from ticket counts.

### Fixed

- Correct stale read-tool and destructive-tool counts in the README.

## [0.2.0] - 2026-08-11

### Added

- OpenProject 17.x compatibility surfaced in existing tools:
  - `list_projects`/`get_project` rows carry `workspace_type`
    (`project` | `program` | `portfolio`) — on 17.x the projects index
    deliberately mixes all three workspace kinds.
  - Work-package rows and details carry `display_id`, and
    `get_work_package` accepts the semantic identifier form (`PROJ-42`)
    on instances that enable it; a semantic miss hints at the numeric id.
  - `get_project_metadata` type rows carry `own_name` and `parent` for
    the 17.7+ type-variant model (null on older instances).
- Meetings suite: 13 new tools completing the meetings write surface
  (OpenProject 17.4+ routes; the flat agenda-item/outcome writes need 17.6+).
  Meetings gain `update_meeting` — including the lifecycle `state`, where
  publishing a draft is `state='open'` — and `delete_meeting`; agenda items
  gain `update_meeting_agenda_item` and `delete_meeting_agenda_item`; outcomes
  gain `add_meeting_outcome`, `update_meeting_outcome` and
  `delete_meeting_outcome` (writable only while the meeting is
  `in_progress`). A new recurring-meetings module adds
  `list_recurring_meetings`, `get_recurring_meeting`,
  `create_recurring_meeting`, `delete_recurring_meeting`,
  `init_recurring_meeting_occurrence` and
  `cancel_recurring_meeting_occurrence`. Meeting and agenda-item updates are
  lock-safe (a concurrent edit answers 409 carrying the fresh `lockVersion`),
  every delete is confirm-gated, and 404/405s on these routes explain the
  version floor instead of reading as "does not exist". The recurring tools
  absorb the upstream traps: `timeZone` being overwritten on create (corrected
  with a follow-up PATCH), `duration` as a plain number of hours, the draft
  template blocking occurrence init (500), and exact-instant occurrence
  matching (normalized to UTC). Tool count: 72 → 85.
- Project phases (read-only, OpenProject 16.1+): `list_project_phase_definitions`
  (the instance-wide phase catalog with start/finish gates) and
  `get_project_phase` (one project's phase record). Work-package details carry a
  `project_phase` reference — the id-producing path, since the API has no phases
  index — and `list_projects` gains `in_phase`/`phase_on_date`, a date-based
  filter ("projects in Executing today") resolved against the definition
  catalog by id or name. Phase dates themselves are not exposed by the API and
  the tools say so. Tool count: 85 → 87.
- `fetch_all` on `list_work_packages`, `list_projects` and
  `list_time_entries`: aggregate every page into one result instead of paging
  manually. Capped at 500 items — the cap is reported in `notes` and
  `has_more` stays honest about anything left unfetched; server-side `groups`
  and `sums` still describe the full filtered set.

## [0.1.2] - 2026-08-11

### Added

- `glama.json` maintainer manifest so the Glama MCP directory
  (glama.ai) can associate the listing with this repository.
- Listing icon (`assets/icon.svg` + `assets/icon-512.png`) referenced from
  `server.json` via `icons`, and `websiteUrl` pointing at the README — the
  registry entry now renders with an icon in MCP directories.
- README: a "What it looks like" worked example near the top — two synthetic
  transcript excerpts showing tool calls with their structured results and a
  server-rendered `weekly_report` prompt.
- MCP registry listing: `server.json` manifest
  (`io.github.kar-thik/openproject-mcp-server`) and a Release-workflow job that
  republishes the registry entry on every tag via GitHub OIDC — no stored
  secrets, mirroring the PyPI Trusted Publishing model.

## [0.1.1] - 2026-08-04

### Added

- Python 3.14 trove classifier: the full test suite has been green on CPython
  3.14 in CI since before 0.1.0; the classifier now says so.
- README: an "Updating or rotating your API token" section directly after the
  install instructions — rotating via an OS-environment export (keeps the
  secret out of client configs entirely), via `claude mcp remove`/`add`, via
  JSON-configured clients, and via `.env`; includes the tell-tale symptom of a
  server upgrade invalidating tokens (every call failing with
  `authentication_failed` / HTTP 401) and zero-downtime rotation using
  parallel tokens.

### Changed

- README: the project is now explicitly aimed at the OpenProject **Community
  edition**. OpenProject's Enterprise edition ships its own built-in MCP
  integration; this server brings the same capability to self-hosted Community
  instances and continues to run against any edition, needing only the public
  API v3.

## [0.1.0] - 2026-07-30

### Added

- Initial release: 72 MCP tools for the OpenProject API v3 — work packages
  (search/list/get/create/update/delete), comments, relations, watchers,
  reminders and comment reactions, attachments (upload and download with
  virus-quarantine handling, a size cap and SHA-256 digests), git/GitHub/GitLab
  activity, projects and memberships, saved queries, notifications, time
  tracking, versions and sprints, people, metadata and schemas, meetings, wiki,
  news, documents, budgets, and reporting.
- 4 server-rendered prompts (`weekly_report`, `daily_standup`, `triage_inbox`,
  `groom_backlog`) and 3 resource templates (work package, project,
  attachment).
- stdio transport (default) and an HTTP transport that binds `127.0.0.1` by
  default and refuses to start unless `OPENPROJECT_MCP_AUTH_TOKENS` is set (or
  `OPENPROJECT_MCP_INSECURE=1` is passed explicitly, for local development
  only).
- Per-request bearer-token verification on the HTTP transport: every request
  must carry `Authorization: Bearer <token>` matching one of the configured
  `OPENPROJECT_MCP_AUTH_TOKENS` values (comma-separated, e.g. one token per
  client); missing or invalid tokens are rejected with a 401 and a
  `WWW-Authenticate` header. Token comparison is constant-time. A set-but-empty
  token list (for example `OPENPROJECT_MCP_AUTH_TOKENS=,`) refuses to start
  rather than serving an open endpoint.
- Deployment gating: `OPENPROJECT_MCP_READ_ONLY=1` serves read tools only,
  `OPENPROJECT_MCP_ADMIN_TOOLS=1` enables the three membership-write tools
  (hidden by default), and `OPENPROJECT_MCP_DISABLE` drops whole tool groups.
- Version-adaptive behavior across OpenProject 14 LTS through 17.x: the server
  probes the instance (internal comments, reactions, project favorites, filter
  dialects) and degrades explicitly instead of failing silently.

Configuration note: the server binds only the documented `OPENPROJECT_*` /
`OPENPROJECT_MCP_*` environment variable names. Bare unprefixed names (e.g.
`READ_TIMEOUT`, `API_KEY`) are ignored — pre-release git installs also read
some unprefixed names, so rename any such variables when upgrading.
