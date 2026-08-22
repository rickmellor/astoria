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
