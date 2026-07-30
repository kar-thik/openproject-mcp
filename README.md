# openproject-mcp

An MCP server for the OpenProject API v3, built on FastMCP 3.x and httpx.

Status: Phase 0 (skeleton). The client layer, configuration, server assembly and
test harness exist; no MCP tools are registered yet.

- Full technical specification: [SPEC.md](SPEC.md)
- Python >= 3.12, managed with [uv](https://docs.astral.sh/uv/)

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Running

```sh
export OPENPROJECT_URL=https://openproject.example.com
export OPENPROJECT_API_KEY=<api key>
uv run openproject-mcp                     # stdio transport (default)
uv run openproject-mcp --transport http    # streamable HTTP, binds 127.0.0.1
```

Configuration is entirely environment-driven; see `.env.example` for the full
variable list and SPEC.md §14 for the rationale behind each one.
