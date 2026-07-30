# Security Policy

## Supported versions

| Version          | Supported |
| ---------------- | --------- |
| latest 0.x minor | yes       |
| anything older   | no        |

Security fixes land on the latest 0.x minor release only.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
https://github.com/kar-thik/openproject-mcp/security/advisories/new

Do not open public issues for suspected vulnerabilities. This is a
solo-maintained project: you can expect a best-effort acknowledgment within a
week. There is no bug bounty.

## Scope

The areas most worth scrutiny in an MCP server that holds an OpenProject API
key:

- **Credential handling** — the API key and OAuth token are held as Pydantic
  `SecretStr` values; `Authorization`, `Proxy-Authorization` and cookie headers
  are redacted before logging (request/response bodies are logged only when
  `OPENPROJECT_MCP_LOG_BODIES=1` and the level is DEBUG), and the
  `Authorization` header is stripped when a redirect leaves the OpenProject
  origin.
- **Attachment downloads** — quarantined files are refused with a structured
  error, downloads are capped by `OPENPROJECT_MCP_MAX_DOWNLOAD_MB`, filenames
  are sanitized before any filesystem use, and a SHA-256 digest of the bytes is
  returned for verification.
- **HTTP transport** — binds `127.0.0.1` by default and refuses to start
  without `OPENPROJECT_MCP_AUTH_TOKENS` unless `OPENPROJECT_MCP_INSECURE=1` is
  set explicitly (local development only). With tokens configured, every
  request must carry `Authorization: Bearer <token>` matching a configured
  token — comparison is constant-time, anything else is rejected with a 401 —
  and a set-but-empty token list refuses to start rather than serving an open
  endpoint. The server does not terminate TLS or rate-limit the endpoint:
  keep the localhost bind, or front it with a TLS-terminating reverse proxy
  and use long random tokens.
