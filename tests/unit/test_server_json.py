"""``server.json`` must satisfy the MCP registry's limits before a tag reaches it.

The registry publish is the last leg of ``release.yml`` — it runs after PyPI and
the GitHub release have already gone out — so a manifest the registry rejects is
found far too late. The constraints it enforces are pinned here, where they fail
on a pull request instead. The 0.3.2 publish was refused with
``422 expected length <= 100`` on ``description``.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_config import ENV_SURFACE

REPO = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((REPO / "server.json").read_text(encoding="utf-8"))

#: The registry's limit on ``description``.
DESCRIPTION_LIMIT = 100


def test_description_fits_the_registry_limit() -> None:
    description = MANIFEST["description"]
    assert len(description) <= DESCRIPTION_LIMIT, (
        f"server.json description is {len(description)} chars (limit {DESCRIPTION_LIMIT}): "
        f"{description!r}"
    )


def test_environment_variables_are_ones_the_server_reads() -> None:
    names = {
        env["name"] for package in MANIFEST["packages"] for env in package["environmentVariables"]
    }
    assert names <= ENV_SURFACE, (
        f"server.json lists unknown env vars: {sorted(names - ENV_SURFACE)}"
    )


def test_name_matches_the_readme_ownership_marker() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    marker = f"<!-- mcp-name: {MANIFEST['name']} -->"
    assert marker in readme, f"README.md must carry {marker!r} for the registry's ownership check"
