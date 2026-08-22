"""Client identity from a request — who is talking to Astoria (the fact `source`).

`Authorization: Bearer <token>` maps to a client name via ASTORIA_CLIENT_TOKENS
("input:tok,claude-code:tok,..."). Without a token we accept the unauthenticated
`X-Astoria-Client` hint (LAN-only deployment — it is trusted as-is), else "anonymous".
The name becomes the fact `source` and feeds facts.CLIENT_TRUST (ranking only). Only a
valid Bearer token *proves* identity; a bare hint is a convenience for trusted LAN clients.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astoria.config import settings

HINT_HEADER = "x-astoria-client"


def client_from_headers(headers: Mapping[str, str] | None) -> str:
    """Resolve a client name from a (case-insensitive) header mapping."""
    if not headers:
        return "anonymous"
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    auth = (h.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        name = settings().client_token_map().get(tok)
        if name:
            return name
    hint = (h.get(HINT_HEADER) or "").strip()
    if hint:
        return hint[:64]
    return "anonymous"


def client_from_request(request: Any) -> str:
    """FastAPI/Starlette request → client name (see module doc)."""
    try:
        return client_from_headers(request.headers)
    except Exception:  # noqa: BLE001 — identity must never break a route
        return "anonymous"


READ_ACTIONS = frozenset({"recall", "briefing", "profile", "facts_list", "fact_get", "history", "as_of", "episodes_list",
                          "episode_get", "predicates_list", "audit", "health", "retrieve", "user_profile", "graph",
                          "edges_list", "aliases_list", "resolve", "queue_stats"})


def is_authenticated(headers: Mapping[str, str] | None) -> bool:
    """True iff a bearer token in `headers` maps to a configured client name."""
    if not headers:
        return False
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    auth = (h.get("authorization") or "").strip()
    return auth.lower().startswith("bearer ") and auth[7:].strip() in settings().client_token_map()


class WriteNotAuthorized(PermissionError):
    pass


def require_write_token(headers: Mapping[str, str] | None, action: str) -> None:
    """Enforce ASTORIA_REQUIRE_TOKEN: writes need a valid bearer token; reads never do."""
    if settings().require_token and action not in READ_ACTIONS and not is_authenticated(headers):
        raise WriteNotAuthorized(f"action {action!r} requires a client token (ASTORIA_REQUIRE_TOKEN=true)")
