# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is 0.x, MINOR releases may change the MCP tool surface
(tools renamed or removed, parameter or result-envelope semantics changed);
PATCH releases are fixes and strictly additive changes only.

## [Unreleased]

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
