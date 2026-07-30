# OpenProject MCP Server — Technical Specification

**Status:** Draft v2 (adversarially reviewed) · 2026-07-26
**Repo:** `openproject-mcp` (from-scratch rewrite; supersedes `../openproject-mcp-server`)
**Language:** Python ≥ 3.12 · **Framework:** FastMCP 3.x · **Target OpenProject:** 14 LTS → 17.x (version-probed, §4.7)

All OpenProject API claims in this document were verified against the OpenProject source tree at 17.7-dev (`../openproject`), including module code (`modules/*/lib/api`), and the old server's defects against its source (`../openproject-mcp-server`).

---

## Table of contents

1. [Why a rewrite](#1-why-a-rewrite)
2. [Goals, non-goals, guarantees](#2-goals-non-goals-guarantees)
3. [Architecture](#3-architecture)
4. [OpenProject API client layer](#4-openproject-api-client-layer)
5. [Tool design principles](#5-tool-design-principles)
6. [Tool catalog](#6-tool-catalog)
7. [Attachments & file handling](#7-attachments--file-handling)
8. [Git / development activity](#8-git--development-activity)
9. [Filters, search & pagination contract](#9-filters-search--pagination-contract)
10. [MCP resources, prompts & server instructions](#10-mcp-resources-prompts--server-instructions)
11. [Security](#11-security)
12. [Observability](#12-observability)
13. [Testing strategy](#13-testing-strategy)
14. [Packaging & distribution](#14-packaging--distribution)
15. [Implementation phases](#15-implementation-phases)
16. [Migration from the old server](#16-migration-from-the-old-server)
17. [Defect ledger — old bugs → design guarantees](#17-defect-ledger)
18. [Explicitly out of scope](#18-explicitly-out-of-scope)
19. [Open questions](#19-open-questions)

---

## 1. Why a rewrite

A full audit of `../openproject-mcp-server` (62 registered tools, FastMCP 2.13, aiohttp) found the codebase unsalvageable as a foundation. Representative findings (full ledger in §17):

**Architecture**
- A new `aiohttp.ClientSession` + TLS context is created **per request** (`src/client.py:82-88`) — no pooling, no retries, hardcoded 30 s timeout. `PERFORMANCE_OPTIMIZATIONS.md` documents pooling/caching/bulk tools that **do not exist in the code** (zero grep hits).
- Global mutable client constructed at **module import time**; missing env vars crash the import. Tool modules circularly import the server module.
- Every tool returns emoji-decorated **markdown prose**, never structured data. All ~60 `except Exception` handlers flatten errors into strings; MCP `isError` is never used. Ten bare `except:` clauses hide failures, five of them silently defaulting `lockVersion = 0`, which can **clobber concurrent edits**.
- The HAL `_embedded.elements` back-fill block is copy-pasted **15×**; the lockVersion read-before-write is copy-pasted 8× with inconsistent failure handling.
- Dead code everywhere: a 3,213-line legacy monolith in the repo root, an auth module never wired into any transport (HTTP serves all 62 tools on `0.0.0.0` **unauthenticated**), a Dockerfile whose `CMD` references a file that doesn't exist, a `requirements.txt` that can't run the server.

**Capability gaps**
- **No `get_work_package` detail tool** — the single biggest read gap; the client method exists but was never exposed. There is no way to read a WP description.
- **Zero coverage** of: attachments (upload/download), git/repository data, saved queries, notifications, custom fields, watchers, wiki, meetings, documents, schemas/forms, capabilities, reminders, custom actions.
- Unpaginated `list_projects`, `list_users`, `list_time_entries`, `list_memberships` → silent truncation; "Total Hours" sums only the first page.
- Instance-specific values hardcoded as universal: priority id 3 = "High", time-entry activity ids 1–4, Vietnamese-only report template whose classification logic is, ironically, English-keyword-only — so it works on **neither** locale.
- Outright bugs: `list_user_projects` passes a JSON filter string as `project_id`; the weekly report calls a client method that doesn't exist (`AttributeError` swallowed by `except: pass` — dependency analysis silently never worked); `unassign_work_package` builds href `/api/v3/users/None`; `create_work_package` silently drops `status_id`; `create_version` silently drops `due_date`.

**What we keep** (the genuinely valuable domain knowledge):
- Basic-auth scheme `apikey:<token>`; the form-endpoint flow for creation; lockVersion discipline (concept, not implementation); the filter incantations (`subjectOrId **`, `status o/*`, `spentOn <>d`, `parent`/`ancestor`); the status-code → hint table idea; the 8-section Agile weekly-report structure and prompt library (as parameterized MCP prompts, EN default); the tool scope as a field-tested statement of demand.

---

## 2. Goals, non-goals, guarantees

### Goals
1. **Comprehensive, honest coverage** of OpenProject API v3: work packages (full lifecycle incl. comments, watchers, relations, hierarchy, custom fields, reminders, custom actions), projects (incl. copy), attachments **with real file download/upload**, git/dev activity (SCM revisions + GitHub PRs + GitLab MRs), saved queries (run + save), notifications, time tracking, versions, memberships (users/groups **read**; membership write behind an admin gate), meetings, wiki/news/documents/budgets, schemas & capabilities.
2. **LLM-first ergonomics**: structured JSON outputs with schemas, compact token-efficient projections, explicit pagination, actionable typed errors that let the model self-correct, honest tool annotations, no id-consuming tool without an id-producing path.
3. **Production quality**: pooled async HTTP, retries with backoff, typed error taxonomy, optimistic-locking done right, metadata caching, observable, in-memory-tested, uv-packaged, `uvx`-runnable.
4. **Safe by default**: read-only deployment mode, destructive-action guards, no secrets in logs, HTTP transport refuses to start without auth.

### Non-goals
- Not a general OpenProject admin console (no user/group CRUD, backups, instance settings, LDAP, enumeration management — §18).
- No repository *browsing* (files/diffs/branches) — OpenProject does not expose it via API v3 (verified against source; §8).
- No BIM/BCF, boards/grids manipulation, or Rails-session-only features.
- Performance is *sufficiency*, not a KPI — MCP servers are not latency-critical; correctness and token economy dominate.

### Behavioral guarantees
- **G1 — No silent truncation.** Every list result reports `total`, `page`, `page_size`, `has_more`. Any internally-capped aggregation or capped `include` reports what was dropped (`{"truncated": true, "total": N}`).
- **G2 — No silent field drops.** Unknown/unwritable input fields produce an error naming the field (validated against the resource schema), never a silent no-op. Parameters that the connected OpenProject version does not support (e.g. `internal` comments pre-16.0) produce an error, never silent downgrade.
- **G3 — No fabricated defaults.** No instance-specific IDs baked in; enumerations are discovered per instance and cached.
- **G4 — Errors are data.** Every failure is a structured tool error (`is_error=true`) with machine-usable fields; raw upstream HTML is never echoed.
- **G5 — Feature detection over assumption.** Module-provided endpoints are probed (§4.7); absence degrades into an explicit `notes`/`available` marker in the result, not an exception and not a silent narrowing. Any degraded search/aggregation says so in-band.
- **G6 — Writes are never auto-retried.** Reads retry on 429/5xx/network; writes surface the failure.

---

## 3. Architecture

### 3.1 Stack

| Concern | Choice | Rationale |
|---|---|---|
| MCP framework | **FastMCP ≥ 3.4, < 4** (standalone, PrefectHQ) | Official `mcp` SDK is mid-v2-rewrite (class rename, module churn; GA slated ~2026-07-28). FastMCP 3.x is GA, security-patched, has auth providers, middleware, tag filtering, in-memory test client. Same protocol underneath; dropping to the official SDK later remains possible. |
| MCP protocol | negotiate **2025-06-18** as floor; support **2025-11-25** | Structured output & resource links need ≥ 2025-06-18; 2025-11-25 adds elicitation/enums we use progressively. |
| HTTP client | **httpx** with `h2` extra (`httpx[http2]>=0.28`) | Single pooled `AsyncClient` in FastMCP lifespan; `respx` for tests. |
| Models | **Pydantic v2** | Input validation (FastMCP derives JSON Schema), output models (`outputSchema` + `structuredContent`), typed HAL projections. |
| Config | pydantic-settings + env vars | No import-time side effects; lazy validation. |
| Python | ≥ 3.12 | Modern typing, `asyncio.TaskGroup`. |
| Packaging | uv, PyPI, `uvx openproject-mcp` | §14. |

### 3.2 Layout

```
openproject_mcp/
  __main__.py        # console entry → cli (transport select, env checks)
  server.py          # FastMCP app assembly: lifespan, registration, instructions, middleware
  config.py          # Settings (pydantic-settings)
  version_probe.py   # instance version + module/feature detection (§4.7)
  client/
    http.py          # OpenProjectClient: pooled httpx, auth, retries, error mapping
    errors.py        # typed exception taxonomy ↔ structured tool errors
    hal.py           # HAL parsing: link→Ref, collection unwrap, id-from-href
    payloads.py      # typed _links payload builders (assignee, status, parent, CFs…)
    filters.py       # filter grammar builder + validation (§9)
    locking.py       # lockVersion fetch/echo + 409 conflict shaping
    cache.py         # TTL cache for near-static metadata
  tools/
    work_packages.py   wp_collaboration.py   projects.py   attachments.py
    git_activity.py    queries.py   notifications.py   time_entries.py
    versions.py        people.py    metadata.py   modules_collab.py   news.py
    reporting.py
  resources.py       # resource templates (openproject://…)
  prompts.py         # weekly_report, standup, triage… (EN default, locale param)
  projections.py     # compact output models (WorkPackageRow, WorkPackageDetail, Ref…)
tests/
  unit/  (respx-mocked client + tools)
  protocol/ (in-memory FastMCP Client end-to-end)
  fixtures/ (golden HAL payloads; 17.x primary set + 14 LTS set where obtainable, §13.3)
  integration/ (optional, docker-compose OpenProject, marked slow)
```

Tool modules are plain `def register(mcp: FastMCP) -> None` functions called from `server.py` — explicit registration, no import side effects, no circular imports. Every tool is tagged `read` | `write` | `admin` | `destructive` plus a group tag (`work_packages`, `meetings`, …). Deployment filtering: `OPENPROJECT_MCP_READ_ONLY=1` (only `read`), `OPENPROJECT_MCP_ADMIN_TOOLS=1` (enables membership-write tools), `OPENPROJECT_MCP_DISABLE=meetings,news` (drop whole groups to cut prompt cost). The tool set is fixed at startup; the server does **not** emit `tools/list_changed` (stated explicitly so client behavior is predictable).

### 3.3 Transports

- **stdio** (default): single-user; credentials from env. Logging strictly to stderr.
- **streamable HTTP** (`--transport http`): binds `127.0.0.1` by default; **refuses to start without an auth provider configured** unless `OPENPROJECT_MCP_INSECURE=1` is set explicitly (dev only). §11.
- No SSE transport — deprecated in both the MCP spec and Claude clients.

### 3.4 Lifespan & state

FastMCP lifespan creates: `Settings` → `OpenProjectClient` (one `httpx.AsyncClient`) → version probe → metadata `TTLCache`. Tools receive them via lifespan context. Nothing global, nothing at import time; the server object is constructable with zero env vars (needed for tests and `--help`).

---

## 4. OpenProject API client layer

### 4.1 Requests

- One `httpx.AsyncClient(base_url=f"{url}/api/v3", http2=True, timeout=Timeout(connect=10, read=30, write=60, pool=5), limits=Limits(max_connections=10))`.
- Auth: HTTP Basic `apikey:<token>` (default) or `Authorization: Bearer <oauth-token>` — pluggable `httpx.Auth`. Never logged; `SecretStr` end-to-end.
- Query params via httpx `params=` (never hand-built f-strings). `filters` serialized by the filter builder (§9).
- **Retries (reads only, G6):** up to 3 attempts on 429 (honoring `Retry-After`), 502/503/504, and transport errors; exponential backoff with jitter (0.5 s → 4 s).
- Per-call timeout override for downloads/uploads (no read timeout on streamed bodies; total wall-clock cap instead).
- Outbound proxy honored via standard `HTTPS_PROXY`/`ALL_PROXY` env (httpx native) — no bespoke proxy plumbing.
- Optional `Accept-Language` from `OPENPROJECT_MCP_ACCEPT_LANGUAGE` — OpenProject localizes validation messages; they pass through as-is (whatever locale), our `hint` fields are always English.

### 4.2 Error taxonomy

```python
OpenProjectError               # base; carries http_status, error_identifier, message, hint
├── AuthenticationError        # 401 — "check OPENPROJECT_API_KEY / token validity"
├── PermissionDeniedError      # 403 — includes required-permission hint when derivable
├── NotFoundError              # 404 — distinguishes bad-id vs module-not-enabled where possible
├── ValidationFailedError      # 422 — parses OP error body: errorIdentifier urn, message,
│                              #   _embedded.details / multiple-errors → [{attribute, message}]
├── ConflictError              # 409 — stale lockVersion; carries fresh resource snapshot
├── RateLimitedError           # 429 — retry_after seconds
├── UpstreamServerError        # 5xx — body discarded, never echoed (G4)
└── NetworkError               # DNS/timeout/TLS — actionable hint (URL typo, VPN, proxy)
```

**Mechanism** (spec-conformant): a shared decorator catches the taxonomy and returns `ToolResult(is_error=True)` (FastMCP ≥ 3.4) whose text content is the JSON error envelope below. `structuredContent` is **not** set on errors — MCP requires `structuredContent` to conform to the declared `outputSchema`, which describes the success shape only.

```json
{
  "error": {
    "type": "validation_failed",
    "http_status": 422,
    "error_identifier": "urn:openproject-org:api:v3:errors:PropertyConstraintViolation",
    "message": "Subject can't be blank.",
    "violations": [{"attribute": "subject", "message": "can't be blank"}],
    "hint": "Provide a non-empty 'subject'. Use get_work_package_schema to see required fields."
  }
}
```

The 401/403/404/409/422/429/5xx → hint mapping preserves (and fixes) the old server's one good idea. Validation errors always carry enough to self-correct: required fields, allowed values (from the form/schema response) when the API provides them.

### 4.3 HAL handling

- `hal.py` provides `Ref` extraction (`{"id", "name", "href"}` from any `_links` entry; id parsed from href tail), collection unwrap (`_embedded.elements`, `total`, `count`, `pageSize`, `offset`), and formattable-field handling (`{format, raw, html}` → we surface `raw` and drop `html`).
- **Canonical output Ref is `{id, name}`** — hrefs are internal to `hal.py` and never appear in tool output. Every projection includes the resource's **own `id`** (so sibling tools can consume it — activity ids for `edit_work_package_comment`, relation ids for `delete_work_package_relation`, etc.).
- **One** implementation, property-based-tested — replacing the 15 copy-pasted back-fill blocks.

### 4.4 Optimistic locking (`locking.py`)

- PATCH flows: use caller-supplied `lock_version` if given; otherwise GET current, echo it. A fetch failure **aborts the write** (never defaults to 0).
- On 409: re-fetch, return `ConflictError` with the current `lock_version` and a compact diff of the conflicting fields so the model can retry deliberately.

### 4.5 Forms & schemas

- Creation/update of work packages, projects, memberships, versions, time entries, meetings go through the API's **form endpoints**: `POST …/form` → surface `validationErrors` into `ValidationFailedError`, use returned `payload` defaults, then commit. (The old server fetched the form and threw the validation away.)
- Schemas power custom-field discovery: `/api/v3/work_packages/schemas/{project}-{type}` → field names, types, `writable`, `allowedValues` (resolved to `Ref` lists). Cached (TTL 300 s; `refresh=true` param on metadata tools).

### 4.6 Metadata cache

TTL cache (default 300 s, configurable) for: API root, `/configuration`, `/types`, `/statuses`, `/priorities`, `/roles`, per-project types/versions/categories, WP schemas, time-entry activities, `users/me`, version-probe results (§4.7, TTL 1 h). lockVersions are never cached. Cache is per-credential (multi-user HTTP mode keys by principal).

### 4.7 Version & feature probing

The API surface genuinely differs across supported versions (verified via source history):

| Feature | Availability | Probe/strategy |
|---|---|---|
| Time-entry **work-package filter name** | ≤ ~15.x: `workPackage`; current: `entityId` + `entityType` (the old filter was removed 2025-05) | Try `entityId`; on 400 invalid-filter, fall back to `workPackage` (result cached) |
| `internal` comments (write) | ≥ 16.0 (2025-05); older servers **silently ignore** the param | Core version from `/api/v3` root; `internal=true` on older → hard error (G2) |
| Emoji reactions | ≥ 16.x | 404-tolerant; feature flag in probe |
| Project favorites API | ≥ 17.x | 404-tolerant Ⓜ tool |
| Project phases / workspaces / portfolios / programs | ≥ 17.x | Out of v1 scope (§18) |
| Capabilities context filter | `g` (global) / `p{id}`; `p` deprecated upstream in favor of `w{id}` from 17.2 | Send `p{id}`; on rejection retry `w{id}` (cached) |
| Notification `dateAlert` reason filter | Enterprise-gated (`date_alerts` token) | Pass through the API's 400 with a hint |
| Module endpoints (github/gitlab/meetings/documents/budgets/backlogs/storages) | Instance-dependent | Ⓜ tools: 404 → `"unavailable (module not installed)"`; 403 → `"no permission"` (G5) |

The probe runs lazily on first need, is cached 1 h, and its result is included in `get_instance_info` output so the model can see what this instance supports.

---

## 5. Tool design principles

1. **Structured output everywhere.** Every tool declares a Pydantic return model → MCP `outputSchema` + `structuredContent` (FastMCP emits a text fallback automatically). No emoji prose. Rich text fields return markdown `raw`.
2. **Compact projections.** List tools return `WorkPackageRow` (id, subject, type, status, priority, assignee, project, start_date, due_date, percentage_done, updated_at — as `{id, name}` Refs, hrefs dropped). Detail tools add description and custom fields. Target: a 20-row WP list ≤ ~1.5k tokens. (Implementation note: the API's `select=` sparse-fieldset mode exists but exposes too few properties for `WorkPackageRow` — compaction happens client-side in `projections.py`; `select=` may be used for id-only probes.)
3. **Consolidation over multiplication.** The old server spent ~15 tools on strict subsets of `list_work_packages` while covering none of attachments/git/queries/notifications. Convenience queries are **parameters**, not tools (migration recipes in §16).
4. **Honest annotations.** `readOnlyHint` on all reads; `destructiveHint` on all 🗑 tools; `idempotentHint` where true; `openWorldHint: true`. Claude-specific: `anthropic/requiresUserInteraction` on **every** 🗑 tool; `anthropic/maxResultSizeChars` on known-large reads (`list_work_package_comments`, `get_project_report_data`, `run_query`).
5. **Destructive guards.** 🗑 tools require `confirm=true` (validated in-body with an explanatory error). Elicitation is used when the client supports it; parameter fallback otherwise.
6. **Names an LLM can pick.** `verb_noun` with consistent full prefixes (`…_work_package_comment`, `…_work_package_relation`, `…_work_package_watcher`). Descriptions written like skill descriptions: what it does, when to use it, what it returns, pitfalls, and **cross-references** ("for linked PRs/commits/CI status use `get_work_package_git_activity`; for the comment thread use `list_work_package_comments`").
7. **IDs and humans.** Tools accept numeric ids everywhere; project params also accept the string `identifier`; `type`/`status`/`priority` write params accept **name or id** (resolved via cached metadata; ambiguity or miss → typed error listing valid values). Principal params accept `"me"` where the API does (WP filters, time entries — **not** capabilities, §6.1).
8. **Dates** ISO `YYYY-MM-DD`; datetimes ISO 8601 UTC. Durations returned as float hours, never `PT7H30M`. Sort keys are snake_case and mapped to wire camelCase; unknown keys → error listing allowed keys.
9. **Long-running tools** (paged aggregations, large downloads/uploads) emit MCP **progress notifications** between pages/chunks (resets client idle timeouts) and honor **cancellation** between iterations.
10. **Every id-consuming tool has an id-producing path**, stated in its description (e.g. `github_pull_request_id` comes from `get_work_package_git_activity`, never the GitHub PR number).

---

## 6. Tool catalog

Legend: 🔍 read · ✏️ write · 🗑 destructive (confirm + `requiresUserInteraction`) · ⚙️ admin-gated · Ⓜ module/version-dependent (probed, G5). Ph → §15.

**Count: 72 tools — Ph1: 16 · Ph2: 33 · Ph3: 23.** (Vs the old server's 62: strictly more *capability*; the count is honest, not the sales pitch. Deployments trim via tag filters, §3.2.) A CI check asserts this table always equals the registered tool set (§13.5).

### 6.1 Instance & identity (Ph1: 1 · Ph2: 1)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `get_instance_info` | — | `GET /`, `/configuration`, `/users/me` | 1 |
| 🔍 `list_permissions` | `project_id?, permission?` | `GET /capabilities?filters=…` | 2 |

`get_instance_info`: core version, instance name, `maximumAttachmentFileSize`, feature-probe summary (§4.7), current user (id, name, login, admin). Doubles as the connection test.

`list_permissions` (né `check_permissions` — renamed; the old tool returned a user profile and called it permissions): resolves the **numeric** principal id via cached `users/me` (the capabilities API has **no** `"me"` value), filters context as `g` (global) or `p{id}` (with `w{id}` fallback, §4.7). Optional `permission?` returns a `{allowed: bool}` predicate. Description carries the API's own caveat: **only a subset of actions is exposed** — absence of a capability is not proof of missing permission.

### 6.2 Work packages — core (Ph1: 6)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `search_work_packages` | `query, project_id?, mode='quick'\|'fulltext', status_scope='all', page, page_size` | `GET /work_packages` + `typeahead`/`search` filter | 1 |
| 🔍 `list_work_packages` | see below | `GET /work_packages` or `/projects/{id}/work_packages` | 1 |
| 🔍 `get_work_package` | `id, include?: [relations, watchers, attachments, children, custom_actions, meetingsⓂ]` | `GET /work_packages/{id}` (+ sub-resources) | 1 |
| ✏️ `create_work_package` | `project, type, subject, description?, …, custom_fields?, attachment_paths?` | `POST /work_packages/form` → `POST /work_packages` | 1 |
| ✏️ `update_work_package` | `id, lock_version?, …any writable field…, custom_fields?` | form → `PATCH /work_packages/{id}` | 1 |
| 🗑 `delete_work_package` | `id, confirm` | `DELETE /work_packages/{id}` | 1 |

**Status scoping — explicit and uniform.** OpenProject's server default (open-only when no filter is sent) is never relied on: both tools always send an explicit status filter derived from `status_scope`. `search_work_packages` defaults to `'all'` (finding closed items is the point of search); `list_work_packages` defaults to `'open'`. Each default is stated in the tool description. `status_ids` overrides `status_scope` (documented; no silent fight).

`search_work_packages` modes (verified filter compositions): `quick` → `typeahead **` (subject + project name + type/status name + id — what the UI header search runs); `fulltext` → `search **` (subject + description + **comments** + searchable CFs, plus attachment content/filename when the instance's Postgres has TSV). When TSV is unavailable the result carries `notes: ["attachment content not searched on this instance"]` (G5) — comment search is core and always included.

`list_work_packages` typed filters: `project` (id | identifier), `query?` (text, `search` filter AND-combined with the rest), `status_scope`, `status_ids`, `type_ids`, `priority_ids`, `assignee` (ids, `"me"`, or `"none"` → `!*`), `author`, `responsible`, `version_ids`, `parent_id`, `top_level_only` (→ `parent !*`), `ancestor_id` (subtree), `milestones_only`, `due_before/after`, `start_before/after`, `created_since`, `updated_since`, `percentage_done_min/max`, `watcher`, plus `raw_filters?` (typed escape hatch, §9.2), `sort_by` (snake_case pairs, server-side), `group_by?`, `show_sums=false`, `page`, `page_size`. Open-ended date ranges use `<>d` with an empty bound — no sentinel dates.

`get_work_package` returns full detail: description (markdown raw), all core fields, custom fields (§6.2.1), parent Ref, availability flags for dev-links/meetings/files, and requested includes. **Every include is capped at 20 items** with a G1 marker (`{"truncated": true, "total": 500}`) and a pointer to the full-listing tool (`list_work_packages(parent_id=…)` for children; relations via the relations filter). `custom_actions` include lists the instance-defined one-click actions available on this WP (feeds `execute_custom_action`).

`create_work_package`: form-validated; supports `status` (name or id — fixing the silent drop), milestone types (single `date`), `parent_id`, and `attachment_paths` — files upload **uncontainered** (`POST /attachments`) and are claimed via `_links.attachments` on create (the API-correct flow; uploads to `…/{id}/attachments` need *edit* permission which a fresh author may lack).

`update_work_package`: any writable field; `assignee=null` clears via `{"href": null}` (fixing `/users/None`); status changes validated via form so an invalid transition error lists allowed target statuses; `parent_id` set/clear here (no separate hierarchy tools).

#### 6.2.1 Custom fields — canonical shape (both directions)

- **Read** (in `get_work_package`, and in `get_work_package_schema`):
  `custom_fields: [{"key": "customField12", "name": "Severity", "type": "list", "value": "High", "value_ids": [4]}]` — always a list, always both `key` and `name`, plain `value` for scalars, `value_ids` alongside resolved names for list/user/version CFs.
- **Write** (in create/update): `custom_fields: {"<key or exact name>": <value>}` — keys may be `customField12` **or** the display name; resolved via the cached schema; ambiguous or unknown names → typed error listing valid keys. List-CF values accept option ids or option names (same resolution rules).

### 6.3 Work packages — collaboration (Ph1: 2 · Ph2: 6 · Ph3: 4)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `list_work_package_comments` | `id, page, page_size=10, max_comment_chars=2000, activity_id?` | `GET /work_packages/{id}/activities` | 1 |
| ✏️ `add_work_package_comment` | `id, comment, notify=true, internal=false` | `POST /work_packages/{id}/activities` | 1 |
| ✏️ `edit_work_package_comment` | `activity_id, comment` | `PATCH /activities/{id}` | 2 |
| ✏️ Ⓜ `toggle_comment_reaction` | `activity_id, reaction` (enum of 8) | `PATCH /activities/{id}/emoji_reactions` | 3 |
| ✏️ `add_work_package_watcher` | `work_package_id, user_id` | `POST /work_packages/{id}/watchers` | 2 |
| ✏️ `remove_work_package_watcher` | `work_package_id, user_id` | `DELETE /work_packages/{id}/watchers/{uid}` | 2 |
| ✏️ `create_work_package_relation` | `from_id, to_id, type, lag?, description?` | `POST /work_packages/{id}/relations` | 2 |
| ✏️ `update_work_package_relation` | `relation_id, …` | `PATCH /relations/{id}` | 2 |
| 🗑 `delete_work_package_relation` | `relation_id, confirm` | `DELETE /relations/{id}` | 2 |
| ✏️ `set_work_package_reminder` | `work_package_id, remind_at \| null, note?` | `GET/POST /work_packages/{id}/reminders`, `PATCH/DELETE /reminders/{id}` | 3 |
| 🔍 `list_reminders` | — (own upcoming) | `GET /reminders` | 3 |
| ✏️ `execute_custom_action` | `custom_action_id, work_package_id, lock_version?` | `POST /custom_actions/{id}/execute` | 3 |

`list_work_package_comments` — named for what agents ask for ("read the comments"); returns the full activity journal: comment entries (full text up to `max_comment_chars`, per-item `truncated` marker per G1; `activity_id?` fetches one entry uncapped via `GET /activities/{id}`) and field-change entries parsed to `{field, from, to}`. **The upstream endpoint is unpaginated** — the server fetches the full journal and pages client-side (stated here so nobody "optimizes" it into a lie). Only comment-type activities are editable (noted in `edit_work_package_comment`).

`add_work_package_comment(internal=true)` requires OpenProject ≥ 16 — on older versions the API **silently ignores** the flag, so we hard-error instead (G2/§4.7). Relation `type` is a real enum (`relates, precedes, follows, blocks, blocked, duplicates, duplicated, includes, partof, requires, required`); `lag` only valid on `follows`/`precedes` (validated locally). `set_work_package_reminder` upserts (OpenProject allows one active reminder per WP — a 409 becomes an update); `remind_at=null` deletes. Reaction enum: `thumbs_up, thumbs_down, grinning_face_with_smiling_eyes, confused_face, heart, party_popper, rocket, eyes`.

### 6.4 Attachments (Ph1: 3 · Ph2: 1 · Ph3: 1) — semantics in §7

| Tool | Sig (abridged) | Ph |
|---|---|---|
| 🔍 `list_attachments` | `container_type ('work_package'\|'wiki_page'\|'meeting'\|'document'\|'budget'\|'comment'), container_id` | 1 |
| 🔍 `download_attachment` | `attachment_id, save_dir?, return_image=false` | 1 |
| ✏️ `upload_attachment` | `container_type, container_id, file_path, file_name?, description?` | 1 |
| 🗑 `delete_attachment` | `attachment_id, confirm` | 2 |
| 🔍 Ⓜ `list_file_links` | `work_package_id` (incl. `staticOriginOpen`/`staticOriginDownload` URLs) | 3 |

Attachment ids are produced by `list_attachments` and `get_work_package(include=[attachments])`. (`forum_post` is a valid API container but has no discovery path in this server — excluded, §18.)

### 6.5 Git / development activity (Ph2: 2) — semantics in §8

| Tool | Sig (abridged) | Ph |
|---|---|---|
| 🔍 Ⓜ `get_work_package_git_activity` | `work_package_id, include?: [revisions, github, gitlab]` | 2 |
| 🔍 Ⓜ `get_github_pull_request` | `github_pull_request_id` | 2 |

`github_pull_request_id` is the **OpenProject-internal id** returned by `get_work_package_git_activity` (present on every PR object), **not** the GitHub PR number — stated in both descriptions.

### 6.6 Projects (Ph1: 2 · Ph2: 3 · Ph3: 3)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `list_projects` | `search?, active=true, parent_id?, favorites_only?, sort_by?, page, page_size` | `GET /projects` (paginated; `name_and_identifier`/`typeahead` filters) | 1 |
| 🔍 `get_project` | `id_or_identifier` | `GET /projects/{id}` | 1 |
| ✏️ `create_project` | `name, identifier?, description?, parent_id?, public?, status_code?` | form → `POST /projects` | 2 |
| ✏️ `update_project` | `id, …, status_code?, status_explanation?` | form → `PATCH /projects/{id}` | 2 |
| 🗑 `delete_project` | `id, confirm` | `DELETE /projects/{id}` — **async** (deletion is scheduled; tool returns the scheduled state, not "gone") | 2 |
| ✏️ `copy_project` | `id, new_name, …` | `POST /projects/{id}/copy` → async job | 3 |
| 🔍 `get_job_status` | `job_id` | `GET /job_statuses/{uuid}` — poll copy/export/delete jobs | 3 |
| ✏️ Ⓜ `set_project_favorite` | `id, favorite: bool` | `POST/DELETE /projects/{id}/favorite` (≥ 17.x) | 3 |

`status_code` enum: `on_track, at_risk, off_track, not_started, finished, discontinued` — written via the `status` link (fixing the old free-string that silently failed).

### 6.7 Saved queries (Ph2: 2 · Ph3: 1)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `list_queries` | `project_id?, page, page_size` | `GET /queries?filters=[project]` | 2 |
| 🔍 `run_query` | `query_id, page?, page_size?, override_filters?` | `GET /queries/{id}` (embeds results; params override stored props) | 2 |
| ✏️ `save_query` | `name, filters, project_id?, public=false, star=false, sort_by?, group_by?` | form → `POST /queries` (+ `/star`) | 3 |

`run_query` leverages run-on-read semantics — users' saved views become directly usable. `override_filters` is typed identically to `raw_filters` (§9.2). Grouped/summed results use the §9.3 envelope extensions. Query update/delete: excluded (§18).

### 6.8 Notifications (Ph2: 3)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `list_notifications` | `unread_only=true, reason?, project_id?, page, page_size` | `GET /notifications` (`readIAN`/`reason`/`project` filters) | 2 |
| ✏️ `mark_notifications` | `ids (required, non-empty), read=true` | `POST /notifications/{id}/read_ian\|unread_ian` | 2 |
| ✏️ `mark_all_notifications_read` | `reason?, project_id?` | bulk `POST /notifications/read_ian?filters=…` | 2 |

Two tools instead of one union-shaped footgun: `mark_notifications` requires explicit ids; the mass operation is a separately-named tool, marks **read only** (the safe direction), and its description says it affects everything matching the (optional) filters. Reason enum (verified wire values): `mentioned, assigned, responsible, watched, subscribed, commented, created, processed, prioritized, scheduled, shared, reminder, dateAlert` — `dateAlert` filtering is Enterprise-gated (`date_alerts` token); the API's rejection passes through with a hint (§4.7).

### 6.9 Time tracking (Ph2: 4)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `list_time_entries` | `work_package_id?, project_id?, user? ('me'), from_date?, to_date?, activity_id?, sum_hours=false, page, page_size` | `GET /time_entries` — `spentOn <>d`; WP filter name version-probed (§4.7) | 2 |
| ✏️ `log_time` | `work_package_id? \| project_id?` (≥ 1 required), `hours, spent_on, activity?, comment?` | form → `POST /time_entries` | 2 |
| ✏️ `update_time_entry` | `id, …` | `PATCH /time_entries/{id}` | 2 |
| 🗑 `delete_time_entry` | `id, confirm` | `DELETE /time_entries/{id}` | 2 |

`sum_hours=true` pages through all matches (cap 2,000 entries; cap-hit reported per G1) and returns an accurate total. `activity` accepts name or id; omitted → instance default from the form; allowed activities appear in validation errors and `get_project_metadata`. Project-level entries (no WP) are supported — `work_package_id` is optional with `project_id`.

### 6.10 Versions / sprints (Ph2: 4)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `list_versions` | `project_id?, include_sprintsⓂ=false` | `GET /versions` or `/projects/{id}/versions` (+ `/projects/{id}/sprints` Ⓜ) | 2 |
| ✏️ `create_version` | `project_id, name, start_date?, end_date?, description?, status?, sharing?` | form → `POST /versions` | 2 |
| ✏️ `update_version` | `id, …` | `PATCH /versions/{id}` | 2 |
| 🗑 `delete_version` | `id, confirm` | `DELETE /versions/{id}` | 2 |

`end_date` maps to API `endDate` (the old server's tool/client disagreed and the value vanished).

### 6.11 People & access (Ph2: 7)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `search_principals` | `query?, type? ('user'\|'group'\|'placeholder'), member_of_project?, status?, page, page_size` | `GET /principals` (typeahead/name filters) | 2 |
| 🔍 `get_user` | `id \| 'me'` | `GET /users/{id}` | 2 |
| 🔍 `list_memberships` | `project_id?, principal_id?, page, page_size` | `GET /memberships` | 2 |
| ⚙️✏️ `create_membership` | `project_id, principal_id, role_ids, notify_message?` | form → `POST /memberships` | 2 |
| ⚙️✏️ `update_membership` | `membership_id, role_ids` | `PATCH /memberships/{id}` | 2 |
| ⚙️🗑 `delete_membership` | `membership_id, confirm` | `DELETE /memberships/{id}` | 2 |
| 🔍 `list_roles` | `include_permissions=false` | `GET /roles` | 2 |

Users and groups are **read-only** (`search_principals` covers group-id discovery for memberships); user/group CRUD and locking are out of scope (§18). The `⚙️` gate (`OPENPROJECT_MCP_ADMIN_TOOLS=1`) covers exactly the three membership-write tools. `list_roles` returns `{id, name}` by default; `include_permissions=true` opts into the full permission arrays (token bomb otherwise).

### 6.12 Metadata & schemas (Ph1: 2)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 `get_project_metadata` | `project_id?, refresh=false` | `/projects/{id}/types`, `/statuses`, `/priorities`, `/projects/{id}/versions`, `/projects/{id}/categories`, TE activities via form | 1 |
| 🔍 `get_work_package_schema` | `project_id, type_id, refresh=false` | `GET /work_packages/schemas/{p}-{t}` | 1 |

`get_project_metadata` without `project_id` returns the instance-global sets (types, statuses, priorities, roles) — so cross-project filtering never requires picking an arbitrary project first. With `project_id` it adds project-scoped types, versions, categories, and time-entry activities. This is the one-call answer to "what ids/names do I use here". `get_work_package_schema` exposes writable fields, required flags, and allowed values (statuses, categories, versions, CF options as `{id, name}`).

### 6.13 Collaboration modules Ⓜ (Ph3: 13)

| Tool | Sig (abridged) | Endpoint(s) | Ph |
|---|---|---|---|
| 🔍 Ⓜ `list_meetings` | `project_id?, upcoming_only=true, page, page_size` | `GET /meetings` | 3 |
| 🔍 Ⓜ `get_meeting` | `id` (incl. agenda items + outcomes + participants) | `GET /meetings/{id}` + `/agenda_items` | 3 |
| ✏️ Ⓜ `create_meeting` | `project_id, title, start_time, duration, participants?` | form → `POST /meetings` | 3 |
| ✏️ Ⓜ `add_meeting_agenda_item` | `meeting_id, title, notes?, duration_minutes?, work_package_id?` | `POST /meeting_agenda_items` | 3 |
| 🔍 `get_wiki_page` | `id` — id comes from a UI URL the user supplies; APIv3 has **no wiki index/search**, and page *content* is not exposed (title + attachments only) — both stated in the description | `GET /wiki_pages/{id}` | 3 |
| 🔍 `list_news` | `project_id?, page, page_size` | `GET /news` | 3 |
| 🔍 `get_news` | `id` (full description) | `GET /news/{id}` | 3 |
| ✏️ `create_news` | `project_id, title, summary?, description?` | `POST /news` | 3 |
| ✏️ `update_news` | `id, …` | `PATCH /news/{id}` | 3 |
| 🗑 `delete_news` | `id, confirm` | `DELETE /news/{id}` | 3 |
| 🔍 Ⓜ `list_documents` / `get_document` | `page…` / `id` | `GET /documents(…/{id})` | 3 |
| 🔍 Ⓜ `list_budgets` | `project_id` | `GET /projects/{id}/budgets` | 3 |

(`list_documents`/`get_document` count as two tools.) "Where was this WP discussed?" → `get_work_package(include=[meetings])` uses `GET /work_packages/{id}/meeting_agenda_items` Ⓜ. Meeting update/delete, outcomes/sections write, recurring meetings, and document update: excluded (§18).

### 6.14 Reporting (Ph3: 1)

| Tool | Sig (abridged) | Ph |
|---|---|---|
| 🔍 `get_project_report_data` | `project_id, from_date, to_date` | 3 |

Structured JSON aggregation powering the report prompts: WPs created/updated/closed in window (correct `createdAt`/`updatedAt` `<>d` + `status c` filters — no 30-day fudge), open-by-status counts (`groupBy=status` server-side), time entries in window with per-activity totals, membership roster. Internal caps: 3,000 WPs and 5,000 time entries per window — cap-hits reported in-band (G1). Status bucketing uses each status's **`isClosed` flag from the API**, never name keywords (the old EN-keyword classifier broke on every localized instance). Rendering (weekly report, standup) is done by **prompts** (§10), parameterized `locale='en'|'vi'|…` — preserving the valued 8-section Agile template without hardcoding Vietnamese into code.

---

## 7. Attachments & file handling

Semantics verified against OpenProject source (17.7-dev).

### 7.1 Download

`GET /api/v3/attachments/{id}` is metadata-only. Bytes come from `GET /api/v3/attachments/{id}/content` (absent from the OpenAPI doc but stable; it is `staticDownloadLocation` in the HAL body), which either:
- **302-redirects to a presigned S3/fog URL** (self-authorizing, ~6 h expiry) — followed **without forwarding the Authorization header** (explicit httpx event hook; leaking Basic credentials to the object store is the classic bug here), or
- **streams bytes** with `Content-Disposition` and `X-Content-Type-Options: nosniff` (local storage).

`download_attachment` flow:
1. GET metadata → `fileName`, `fileSize`, `contentType`, `status`. `quarantined` → structured error (virus quarantine); pending-scan files 401 for non-authors → clear hint.
2. Stream to disk with a wall-clock cap and size cap (`OPENPROJECT_MCP_MAX_DOWNLOAD_MB`, default 100). Progress notifications per chunk (§5.9).
3. Target dir: explicit `save_dir` → validated against MCP **roots** when provided, else `OPENPROJECT_MCP_DOWNLOAD_DIR`; default `<first root or cwd>/openproject-downloads/`. Filename sanitized; collisions uniquified; traversal in `fileName` neutralized.
4. Returns `{path, file_name, size_bytes, content_type, sha256}`. With `return_image=true`, images ≤ 1 MB additionally return an MCP `ImageContent` block so the model can *see* them.

**Transport caveat:** path-based download/upload assumes the server shares a filesystem with the user (stdio). In multi-user **HTTP** mode these tools switch behavior: `download_attachment` returns the attachment as an MCP resource/blob (§10) or the short-lived `downloadLocation` URL instead of writing files; `upload_attachment`/`attachment_paths` are disabled with an explanatory error. Stated in tool descriptions at runtime.

### 7.2 Upload

Plain multipart is universal (the S3 "prepare" direct-upload flow is optional instance config — skipped in v1, noted as future optimization):

- `POST /api/v3/{container}/{id}/attachments`, exactly two parts: `metadata` (JSON: `fileName`, optional `description`) and `file` (bytes). The server ignores the part filename and client content type (re-detects from bytes) — `file_name` drives naming via metadata only.
- Containers (verified mounts): work_package, wiki_page, meeting, document, budget, activity comment, plus **uncontainered** `POST /api/v3/attachments`.
- **New-WP flow:** uncontainered upload + claim via `_links.attachments` in the create payload (`create_work_package(attachment_paths=…)`). Unclaimed uploads are purged by OpenProject after ~180 min, so failures don't leak junk.
- Pre-flight: file existence, size vs `maximumAttachmentFileSize` (cached `/configuration`) → local error before burning the upload. 422 allowlist violations surfaced with the whitelist hint.

---

## 8. Git / development activity

Ground truth from source — what OpenProject can and cannot tell us:

| Source | Endpoint | Payload highlights |
|---|---|---|
| SCM (git/svn repos attached to a project; commits referencing WPs via `refs #123` etc. in the message) | `GET /work_packages/{id}/revisions` | `identifier` (full SHA), `formattedIdentifier`, `authorName`, `message.raw`, `createdAt`, UI URL |
| GitHub module (PRs linked via `OP#123` / WP URL in PR body or comments) | `GET /work_packages/{id}/github_pull_requests`; `GET /github_pull_requests/{id}` | number, title, state, draft, merged/mergedAt, htmlUrl, repo, additions/deletions/changedFiles, comment counts, labels, author, **check runs** (name/status/conclusion/htmlUrl) — CI status |
| GitLab module | `GET /work_packages/{id}/gitlab_merge_requests`, `…/gitlab_issues` | like GitHub minus diff counts, plus **pipelines** (status, commitId, job details) |

`get_work_package_git_activity` fans out to all requested sources concurrently and returns:

```json
{
  "available": {"revisions": true, "github": true, "gitlab": false},
  "revisions": [...],
  "github_pull_requests": [{"id": 17, "number": 481, "title": "...", "state": "closed",
      "merged": true, "check_runs": [{"name": "ci/test", "status": "completed",
      "conclusion": "success"}], ...}],
  "gitlab_merge_requests": [], "gitlab_issues": [],
  "notes": ["gitlab: not available on this instance (module absent)"]
}
```

Every PR/MR object carries the **internal `id`** (input to `get_github_pull_request`) alongside the GitHub/GitLab-side `number`.

**Availability detection (corrected against source):** the `github_pull_requests`/`gitlab_*` links on the WP are rendered **unconditionally** whenever the bundled plugin is loaded — they do *not* signal permission. The permission-gated signals are the `github`/`gitlab` **tab** links (`show_github_content`/`show_gitlab_content`) and, for SCM, the `revisions` link (`view_changesets`). Strategy: use the gated links as the primary signal; on the actual GET, map 403 → `"no permission"`, 404 → `"module not installed"` — reported per-source in `notes`, never as a hard failure (G5).

**Not promised** (absent from API v3, verified): repository/file/diff/branch browsing, commit lists per project, revision→changed-files, creating PR↔WP links via API (webhook/commit-message ingestion only), standalone check-run/pipeline resources, GitLab push-commit history (push events surface only as journal comments). Tool descriptions state the linking magic words (`refs #123` in commit messages, `OP#123` or the WP URL in PR/MR descriptions) so the model can teach users how to make data appear.

---

## 9. Filters, search & pagination contract

### 9.1 The grammar (internal)

`filters.py` owns serialization of the API's JSON filter array `[{"name": {"operator": op, "values": [...]}}]` with the verified operator vocabulary (`=`, `!`, `*`, `!*`, `~`, `!~`, `**`, `o`, `c`, `t`, `w`, `t-`, `t+`, `<t+`, `>t+`, `>t-`, `<t-`, `=d`, `<>d`, `>=`, `<=`, `&=`, relation ops) and per-type operator validation mirroring the server's strategy classes — invalid combos fail **locally** with the allowed set listed. Key gotchas, encoded once:

- Omitted `filters` ⇒ the server defaults to **open WPs only**. Our tools always send explicit status filters (§6.2), so this default never bites.
- OpenProject's `offset` param is a **1-based page number**, not a record offset — we expose `page`/`page_size` and map directly.
- Boolean values are `"t"`/`"f"`; WP/TE principal filters accept `"me"` (capabilities does not); open-ended `<>d` ranges leave a bound empty.
- Wire names are camelCase (`assignee`, `dueDate`, `updatedAt`, `subprojectId`, `customField{N}`); all tool params and sort keys are snake_case, mapped centrally.

### 9.2 The escape hatch

Typed params cover the 95% case. `raw_filters` covers the rest — **typed, not stringly**: `list[{name: str, operator: str, values: list[str]}]` (a JSON-in-a-string parameter invites escaping failures; a typed array schema doesn't). Same shape for `run_query.override_filters`. Custom-field filters go through `raw_filters` with `customField{N}` names (discovered via `get_work_package_schema`). Available filter names/operators per resource are discoverable via the API's own `queries/filter_instance_schemas` — referenced in the tool description.

### 9.3 Result envelope (every list tool)

```json
{
  "items": [...],
  "pagination": {"total": 137, "page": 2, "page_size": 20, "has_more": true},
  "groups": [{"value": "In progress", "count": 12, "sums": {"estimated_hours": 41.5}}],
  "sums": {"estimated_hours": 220.0},
  "notes": ["attachment content not searched on this instance"]
}
```

- `groups`/`sums` present only when `group_by`/`show_sums` were requested (populated from the API's `groups`/`totalSums`; groups are computed server-side over the **full** filtered set, independent of pagination — stated so the model doesn't sum pages).
- `notes` carries G5 degradation markers.
- Defaults `page_size=20`, max 100 (the server may clamp lower via `apiv3_max_page_size`; we report actual counts).
- Small fetched-in-full collections (`list_versions`, `list_roles`, `list_reminders`, `list_attachments`, `list_file_links`, metadata) return the same envelope with `has_more: false` — one shape everywhere.

---

## 10. MCP resources, prompts & server instructions

**Resource templates** (Claude Code: `@openproject:` mentions):
- `openproject://work_package/{id}` → WP detail projection (JSON)
- `openproject://project/{identifier}` → project overview
- `openproject://attachment/{id}` → attachment bytes as a blob resource (the HTTP-mode download path, and a no-tool option for resource-aware clients)

**Prompts** (→ Claude Code slash commands):
- `weekly_report(project, from_date?, to_date?, locale='en', team_name?)` — calls `get_project_report_data` and renders the 8-section Agile/Scrum report (Done/InProgress/Planned by `isClosed`, capacity from time entries, impediments from `blocks` relations). The Vietnamese template survives as `locale='vi'`.
- `daily_standup(project)` — yesterday's activity + due-today + blockers.
- `triage_inbox()` — unread notifications grouped by reason with suggested actions.
- `groom_backlog(project)` — unassigned/unestimated/stale WP sweep.

**Server `instructions`** (load-bearing for Claude Code tool-search): one paragraph — manages OpenProject (work packages, projects, files, git links, time, meetings, notifications); ids are discoverable via `search_*`/`get_project_metadata`; `list_work_packages` defaults to open items only; destructive tools need `confirm`.

**Elicitation** (progressive): destructive confirms and missing-required-field prompts when the client supports it; parameter/error fallback otherwise. **No sampling** dependence (unsupported in Claude clients). Icons metadata: nice-to-have, harmless.

---

## 11. Security

- **Credentials**: `OPENPROJECT_API_KEY` as `SecretStr`; never in logs (Authorization redacted; body logging is opt-in `OPENPROJECT_MCP_LOG_BODIES=1`, dev-only). TLS verified always; `OPENPROJECT_MCP_CA_BUNDLE` for private CAs (no verify-off switch).
- **Redirect hygiene**: Authorization stripped on cross-origin redirects (presigned URLs) — explicit httpx event hook, unit-tested.
- **HTTP transport**: requires a FastMCP auth provider (static bearer set, JWT verifier, or OAuth proxy — FastMCP 3.x built-ins). Binds localhost by default. Multi-user mode maps the **verified MCP principal → per-user OpenProject API key** (`user_keys.toml`) or forwards an OAuth token — the shared-impersonation-key anti-pattern is not reproduced. Without auth configured, HTTP mode exits with an error naming the required env vars. Filesystem-coupled tools switch to resource/blob mode (§7.1).
- **Blast-radius controls**: `OPENPROJECT_MCP_READ_ONLY=1` (drops all ✏️/🗑/⚙️), `OPENPROJECT_MCP_ADMIN_TOOLS` (membership writes), `OPENPROJECT_MCP_DISABLE` (group opt-outs), `confirm` + `requiresUserInteraction` on every 🗑 tool.
- **Output hygiene**: upstream error bodies never echoed verbatim (G4); HTML stripped; attachment filenames sanitized before filesystem use.

---

## 12. Observability

- **Structured logging to stderr** (JSON lines when `OPENPROJECT_MCP_LOG_FORMAT=json`, human-readable otherwise). Levels via `OPENPROJECT_MCP_LOG_LEVEL` (default `INFO`).
- Per tool call: tool name, duration, upstream requests made (method + path + status + ms each), outcome (ok / error type). **Never** request/response bodies, filter values, or credentials at INFO; bodies only with `LOG_BODIES` at DEBUG.
- A per-call correlation id ties tool-call log lines to their upstream requests.
- **OpenTelemetry**: optional, via FastMCP 3.x native tracing (`OPENPROJECT_MCP_OTEL=1` + standard OTEL_* env). No custom instrumentation layer.
- MCP-level logging notifications (server → client) mirror WARN+ events so hosted clients see problems without server-side log access.

---

## 13. Testing strategy

1. **Unit** — client layer against `respx`: error taxonomy per status, retry/backoff (429 `Retry-After` honored; writes never retried), redirect auth-stripping, HAL property tests (hypothesis: id-from-href, collection unwrap), filter builder (every operator × type combo, golden URLs), lockVersion flows incl. 409 shaping, version-probe fallbacks (§4.7: `entityId`→`workPackage`, `p{id}`→`w{id}`, `internal` rejection).
2. **Protocol** — in-memory `fastmcp.Client(server)`: every tool callable end-to-end with mocked upstream; assertions on **result contents** (not just absence-of-error); `structuredContent` validates against declared `outputSchema`; annotations asserted (all reads `readOnlyHint`, all 🗑 `destructiveHint` + `requiresUserInteraction`); read-only mode hides writes; the §9.3 envelope on every list tool; progress notifications emitted by paging tools; cancellation honored between pages.
3. **Fixtures** — golden HAL payloads captured from a live 17.x instance (scrubbed), one per resource; a 14 LTS fixture set for the version-sensitive surfaces (time-entry filters, comments) — if a 14 instance is unobtainable, the risk is documented in the README rather than silently untested.
4. **Integration (opt-in, CI-nightly)** — docker-compose OpenProject; seeded project; smoke: search→get→create→comment→upload→download→log time→report data. Marked `-m integration`; never required for default `pytest` (the old repo's tests needed a live server and always exited 0 — both banned).
5. **Quality gates** — `uv run pytest` green; `ruff` + `ruff format`; **pyright strict on `client/` AND `tools/`** (the old server's nonexistent-method bug lived in a tool module); coverage ≥ 85% on `client/` + `tools/`; **doc-sync check**: the §6 catalog table, the README tool table, and the registered tool set must match (script compares them in CI).

---

## 14. Packaging & distribution

- `pyproject.toml` (uv-managed): `[project.scripts] openproject-mcp = "openproject_mcp.__main__:main"`. Pins: `fastmcp>=3.4,<4`, `httpx[http2]>=0.28`, `pydantic>=2.7`. PyPI → `claude mcp add openproject -- uvx openproject-mcp`; `server.json` for the MCP registry; `fastmcp.json` for declarative runs.
- Docker: multi-stage uv image, non-root, `CMD ["openproject-mcp", "--transport", "http", "--host", "0.0.0.0"]` (a CMD that exists), healthcheck on the MCP endpoint.
- Config surface (all env, documented in README + a shipped `.env.example`): `OPENPROJECT_URL`, `OPENPROJECT_API_KEY`, `OPENPROJECT_MCP_{READ_ONLY, ADMIN_TOOLS, DISABLE, DOWNLOAD_DIR, MAX_DOWNLOAD_MB, CACHE_TTL, CA_BUNDLE, INSECURE, LOG_LEVEL, LOG_FORMAT, LOG_BODIES, OTEL, ACCEPT_LANGUAGE}`, HTTP auth vars (provider-specific), plus standard `HTTPS_PROXY`/`ALL_PROXY`/`OTEL_*` passthrough.
- Docs: README (EN) with setup matrix (Claude Code/Desktop, stdio/HTTP), per-tool permission-requirements matrix (carried from the old README — it was accurate), troubleshooting (401/403/404/SSL/proxy), cookbook adapted from the old Vietnamese guides (translated), magic-words guide for git linking, migration guide (§16).

---

## 15. Implementation phases

Phase lists are generated from the §6 tables' Ph column (the CI doc-sync check keeps them equal).

**Phase 0 — skeleton.** Config, client (`http/errors/hal/filters/locking/cache`), version probe, error decorator, projections, server assembly, CI, test harness. No tools. Exit: in-memory protocol handshake green; client green against respx and one live smoke call.

**Phase 1 — the daily loop (16 tools).** `get_instance_info` · WP core 6 (`search/list/get/create/update/delete_work_package`) · `list_work_package_comments`, `add_work_package_comment` · attachments 3 (`list/download/upload_attachment`) · `list_projects`, `get_project` · `get_project_metadata`, `get_work_package_schema`. Exit: golden-path demo — find a WP, read it with comments, attach + download a file, create a subtask with custom fields set.

**Phase 2 — breadth (33 tools).** `list_permissions` · `edit_work_package_comment` · watchers 2 · relations 3 · `delete_attachment` · git 2 (`get_work_package_git_activity`, `get_github_pull_request`) · projects write 3 (`create/update/delete_project`) · queries 2 (`list_queries`, `run_query`) · notifications 3 · time 4 · versions 4 · people 7. Exit: parity-plus vs the old server on every non-module resource; integration suite green.

**Phase 3 — modules & polish (23 tools + UX).** `toggle_comment_reaction` · reminders 2 · `execute_custom_action` · `list_file_links` · `copy_project`, `get_job_status`, `set_project_favorite` · `save_query` · meetings 4 · `get_wiki_page` · news 5 · documents 2 · `list_budgets` · `get_project_report_data` · prompts + resources · HTTP multi-user auth mode · MCP registry publish.

Each phase lands as a PR train with protocol tests; tool descriptions reviewed against a "can the model pick the right tool blind?" checklist per phase.

---

## 16. Migration from the old server

Coexistence: the new server registers under a different MCP server name (`openproject` vs the old `openproject-mcp`), so both can run side-by-side during cutover.

**Env vars:** `OPENPROJECT_URL`, `OPENPROJECT_API_KEY` carry over unchanged. `OPENPROJECT_PROXY` → standard `HTTPS_PROXY`. `LOG_LEVEL` → `OPENPROJECT_MCP_LOG_LEVEL`. `MCP_API_KEYS` (never actually enforced by the old server) → real HTTP auth (§11).

**Tool mapping** (old → new; consolidated tools become parameter recipes):

| Old tool(s) | New equivalent |
|---|---|
| `test_connection`, `check_permissions` | `get_instance_info`; `list_permissions` (real capabilities) |
| `get_work_package` (legacy-only, lost in old src) | `get_work_package` |
| `list_overdue_work_packages` | `list_work_packages(due_before=<today>, sort_by=[["due_date","asc"]])` |
| `list_work_packages_due_soon(days=N)` | `list_work_packages(due_after=<today>, due_before=<today+N>)` |
| `list_unassigned_work_packages` | `list_work_packages(assignee="none")` |
| `list_work_packages_created_recently(days=N)` | `list_work_packages(created_since=<today−N>)` |
| `list_high_priority_work_packages` | `list_work_packages(priority_ids=…)` (ids from `get_project_metadata` — no hardcoded 3) |
| `list_work_packages_nearly_complete` | `list_work_packages(percentage_done_min=80)` |
| `assign_/unassign_work_package` | `update_work_package(assignee=…/null)` |
| `set_/remove_work_package_parent`, `list_work_package_children` | `update_work_package(parent_id=…/null)`; `list_work_packages(parent_id=…)` |
| `add_subproject`, `get_subprojects` | `create_project(parent_id=…)`; `list_projects(parent_id=…)` |
| `list_project_members`, `list_user_projects` (broken) | `list_memberships(project_id=… / principal_id=…)` |
| `list_users`, `get_user`, `list_roles`, `get_role` | `search_principals`, `get_user`, `list_roles(include_permissions=…)` |
| `list_types/statuses/priorities`, `list_time_entry_activities` | `get_project_metadata` |
| `list_work_package_activities` | `list_work_package_comments` (full text, paged) |
| `generate_weekly_report`, `generate_this/last_week_report`, `get_report_data` | `weekly_report` prompt + `get_project_report_data` |
| news 5, time 4, versions 2, relations 5, memberships 5 | same-shape equivalents (news gains `get_news`; versions gain update/delete; relation create gains local enum/lag validation) |

Dropped with rationale: none of the old server's *capabilities* are dropped; only redundant tool *names* are (recipes above).

---

## 17. Defect ledger

Every audited defect maps to a preventing design element:

| Old defect | Prevention here |
|---|---|
| Per-request ClientSession, no retries | Single lifespan `AsyncClient`; retry policy §4.1 |
| Fiction docs (PERFORMANCE_OPTIMIZATIONS.md) | CI doc-sync check: catalog == README == registered tools §13.5 |
| No `get_work_package` | Phase 1 tool §6.2 |
| String outputs, emoji, `isError` never used | Structured outputs §5.1; `ToolResult(is_error=True)` envelope §4.2 |
| Bare `except:` → lockVersion=0 clobbering | `locking.py` aborts on fetch failure; 409 → ConflictError §4.4 |
| Nonexistent `client.get_relations` swallowed forever | No blanket except; pyright strict on client/ **and** tools/ §13.5; protocol tests assert result contents §13.2 |
| `/users/None` unassign | `{"href": null}` payload builder, unit-tested |
| Silent drops (`status_id`, `due_date`) | G2: schema-validated inputs; form-backed writes §4.5 |
| Unpaginated lists, first-page sums | §9.3 envelope; `sum_hours` full paging with stated cap §6.9 |
| Hardcoded priority 3 / activity ids 1–4 | G3; `get_project_metadata` §6.12; name-or-id resolution §5.7 |
| Vietnamese-only template, EN-only classifier | Locale-param prompts §10; `isClosed`-flag classification §6.14 |
| Wrong filter ops (`spent_on >=`) | Central validated filter builder §9.1 |
| Unauthed 0.0.0.0 HTTP with shared key | §11: auth-or-refuse-to-start; per-user key mapping |
| 62 tools, ~15 redundant subsets | Orthogonal catalog §6; convenience = parameters; migration recipes §16 |
| Import-time env crash | Lazy settings; zero-env server construction §3.4 |
| Tests that can't fail / need a live server | §13: in-memory protocol tests default; integration opt-in |
| Version-blind API use | Version probe §4.7; hard errors over silent downgrade (G2) |

---

## 18. Explicitly out of scope

Each item is a deliberate exclusion, not an omission:

- **Repository browsing** (files/diffs/branches/blame) and per-project commit lists — not in API v3.
- **Creating commit/PR↔WP links via API** — ingestion is webhook/commit-scan only (§8).
- **User & group CRUD, user locking, placeholder-user CRUD** — admin-console territory; users/groups are read-only here.
- **Query update/delete** — `save_query` covers the create case; editing saved views stays in the UI.
- **Wiki page content & CRUD** — API v3 exposes only id/title/attachments; no index endpoint (the `get_wiki_page` description says where ids come from). Forum posts likewise (no discovery path) — `forum_post` attachment container excluded.
- **Meetings**: update/delete, sections/outcomes write, recurring meetings. **Documents**: update (PATCH exists; low value). Both revisitable on demand.
- **Storages**: remote file browsing, remote upload, folder creation, and file-link create/delete — all require per-user storage OAuth grants (browser flow) or origin metadata the model can't obtain; `list_file_links` (with open/download URLs) is the useful readable slice.
- **Workspaces / portfolios / programs / project phases** (17.x) — `list_projects` covers all workspace types transparently; dedicated tools deferred until 17.x is the deployed floor.
- **Days/working-calendar, user working-hours records, help texts, `my_preferences`, shares list, cost entries (`summarized_costs_by_type`), wiki-page↔WP links, views (`POST /views/{type}`), grids/boards/my-page, render/markdown preview, notification detail sub-resources, custom-field hierarchy items, backups, OAuth app admin, string objects** — low agent value or admin-only; all reachable later via `raw_filters`/new tools if demand appears.
- **BCF/BIM APIs; SSE transport; resource subscriptions; sampling** (no Claude-client support).
- **S3 direct-upload (`/attachments/prepare`) flow** — plain multipart works everywhere; revisit for >100 MB use cases.

---

## 19. Open questions

1. **Multi-user HTTP auth** — per-user API-key mapping file vs OpenProject OAuth (Doorkeeper auth-code + PKCE) token passthrough. Proposal: keys file in Phase 3, OAuth passthrough as fast-follow. Decide before Phase 3.
2. **Version floor** — the spec targets 14 LTS with probes (§4.7) for the verified divergences (time-entry filters, internal comments, favorites, capabilities contexts, emoji reactions). If the deployed instance is ≥ 16, the probe layer shrinks; confirm the actual target instance version to descope.
3. **OpenProject's first-party MCP server** — it has **shipped** in 17.x core (`/mcp` endpoint, Enterprise-gated via `mcp_server` token, admin-configured). Positioning: this server is the free/self-hosted-friendly, deeper alternative (files, git aggregation, reporting, 14-LTS support). Track its tool surface each OpenProject release to avoid name collisions and to steal good ideas.
4. **Old-name compatibility aliases** — should v1 ship hidden aliases for the old server's 62 tool names (FastMCP tool transformation makes this cheap) to ease migration for existing prompt libraries, or is the §16 mapping table enough? Leaning: mapping table only; aliases add permanent surface for a temporary problem.
