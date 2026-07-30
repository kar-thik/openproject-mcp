"""StaticBearerVerifier: match/reject semantics and no token leakage."""

from __future__ import annotations

import pytest

from openproject_mcp.auth import StaticBearerVerifier


async def test_configured_token_is_accepted() -> None:
    verifier = StaticBearerVerifier(("alpha",))
    access = await verifier.verify_token("alpha")
    assert access is not None
    assert access.scopes == []


async def test_every_configured_token_is_accepted() -> None:
    verifier = StaticBearerVerifier(("alpha", "beta"))
    assert await verifier.verify_token("alpha") is not None
    assert await verifier.verify_token("beta") is not None


async def test_unknown_token_is_rejected() -> None:
    verifier = StaticBearerVerifier(("alpha", "beta"))
    assert await verifier.verify_token("gamma") is None


async def test_prefix_and_case_variants_are_rejected() -> None:
    verifier = StaticBearerVerifier(("alpha",))
    assert await verifier.verify_token("alph") is None
    assert await verifier.verify_token("alphaa") is None
    assert await verifier.verify_token("Alpha") is None
    assert await verifier.verify_token("") is None


async def test_empty_token_set_rejects_everything() -> None:
    verifier = StaticBearerVerifier(())
    assert await verifier.verify_token("") is None
    assert await verifier.verify_token("anything") is None


async def test_comparison_covers_every_token_even_after_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An early match must not short-circuit the remaining comparisons.

    Pins the constant-time property: ``secrets.compare_digest`` runs once per
    configured token even when the first one already matched, so response
    timing reveals nothing about which token (if any) matched.
    """
    import secrets

    calls = 0
    real = secrets.compare_digest

    def counting(left: bytes, right: bytes) -> bool:
        nonlocal calls
        calls += 1
        return real(left, right)

    monkeypatch.setattr("openproject_mcp.auth.secrets.compare_digest", counting)
    verifier = StaticBearerVerifier(("alpha", "beta", "gamma"))
    assert await verifier.verify_token("alpha") is not None
    assert calls == 3


async def test_client_id_names_the_matched_token_position() -> None:
    """Each token authenticates as a distinct, non-secret client id."""
    verifier = StaticBearerVerifier(("alpha", "beta"))
    first = await verifier.verify_token("alpha")
    second = await verifier.verify_token("beta")
    assert first is not None and first.client_id == "openproject-mcp/token-1"
    assert second is not None and second.client_id == "openproject-mcp/token-2"


def test_verifier_repr_does_not_leak_tokens() -> None:
    # Guards against a future __repr__ that dumps attributes; today the class
    # has no __repr__ of its own, so this can only fail if one is added.
    verifier = StaticBearerVerifier(("super-secret-token",))
    assert "super-secret-token" not in repr(verifier)
