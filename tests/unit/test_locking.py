"""lockVersion discipline: echo, abort-on-fetch-failure, 409 shaping."""

from __future__ import annotations

import copy

import httpx
import pytest
import respx

from openproject_mcp.client.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnexpectedResponseError,
)
from openproject_mcp.client.http import OpenProjectClient
from openproject_mcp.client.locking import (
    conflicting_fields,
    extract_lock_version,
    patch_with_lock,
    resolve_lock_version,
)
from tests.fixtures.hal_payloads import WORK_PACKAGE


def test_extract_lock_version() -> None:
    assert extract_lock_version(WORK_PACKAGE) == 7
    assert extract_lock_version({"lockVersion": "3"}) == 3
    assert extract_lock_version({}) is None
    assert extract_lock_version({"lockVersion": True}) is None


async def test_supplied_lock_version_skips_the_fetch(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("work_packages/1234").mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE)
    )
    version, snapshot = await resolve_lock_version(op_client, "work_packages/1234", supplied=5)
    assert (version, snapshot) == (5, None)
    assert route.call_count == 0


async def test_missing_lock_version_is_fetched(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/1234").mock(return_value=httpx.Response(200, json=WORK_PACKAGE))
    version, snapshot = await resolve_lock_version(op_client, "work_packages/1234")
    assert version == 7
    assert snapshot is not None and snapshot["id"] == 1234


@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, NotFoundError), (403, PermissionDeniedError)],
)
async def test_fetch_failure_aborts_the_write(
    status: int,
    expected: type[Exception],
    op_client: OpenProjectClient,
    mock_api: respx.MockRouter,
) -> None:
    mock_api.get("work_packages/1234").mock(return_value=httpx.Response(status, json={}))
    patch_route = mock_api.patch("work_packages/1234")

    with pytest.raises(expected):
        await patch_with_lock(op_client, "work_packages/1234", {"subject": "new"})
    assert patch_route.call_count == 0, "the write must never proceed without a lock version"


async def test_absent_lock_version_field_aborts_the_write(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/1234").mock(
        return_value=httpx.Response(200, json={"id": 1234, "subject": "no lock version"})
    )
    patch_route = mock_api.patch("work_packages/1234")

    with pytest.raises(UnexpectedResponseError):
        await patch_with_lock(op_client, "work_packages/1234", {"subject": "new"})
    assert patch_route.call_count == 0


async def test_patch_echoes_the_fetched_lock_version(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/1234").mock(return_value=httpx.Response(200, json=WORK_PACKAGE))
    patch_route = mock_api.patch("work_packages/1234").mock(
        return_value=httpx.Response(200, json=WORK_PACKAGE)
    )

    await patch_with_lock(op_client, "work_packages/1234", {"subject": "new"})

    import json

    body = json.loads(patch_route.calls.last.request.content)
    assert body == {"subject": "new", "lockVersion": 7}


async def test_conflict_is_reshaped_with_a_fresh_snapshot(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    fresh = copy.deepcopy(WORK_PACKAGE)
    fresh["lockVersion"] = 9
    fresh["subject"] = "Renamed by someone else"

    mock_api.get("work_packages/1234").mock(
        side_effect=[
            httpx.Response(200, json=WORK_PACKAGE),
            httpx.Response(200, json=fresh),
        ]
    )
    mock_api.patch("work_packages/1234").mock(
        return_value=httpx.Response(
            409,
            json={
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
                "message": "Your changes could not be saved.",
            },
        )
    )

    with pytest.raises(ConflictError) as excinfo:
        await patch_with_lock(op_client, "work_packages/1234", {"subject": "My rename"})

    conflict = excinfo.value
    assert conflict.lock_version == 9
    assert conflict.current["subject"] == "Renamed by someone else"
    assert conflict.conflicting_fields["subject"] == {
        "attempted": "My rename",
        "current": "Renamed by someone else",
    }
    envelope = conflict.to_envelope()["error"]
    assert envelope["type"] == "conflict"
    assert envelope["lock_version"] == 9


async def test_conflict_survives_a_failed_refetch(
    op_client: OpenProjectClient, mock_api: respx.MockRouter
) -> None:
    mock_api.get("work_packages/1234").mock(
        side_effect=[
            httpx.Response(200, json=WORK_PACKAGE),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
        ]
    )
    mock_api.patch("work_packages/1234").mock(
        return_value=httpx.Response(409, json={"message": "conflict"})
    )

    with pytest.raises(ConflictError) as excinfo:
        await patch_with_lock(op_client, "work_packages/1234", {"subject": "My rename"})
    assert excinfo.value.http_status == 409


def test_conflicting_fields_only_reports_attempted_attributes() -> None:
    diff = conflicting_fields(
        {"subject": "mine", "lockVersion": 3, "_links": {}},
        {"subject": "theirs", "dueDate": "2026-08-01", "lockVersion": 9},
    )
    assert diff == {"subject": {"attempted": "mine", "current": "theirs"}}


def test_conflicting_fields_compares_formattable_text() -> None:
    diff = conflicting_fields(
        {"description": {"format": "markdown", "raw": "same"}},
        {"description": {"format": "markdown", "raw": "same", "html": "<p>same</p>"}},
    )
    assert diff == {}
