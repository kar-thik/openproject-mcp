# OpenProject MCP Server

<!-- mcp-name: io.github.kar-thik/openproject-mcp-server -->

[![PyPI](https://img.shields.io/pypi/v/openproject-mcp-server)](https://pypi.org/project/openproject-mcp-server/)
[![CI](https://github.com/kar-thik/openproject-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/kar-thik/openproject-mcp/actions/workflows/ci.yml)

An MCP ([Model Context Protocol](https://modelcontextprotocol.io/)) server for the
[OpenProject](https://www.openproject.org/) API v3. It gives Claude and any other MCP client
72 tools covering work packages, comments and relations, attachments, git/PR activity, projects,
saved queries, notifications, time tracking, versions, people and memberships, meetings, news,
documents, budgets and reporting — plus 4 report/workflow prompts and 3 resource templates.
Built on FastMCP 3.x and httpx (HTTP/2).

Design principles, all enforced in code:

- **Structured everything.** Every tool returns a typed model, so clients get an
  `outputSchema` and machine-readable `structuredContent`, not prose. Errors come back as a
  JSON envelope with a stable `type`, the upstream `http_status`, a `message` and a `hint`
  describing how to correct the call.
- **Honest degradation.** OpenProject instances differ by version, installed modules and
  permissions. Tools report what they could not see as in-band notes — a missing module yields
  an empty page with an explanation, never a fake success or a bare traceback.
- **Safe by default.** A read-only mode, admin-gated membership writes, per-group tool
  disabling, a `confirm=true` guard on every destructive tool, TLS always verified, and
  credentials that never appear in logs.
- **Version-adaptive.** Targets OpenProject 14 LTS through 17.x; API differences are detected
  by a lazy, cached feature probe instead of assumptions (see
  [Supported OpenProject versions](#supported-openproject-versions)).

## Requirements

- Python >= 3.12
- An OpenProject instance, version 14 LTS through 17.x
- An OpenProject API key: in OpenProject, go to **My account → Access tokens** and generate an
  API token

## Installation

The distribution name is `openproject-mcp-server`. It installs two identical console scripts,
`openproject-mcp-server` and `openproject-mcp`; the long form is canonical (an unrelated PyPI
package also installs a bin named `openproject-mcp`).

Run one-shot with [uv](https://docs.astral.sh/uv/), no install step:

```sh
uvx openproject-mcp-server
```

Or install persistently:

```sh
uv tool install openproject-mcp-server
# or
pip install openproject-mcp-server
```

The minimal configuration is two environment variables:

```sh
export OPENPROJECT_URL=https://openproject.example.com
export OPENPROJECT_API_KEY=your-api-key
```

Validate the configuration without starting the server:

```sh
openproject-mcp-server --check
```

`--check` verifies the configuration and exits; it does not contact your instance. Once
connected through a client, call the `get_instance_info` tool for a live end-to-end check.
When configuration is missing or invalid, the server prints the specific problems to stderr
and exits with code 2 — never a traceback.

### Claude Code

```sh
claude mcp add openproject \
  --env OPENPROJECT_URL=https://openproject.example.com \
  --env OPENPROJECT_API_KEY=your-api-key \
  -- uvx openproject-mcp-server
```

### Claude Desktop and other MCP clients

Add to `claude_desktop_config.json` (or your client's equivalent `mcpServers` config):

```json
{
  "mcpServers": {
    "openproject": {
      "command": "uvx",
      "args": ["openproject-mcp-server"],
      "env": {
        "OPENPROJECT_URL": "https://openproject.example.com",
        "OPENPROJECT_API_KEY": "your-api-key"
      }
    }
  }
}
```

### From source

```sh
git clone https://github.com/kar-thik/openproject-mcp
cd openproject-mcp
uv sync
uv run openproject-mcp-server
```

## Configuration

Configuration is entirely environment-driven. The table below is the authoritative reference:
the server binds **exactly these 25 names and no others**. Bare, unprefixed names such as
`READ_TIMEOUT` or `API_KEY` are deliberately ignored (a stray variable in your shell cannot
change or break the server), as is any other unknown variable. A `.env` file in the server's
working directory is read with the same names; real environment variables take precedence.
From-source users can start from
[`.env.example`](https://github.com/kar-thik/openproject-mcp/blob/main/.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `OPENPROJECT_URL` | — (required) | Instance root URL, e.g. `https://openproject.example.com`. A trailing `/api/v3` is tolerated and stripped. |
| `OPENPROJECT_API_KEY` | — (required*) | API key from **My account → Access tokens**. Sent as HTTP Basic `apikey:<token>`. |
| `OPENPROJECT_OAUTH_TOKEN` | unset | OAuth bearer token, as an alternative to the API key. *One of the two credentials is required. |
| `OPENPROJECT_MCP_ACCEPT_LANGUAGE` | unset | Sent as the `Accept-Language` header; OpenProject localizes validation messages accordingly. |
| `OPENPROJECT_MCP_CA_BUNDLE` | system trust store | Path to a CA bundle (PEM) for instances behind a private CA. TLS is always verified; there is deliberately no off switch. |
| `OPENPROJECT_MCP_READ_ONLY` | `false` | Serve read tools only: every write, destructive and admin tool is removed at startup. |
| `OPENPROJECT_MCP_ADMIN_TOOLS` | `false` | Expose the three admin-gated membership write tools (hidden by default). |
| `OPENPROJECT_MCP_DISABLE` | empty | Comma-separated group tags to remove whole tool groups at startup (see below). |
| `OPENPROJECT_MCP_INSECURE` | `false` | Allow `--transport http` to start without auth tokens. Local development only. |
| `OPENPROJECT_MCP_DOWNLOAD_DIR` | `./openproject-downloads` | Directory where `download_attachment` writes files (created if missing; default is relative to the server's working directory). |
| `OPENPROJECT_MCP_MAX_DOWNLOAD_MB` | `100` | Size cap for attachment downloads, in MiB. |
| `OPENPROJECT_MCP_CACHE_TTL` | `300` | TTL in seconds for the metadata cache (statuses, types, priorities, schemas). |
| `OPENPROJECT_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` (case-insensitive). |
| `OPENPROJECT_MCP_LOG_FORMAT` | `text` | `text` or `json`. Logs always go to stderr (stdout belongs to the stdio transport). |
| `OPENPROJECT_MCP_LOG_BODIES` | `false` | Log request/response bodies — only at DEBUG level, with credentials redacted. Development use only. |
| `OPENPROJECT_MCP_OTEL` | `false` | Reserved for OpenTelemetry tracing. Accepted but not yet wired to anything in this release; setting it produces no traces. |
| `OPENPROJECT_MCP_HTTP_HOST` | `127.0.0.1` | Bind address for `--transport http`. |
| `OPENPROJECT_MCP_HTTP_PORT` | `8000` | Port for `--transport http`. |
| `OPENPROJECT_MCP_AUTH_TOKENS` | unset | Comma-separated bearer tokens accepted by `--transport http` (e.g. one per client). Every request must carry `Authorization: Bearer <token>` with one of them; anything else gets a 401. Without it (and without `OPENPROJECT_MCP_INSECURE=1`), the HTTP transport refuses to start. See [Transports](#transports). |
| `OPENPROJECT_MCP_CONNECT_TIMEOUT` | `10` | Seconds to wait for a TCP/TLS connection to OpenProject. |
| `OPENPROJECT_MCP_READ_TIMEOUT` | `30` | Seconds to wait for response data. |
| `OPENPROJECT_MCP_WRITE_TIMEOUT` | `60` | Seconds to wait while sending request data (uploads). |
| `OPENPROJECT_MCP_POOL_TIMEOUT` | `5` | Seconds to wait for a free connection from the pool. |
| `OPENPROJECT_MCP_MAX_CONNECTIONS` | `10` | Connection pool size toward OpenProject. |
| `OPENPROJECT_MCP_MAX_RETRIES` | `3` | Retry budget for idempotent requests. |

Secrets (`OPENPROJECT_API_KEY`, `OPENPROJECT_OAUTH_TOKEN`, `OPENPROJECT_MCP_AUTH_TOKENS`) are
held in memory as Pydantic `SecretStr` values and are never written to logs; the
`Authorization` header is redacted in every log record.

### Limiting what the model can do

Three settings shrink the tool surface at startup (the tool list is fixed for the lifetime of
the process):

- `OPENPROJECT_MCP_READ_ONLY=1` serves only the 35 read tools.
- `OPENPROJECT_MCP_ADMIN_TOOLS=1` reveals the three membership write tools
  (`create_membership`, `update_membership`, `delete_membership`); they are hidden by default.
- `OPENPROJECT_MCP_DISABLE` drops whole groups to cut prompt cost, e.g.
  `OPENPROJECT_MCP_DISABLE=meetings,news`. The valid group tags are:
  `work_packages`, `wp_collaboration`, `attachments`, `git_activity`, `projects`, `queries`,
  `notifications`, `time_entries`, `versions`, `people`, `metadata`, `meetings`, `wiki`,
  `documents`, `budgets`, `news`, `reporting` — the same tags that head each section of the
  tool catalog below.

Independent of all three, every destructive tool (the eight permanent deletes) requires an
explicit `confirm=true` argument before it acts.

## Transports

- **stdio** (default) — what Claude Code, Claude Desktop and most local MCP clients use.
  Logs go to stderr; stdout carries only the protocol.
- **Streamable HTTP** — `openproject-mcp-server --transport http`. Binds `127.0.0.1:8000` by
  default; override with `--host`/`--port` or `OPENPROJECT_MCP_HTTP_HOST`/
  `OPENPROJECT_MCP_HTTP_PORT`. The HTTP transport refuses to start without authentication
  configured: set `OPENPROJECT_MCP_AUTH_TOKENS` to a comma-separated list of bearer tokens,
  or — for local development only — set `OPENPROJECT_MCP_INSECURE=1`.

  With tokens configured, every HTTP request to the MCP endpoint must carry an
  `Authorization: Bearer <token>` header whose token matches one of the configured values
  (compared in constant time); requests with a missing, malformed or unknown token are
  rejected with a 401 and a `WWW-Authenticate` header. Multiple tokens are supported —
  for example one per client, so each can be revoked independently. Tokens must be ASCII,
  and the endpoint applies no rate limiting, so use long random values — for example
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
  `OPENPROJECT_MCP_INSECURE=1` remains a development-only escape hatch that leaves the
  endpoint open without authentication.

  The server does not terminate TLS, so tokens would otherwise cross the network in
  plaintext: keep the default `127.0.0.1` bind, and if you must expose the HTTP transport
  beyond localhost, put it behind a TLS-terminating reverse proxy (for example nginx or
  Caddy).

## Tools

72 tools: 35 read, 34 write and 3 admin-gated writes. The admin tools stay hidden unless
`OPENPROJECT_MCP_ADMIN_TOOLS=1`; the 8 destructive tools additionally require `confirm=true`
on every call. Each section heading names the group tag accepted by
`OPENPROJECT_MCP_DISABLE`.

### Work packages (`work_packages`)

| Tool | Kind | What it does |
|---|---|---|
| `search_work_packages` | Read | Find work packages by text when you do not know their ids. |
| `list_work_packages` | Read | List work packages with structured filters — the workhorse read tool. |
| `get_work_package` | Read | Read one work package in full: description, dates, custom fields, parent and progress. |
| `create_work_package` | Write | Create a work package, validated through OpenProject's own form endpoint first. |
| `update_work_package` | Write | Change any writable field of a work package, with optimistic locking. |
| `delete_work_package` | Write (destructive) | Permanently delete a work package and everything attached to it. |

### Comments, relations, watchers, reminders (`wp_collaboration`)

| Tool | Kind | What it does |
|---|---|---|
| `list_work_package_comments` | Read | Read the comment thread and change history of a work package. |
| `add_work_package_comment` | Write | Post a comment on a work package. |
| `edit_work_package_comment` | Write | Rewrite the text of an existing work-package comment. |
| `add_work_package_watcher` | Write | Subscribe a user to a work package's notifications. |
| `remove_work_package_watcher` | Write | Unsubscribe a user from a work package's notifications. |
| `create_work_package_relation` | Write | Link two work packages (blocks, follows, duplicates, relates, ...). |
| `update_work_package_relation` | Write | Change an existing relation's type, lag or description. |
| `delete_work_package_relation` | Write (destructive) | Remove the link between two work packages. |
| `toggle_comment_reaction` | Write | React to a work-package comment with an emoji, or take the reaction back. |
| `set_work_package_reminder` | Write | Set, change or clear your personal reminder on a work package. |
| `list_reminders` | Read | List your own upcoming work-package reminders. |
| `execute_custom_action` | Write | Run an instance-defined one-click action on a work package. |

### Attachments and file links (`attachments`)

| Tool | Kind | What it does |
|---|---|---|
| `list_attachments` | Read | List the files attached to one container. |
| `download_attachment` | Read | Download an attachment's bytes to a file on the machine running this server. |
| `upload_attachment` | Write | Attach a local file to a work package, wiki page, meeting, document, budget or comment. |
| `delete_attachment` | Write (destructive) | Permanently delete one attached file from OpenProject. |
| `list_file_links` | Read | List the external-storage files (Nextcloud, OneDrive/SharePoint) linked to a work package. |

### Git and pull requests (`git_activity`)

| Tool | Kind | What it does |
|---|---|---|
| `get_work_package_git_activity` | Read | Show the code behind a work package: commits, pull/merge requests and CI status. |
| `get_github_pull_request` | Read | Read one linked GitHub pull request in full, including its CI check runs. |

### Projects (`projects`)

| Tool | Kind | What it does |
|---|---|---|
| `list_projects` | Read | List projects, filtered server-side, one page at a time. |
| `get_project` | Read | Read one project in full. |
| `create_project` | Write | Create a project, validated through OpenProject's own form endpoint first. |
| `update_project` | Write | Change a project's name, description, visibility, parent, status or archived state. |
| `delete_project` | Write (destructive) | Schedule the permanent deletion of a project and everything inside it. |
| `copy_project` | Write | Copy a project — its settings, and optionally its work packages — into a new one. |
| `get_job_status` | Read | Check whether a background job (a project copy, a scheduled deletion) has finished. |
| `set_project_favorite` | Write | Add or remove a project from the authenticated user's favorites (OpenProject 17+). |

### Saved queries (`queries`)

| Tool | Kind | What it does |
|---|---|---|
| `list_queries` | Read | List the saved work-package views (queries) this user can open. |
| `run_query` | Read | Run a saved view and get its work packages as they are right now. |
| `save_query` | Write | Save a filter set as a reusable OpenProject view the whole team can open. |

### Notifications (`notifications`)

| Tool | Kind | What it does |
|---|---|---|
| `list_notifications` | Read | Read the authenticated user's OpenProject inbox. |
| `mark_notifications` | Write | Mark specific notifications read (or unread) in one bulk request. |
| `mark_all_notifications_read` | Write | Mark everything matching the filters as read — the whole inbox by default. |

### Time tracking (`time_entries`)

| Tool | Kind | What it does |
|---|---|---|
| `list_time_entries` | Read | List logged time, filtered server-side, with an optional accurate total. |
| `log_time` | Write | Book time against a work package or a project. |
| `update_time_entry` | Write | Correct an existing time entry. |
| `delete_time_entry` | Write (destructive) | Permanently delete a logged time entry. |

### Versions and sprints (`versions`)

| Tool | Kind | What it does |
|---|---|---|
| `list_versions` | Read | List versions (releases, milestones, sprints) you can assign work packages to. |
| `create_version` | Write | Create a version (release, milestone or sprint) inside a project. |
| `update_version` | Write | Change a version's name, dates, description, status or sharing. |
| `delete_version` | Write (destructive) | Permanently delete a version. |

### People and memberships (`people`)

| Tool | Kind | What it does |
|---|---|---|
| `search_principals` | Read | Find users, groups and placeholder users, and get their ids. |
| `get_user` | Read | Read one user's profile: name, login, email, admin flag and status. |
| `list_memberships` | Read | List who has access to which project, and with which roles. |
| `create_membership` | Admin write | Grant a principal one or more roles in a project. |
| `update_membership` | Admin write | Replace the roles of an existing membership. |
| `delete_membership` | Admin write (destructive) | Revoke a principal's access to a project. |
| `list_roles` | Read | List the roles this instance defines, with their ids. |

The three membership write tools require `OPENPROJECT_MCP_ADMIN_TOOLS=1` (and an OpenProject
account with the Manage members permission).

### Instance metadata and schemas (`metadata`)

| Tool | Kind | What it does |
|---|---|---|
| `get_instance_info` | Read | Check the OpenProject connection and report what this instance supports. |
| `get_project_metadata` | Read | List the ids and names (types, statuses, priorities, ...) that are actually valid on this instance. |
| `get_work_package_schema` | Read | Show which fields a work package of this type accepts in this project. |
| `list_permissions` | Read | List what the authenticated user is allowed to do, globally or in one project. |

### Meetings (`meetings`)

| Tool | Kind | What it does |
|---|---|---|
| `list_meetings` | Read | List meetings: what is coming up, what already ran. |
| `get_meeting` | Read | Read one meeting in full: participants, the agenda, and any recorded outcomes. |
| `create_meeting` | Write | Schedule a meeting in a project and optionally invite participants. |
| `add_meeting_agenda_item` | Write | Add one item to a meeting's agenda, optionally pinned to a work package. |

### Wiki (`wiki`)

| Tool | Kind | What it does |
|---|---|---|
| `get_wiki_page` | Read | Read a wiki page's identity and project — not its content (API v3 does not expose page bodies). |

### Documents (`documents`)

| Tool | Kind | What it does |
|---|---|---|
| `list_documents` | Read | List the documents visible to you, across every project. |
| `get_document` | Read | Read one document with its full description text. |

### Budgets (`budgets`)

| Tool | Kind | What it does |
|---|---|---|
| `list_budgets` | Read | List a project's budgets — their ids and names, which is all API v3 exposes. |

### News (`news`)

| Tool | Kind | What it does |
|---|---|---|
| `list_news` | Read | List project news — the announcements a team publishes on its project overview. |
| `get_news` | Read | Read one news entry in full, including the markdown body. |
| `create_news` | Write | Publish a news announcement in a project. |
| `update_news` | Write | Correct or rewrite a published news entry. |
| `delete_news` | Write (destructive) | Permanently delete a news entry. |

### Reporting (`reporting`)

| Tool | Kind | What it does |
|---|---|---|
| `get_project_report_data` | Read | Aggregate everything a status report needs about one project and one date window. |

## Prompts and resources

Four prompt templates render live OpenProject data into ready-to-use briefings:

- **weekly_report** — a weekly status report for one project: done / in progress / planned,
  hours and impediments.
- **daily_standup** — today's standup for one project: yesterday's movement, what is due
  today, and what is blocked.
- **triage_inbox** — groups your unread notifications by reason and suggests actions.
- **groom_backlog** — sweeps a project's open backlog for unassigned, stale and unestimated
  work.

Three resource templates expose OpenProject objects at stable URIs:

- `openproject://work_package/{id}` — one work package as JSON.
- `openproject://project/{identifier}` — one project as JSON (identifier slug or numeric id).
- `openproject://attachment/{id}` — the attachment's bytes with the detected MIME type.

## Supported OpenProject versions

The server targets OpenProject **14 LTS through 17.x**. Instead of assuming one API dialect,
it probes the instance lazily on first need and caches the result for an hour. The
version-dependent surfaces:

- **Internal (private) comments** need OpenProject >= 16. Older servers silently ignore the
  internal flag, so below 16 the server refuses with a clear error rather than posting a
  comment publicly that you asked to keep internal.
- **Emoji reactions** (`toggle_comment_reaction`) need OpenProject >= 16; detected from the
  version, tolerant of a 404 at call time.
- **Project favorites** (`set_project_favorite`) need OpenProject >= 17; same tolerance.
- **Time-entry filters**: the work-package filter is probed (`entityId`, falling back to the
  pre-15.x `workPackage` spelling).
- **Permission contexts** (`list_permissions`): the context prefix is probed (`p{id}`, falling
  back to the `w{id}` spelling introduced in 17.2).
- **Meetings time filter**: OpenProject 17.6 changed the wire dialect; `list_meetings`
  discovers which spelling the instance accepts and caches it.

Features that depend on optional instance modules (meetings, news, documents, budgets, wiki,
backlogs, GitHub/GitLab integration, external storages) degrade honestly when the module is
absent: list tools return an empty page with an in-band note naming the missing module, and
detail tools return a structured error explaining both possible readings of the 404.

## Security

- The API key and OAuth token are held as `SecretStr` and never logged; `Authorization` and
  cookie headers are redacted from every log record. Request/response bodies are only logged
  at DEBUG level with `OPENPROJECT_MCP_LOG_BODIES=1`, for development.
- The `Authorization` header is stripped whenever a redirect leaves the OpenProject origin —
  attachment downloads redirect to presigned object-storage URLs, and the credential must not
  travel there.
- TLS is always verified. Private CAs are supported via `OPENPROJECT_MCP_CA_BUNDLE`; an
  insecure-TLS switch deliberately does not exist.
- Attachment downloads respect OpenProject's virus scanner: quarantined files are never
  fetched and produce a structured `attachment_quarantined` error. Downloads are capped by
  `OPENPROJECT_MCP_MAX_DOWNLOAD_MB`, stored under sanitized file names (directory separators
  and traversal sequences are neutralized), and reported with a SHA-256 of the bytes.
- Destructive tools require `confirm=true` and are annotated so clients can ask the user
  first.
- The HTTP transport refuses to start without `OPENPROJECT_MCP_AUTH_TOKENS` configured and
  verifies the `Authorization: Bearer` header on every request — missing or invalid tokens
  are rejected with a 401, and token comparison is constant-time. The server does not
  terminate TLS: keep the default `127.0.0.1` bind, and put a TLS-terminating reverse proxy
  in front if the port must be reachable from anywhere else (see [Transports](#transports)).

## Troubleshooting

| Symptom | What it means and what to do |
|---|---|
| Server exits with "cannot start, configuration is incomplete" | Run `openproject-mcp-server --check` and fix the problems it lists; the variable reference is the [Configuration](#configuration) section above. |
| 401 authentication_failed | The API key is wrong, revoked, or belongs to a blocked account. Generate a fresh token under **My account → Access tokens**. |
| 403 permission_denied | The account is authenticated but lacks a role permission for that project, or the relevant module is disabled for it. |
| Empty list plus a note about a missing module | That OpenProject module (meetings, news, documents, ...) is not installed or not enabled in the project — the empty result is the honest answer, not an error. |
| TLS certificate errors | Your instance uses a private CA: point `OPENPROJECT_MCP_CA_BUNDLE` at its PEM bundle. There is no option to disable verification. |
| `--transport http` refuses to start | Set `OPENPROJECT_MCP_AUTH_TOKENS` (comma-separated bearer tokens), or `OPENPROJECT_MCP_INSECURE=1` for local development only — see [Transports](#transports). |
| 401 from the MCP HTTP endpoint itself | The request's `Authorization: Bearer <token>` header is missing or does not match any configured `OPENPROJECT_MCP_AUTH_TOKENS` entry. Check the client's token for typos; distinct from the upstream `401 authentication_failed` above, which is about the OpenProject API key. |
| Proxies | Standard `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` variables are honored by the underlying HTTP client. |

When reporting an issue, please include your OpenProject version, the output of
`openproject-mcp-server --version`, and the output of `openproject-mcp-server --check`.

## Development

```sh
git clone https://github.com/kar-thik/openproject-mcp
cd openproject-mcp
uv sync --group dev
uv run pytest              # entire suite runs offline (respx-mocked HTTP), no instance needed
uv run ruff check .
uv run ruff format --check .
uv run pyright             # strict mode
```

The suite currently contains no tests that need a live OpenProject instance; an opt-in
`integration` marker is registered and reserved for any that are added later. The
full technical specification lives in
[SPEC.md](https://github.com/kar-thik/openproject-mcp/blob/main/SPEC.md).

Releasing is documented in
[CONTRIBUTING.md](https://github.com/kar-thik/openproject-mcp/blob/main/CONTRIBUTING.md).

## License and trademark

MIT — see [LICENSE](https://github.com/kar-thik/openproject-mcp/blob/main/LICENSE).

This is a community project. It is not affiliated with, endorsed by, or supported by
OpenProject GmbH. "OpenProject" is a trademark of OpenProject GmbH and is used here only to
indicate interoperability.
