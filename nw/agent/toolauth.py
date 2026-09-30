"""How the agent authenticates to its tools when they are private services.

On Google Cloud the live agent calls the triage, semantic and policy services and the MCP
server as private Cloud Run services: every request carries a Google ID token whose
audience is the target service's URL, minted from the agent's own identity. That is the
`NW_TOOL_AUTH=google-id-token` setting for the HTTP tools and `NW_MCP_AUTH=google-id-token`
for the MCP client. Unset, nothing is added (the cohort API key still is, by
`nw.auth.service_client`).

Tokens last an hour; one is cached per audience for 50 minutes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Generator, Mapping
from urllib.parse import urlsplit

import httpx

GOOGLE_ID_TOKEN = "google-id-token"
TOKEN_TTL_S = 50 * 60

TokenSource = Callable[[str], str]


def google_id_token(audience: str) -> str:
    """An ID token for `audience` from the application default credentials (the metadata
    server on Cloud Run, a service account key or impersonation elsewhere)."""
    import google.auth.transport.requests
    import google.oauth2.id_token

    return google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)


def audience_of(url: str) -> str:
    """Cloud Run's audience is the service URL: scheme and host, no path."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class CachedTokens:
    def __init__(self, source: TokenSource, clock: Callable[[], float] = time.monotonic) -> None:
        self.source, self.clock = source, clock
        self._cache: dict[str, tuple[str, float]] = {}

    def __call__(self, audience: str) -> str:
        hit = self._cache.get(audience)
        if hit and self.clock() < hit[1]:
            return hit[0]
        token = self.source(audience)
        self._cache[audience] = (token, self.clock() + TOKEN_TTL_S)
        return token


class IdTokenAuth(httpx.Auth):
    """`Authorization: Bearer <ID token>` with the request's service as the audience."""

    def __init__(self, tokens: TokenSource) -> None:
        self.tokens = tokens

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["authorization"] = f"Bearer {self.tokens(audience_of(str(request.url)))}"
        yield request


_sources: dict[str, CachedTokens] = {}


def _tokens(source: TokenSource | None) -> TokenSource:
    if source is not None:
        return CachedTokens(source)
    if GOOGLE_ID_TOKEN not in _sources:
        _sources[GOOGLE_ID_TOKEN] = CachedTokens(google_id_token)
    return _sources[GOOGLE_ID_TOKEN]


def tool_auth_from_env(
    env: Mapping[str, str] | None = None,
    *,
    var: str = "NW_TOOL_AUTH",
    source: TokenSource | None = None,
) -> httpx.Auth | None:
    mode = ((env if env is not None else os.environ).get(var) or "").strip().lower()
    if not mode or mode == "none":
        return None
    if mode != GOOGLE_ID_TOKEN:
        raise ValueError(f"{var}={mode!r}: expected {GOOGLE_ID_TOKEN} or unset")
    return IdTokenAuth(_tokens(source))


def mcp_headers(
    url: str, env: Mapping[str, str] | None = None, *, source: TokenSource | None = None
) -> dict[str, str]:
    """Headers for the MCP client: the cohort API key when configured, and with
    `NW_MCP_AUTH=google-id-token` an ID token for the MCP service."""
    from nw.auth import load_api_keys

    e = env if env is not None else os.environ
    headers: dict[str, str] = {}
    key = next(iter(load_api_keys().values()), "")
    if key:
        headers["x-api-key"] = key
    mode = (e.get("NW_MCP_AUTH") or "").strip().lower()
    if mode == GOOGLE_ID_TOKEN:
        headers["authorization"] = f"Bearer {_tokens(source)(audience_of(url))}"
    elif mode and mode != "none":
        raise ValueError(f"NW_MCP_AUTH={mode!r}: expected {GOOGLE_ID_TOKEN} or unset")
    return headers
