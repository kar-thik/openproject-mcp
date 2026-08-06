# Contributing

Thanks for taking the time. This is a solo-maintained project — for anything
larger than a small fix, please open an issue first at
https://github.com/kar-thik/openproject-mcp/issues so the direction is agreed
before you write code.

## Dev setup

Python >= 3.12 and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --group dev
```

## Quality gates

All four must pass:

```sh
uv run pytest                 # offline — HTTP is mocked with respx, no network needed
uv run ruff check .
uv run ruff format --check .
uv run pyright                # strict mode
```

## Conventions

- **Tests are offline.** Every HTTP interaction is mocked with respx; no test
  may require the network or a live OpenProject instance. Tests that do need a
  live instance go behind the `integration` marker — CI excludes them with
  `-m "not integration"`; run them explicitly with
  `uv run pytest -m integration`.
- **pyright is strict over the whole package.** New code must type-check clean
  under `typeCheckingMode = "strict"`.
- **Doc-sync.** The README tool table, the SPEC §6 catalog, and the registered
  tool set must stay identical — `tests/protocol/test_doc_sync.py` enforces
  this. A change to the tool surface therefore always touches code, README.md
  and SPEC.md together.

## Release process

Releases are cut manually from `main` by the maintainer. The PyPI distribution
name is `openproject-mcp-server`.

1. On `main`, all CI green.
2. Bump `__version__` in `openproject_mcp/__init__.py` — the single version
   source (hatchling reads it at build time via `[tool.hatch.version]`, and
   `--version` reports it). `uv.lock` does not pin the project's own version,
   so no lockfile update is needed.
3. In `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
   and add a fresh empty `## [Unreleased]` above it.
4. Commit `release: vX.Y.Z`, push, wait for CI green.
5. `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`
6. Approve the `pypi` environment gate when the Release workflow pauses, then
   verify with `uvx openproject-mcp-server --version`.

After the PyPI publish, the workflow also republishes the MCP registry entry
(`io.github.kar-thik/openproject-mcp-server`) via GitHub OIDC. The versions in
`server.json` are stamped from the tag at publish time, so the checked-in file
does not need a bump per release — keep it updated only when the metadata
itself (description, environment variables, transports) changes.

The release workflow refuses to publish when the tag does not match the package
version. Versioning is SemVer with the 0.x semantics stated at the top of
`CHANGELOG.md`: while 0.x, MINOR releases may change the MCP tool surface;
PATCH releases are fixes and strictly additive changes only.
