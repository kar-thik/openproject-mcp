"""Bearer-token verification for the HTTP transport (SPEC §11).

``OPENPROJECT_MCP_AUTH_TOKENS`` holds a static, comma-separated set of bearer
tokens. When it is configured, :class:`StaticBearerVerifier` is attached to the
server as its auth provider; FastMCP's HTTP app then rejects every request to
the MCP endpoint that does not carry ``Authorization: Bearer <token>`` with a
configured token (401 + ``WWW-Authenticate``). Only the HTTP app consults the
auth provider — the stdio transport and the in-memory client are untouched.

Token comparison is constant-time (:func:`secrets.compare_digest`) and checks
every configured token so timing reveals nothing about which, if any, matched.
Token material is never logged, and an empty token set rejects all requests
rather than leaving the endpoint open.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

from fastmcp.server.auth.auth import AccessToken, TokenVerifier

__all__ = ["StaticBearerVerifier"]


class StaticBearerVerifier(TokenVerifier):
    """Verifies presented bearer tokens against a fixed set of secrets.

    The presented token is compared against **every** configured token with
    :func:`secrets.compare_digest` — no early exit — so response timing does
    not leak which token (if any) matched. With an empty token set every
    request is rejected: a misconfigured server fails closed.

    A match is attributed to a non-secret client id derived from the token's
    1-based position in ``OPENPROJECT_MCP_AUTH_TOKENS`` (for example
    ``openproject-mcp/token-2``), so with one token per client, access logs can
    say which client authenticated without exposing token material.
    """

    def __init__(self, tokens: Sequence[str]) -> None:
        super().__init__()
        self._tokens = tuple(token.encode("utf-8") for token in tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.encode("utf-8")
        matched_index = -1
        for index, candidate in enumerate(self._tokens):
            if secrets.compare_digest(presented, candidate) and matched_index < 0:
                matched_index = index
        if matched_index < 0:
            return None
        return AccessToken(
            token=token, client_id=f"openproject-mcp/token-{matched_index + 1}", scopes=[]
        )
