"""Thin HTTP wrapper for the Astoria REST API (docs/CONTRACT.md).

The CLI never touches the database — everything goes through the service.
Config comes from the environment:

    ASTORIA_URL    base URL          (default http://localhost:8933)
    ASTORIA_TOKEN  bearer token      (optional; maps to a client name server-side)
    ASTORIA_USER   default user_id   (default: the server's ASTORIA_USER_DEFAULT)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8933"
DEFAULT_USER = ""          # empty → the server applies its ASTORIA_USER_DEFAULT
CLIENT_NAME = "cli"

# exit codes (non-zero on any failure; distinct so scripts can branch)
EXIT_OK = 0
EXIT_ERROR = 1          # generic / usage-ish runtime error
EXIT_CONNECT = 3        # could not reach the service
EXIT_HTTP_4XX = 4       # the service rejected the request
EXIT_HTTP_5XX = 5       # the service failed


class ApiError(Exception):
    """Raised for transport failures and non-2xx responses."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None,
                 exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.exit_code = exit_code

    def __str__(self) -> str:  # one-liner for the terminal
        if self.status is not None:
            return f"HTTP {self.status}: {self.message}"
        return self.message


def env_url() -> str:
    return os.environ.get("ASTORIA_URL", DEFAULT_URL).rstrip("/")


def env_user() -> str:
    return os.environ.get("ASTORIA_USER", DEFAULT_USER)


def env_token() -> str | None:
    return os.environ.get("ASTORIA_TOKEN") or None


@dataclass
class AstoriaClient:
    base_url: str = field(default_factory=env_url)
    token: str | None = field(default_factory=env_token)
    user: str = field(default_factory=env_user)
    timeout: float = 30.0

    # ------------------------------------------------------------------ core
    def _headers(self) -> dict[str, str]:
        h = {"X-Astoria-Client": CLIENT_NAME, "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None) -> Any:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            with httpx.Client(timeout=self.timeout, headers=self._headers()) as cx:
                r = cx.request(method, url, params=clean_params or None, json=json_body)
        except httpx.ConnectError as e:
            raise ApiError(f"cannot reach Astoria at {self.base_url} ({e.__class__.__name__})",
                           exit_code=EXIT_CONNECT) from e
        except httpx.TimeoutException as e:
            raise ApiError(f"timed out talking to {self.base_url} after {self.timeout:.0f}s",
                           exit_code=EXIT_CONNECT) from e
        except httpx.HTTPError as e:
            raise ApiError(f"transport error: {e}", exit_code=EXIT_CONNECT) from e

        body: Any
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = r.text

        if r.status_code >= 400:
            msg = None
            if isinstance(body, dict):
                msg = body.get("error") or body.get("detail") or body.get("message")
                if isinstance(msg, (dict, list)):
                    msg = json.dumps(msg)
            if not msg:
                msg = (r.text or r.reason_phrase or "request failed").strip()[:300]
            code = EXIT_HTTP_5XX if r.status_code >= 500 else EXIT_HTTP_4XX
            raise ApiError(str(msg), status=r.status_code, body=body, exit_code=code)
        return body

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self.request("POST", path, json_body=body or {})

    def patch(self, path: str, body: dict | None = None) -> Any:
        return self.request("PATCH", path, json_body=body or {})

    def delete(self, path: str, **params: Any) -> Any:
        return self.request("DELETE", path, params=params)

    # ------------------------------------------------------------- endpoints
    # Each mirrors one row of the REST table in docs/CONTRACT.md.

    def health(self) -> dict:
        return self.get("/health")

    def recall(self, query: str, *, session_id: str | None = None, layers: list[str] | None = None,
               max_tokens: int = 1000, limit: int = 12, facts_only: bool = False,
               include_profile: bool = False, as_of: str | None = None,
               as_believed_at: str | None = None) -> dict:
        body: dict[str, Any] = {"user_id": self.user, "query": query, "max_tokens": max_tokens,
                                "limit": limit, "facts_only": facts_only,
                                "include_profile": include_profile}
        if session_id:
            body["session_id"] = session_id
        if layers:
            body["layers"] = layers
        if as_of:
            body["as_of"] = as_of
        if as_believed_at:
            body["as_believed_at"] = as_believed_at
        return self.post("/recall", body)

    def briefing(self, max_tokens: int | None = None) -> dict:
        return self.get("/briefing", user_id=self.user, max_tokens=max_tokens)

    def profile(self) -> dict:
        return self.get("/profile", user_id=self.user)

    def capture(self, **fields: Any) -> dict:
        body = {"user_id": self.user, **{k: v for k, v in fields.items() if v is not None}}
        return self.post("/capture", body)

    def list_facts(self, *, subject: str | None = None, predicate: str | None = None,
                   status: str | None = None, layer: str | None = None, q: str | None = None,
                   limit: int | None = None, offset: int | None = None) -> list[dict]:
        return self.get("/facts", user_id=self.user, subject=subject, predicate=predicate,
                        status=status, layer=layer, q=q, limit=limit, offset=offset) or []

    def get_fact(self, fact_id: str) -> dict:
        return self.get(f"/facts/{fact_id}", user_id=self.user)

    def add_fact(self, subject: str, predicate: str, value: str, **extra: Any) -> dict:
        body = {"user_id": self.user, "subject": subject, "predicate": predicate, "value": value,
                **{k: v for k, v in extra.items() if v is not None}}
        return self.post("/facts", body)

    def correct(self, subject: str, predicate: str, value: str,
                valid_from: str | None = None) -> dict:
        body: dict[str, Any] = {"user_id": self.user, "subject": subject, "predicate": predicate,
                                "value": value}
        if valid_from:
            body["valid_from"] = valid_from
        return self.post("/correct", body)

    def patch_fact(self, fact_id: str, **fields: Any) -> dict:
        return self.patch(f"/facts/{fact_id}", {"user_id": self.user, **fields})

    def delete_fact(self, fact_id: str, mode: str = "soft") -> dict:
        return self.delete(f"/facts/{fact_id}", user_id=self.user, mode=mode)

    def retract(self, *, subject: str | None = None, predicate: str | None = None,
                value: str | None = None, fact_id: str | None = None) -> dict:
        body = {"user_id": self.user}
        for k, v in (("subject", subject), ("predicate", predicate), ("value", value),
                     ("fact_id", fact_id)):
            if v is not None:
                body[k] = v
        return self.post("/retract", body)

    def forget(self, *, fact_id: str | None = None, query: str | None = None,
               mode: str = "soft") -> dict:
        body: dict[str, Any] = {"user_id": self.user, "mode": mode}
        if fact_id:
            body["fact_id"] = fact_id
        if query:
            body["query"] = query
        return self.post("/forget", body)

    def resolve(self, text: str, *, limit: int | None = None) -> dict:
        body: dict[str, Any] = {"user_id": self.user, "text": text}
        if limit:
            body["limit"] = limit
        return self.post("/resolve", body)

    def resolve_apply(self, *, plan: dict | None = None, text: str | None = None,
                      confirm: bool = False) -> dict:
        body: dict[str, Any] = {"user_id": self.user, "confirm": confirm}
        if plan is not None:
            body["plan"] = plan
        if text:
            body["text"] = text
        return self.post("/resolve/apply", body)

    def approve(self, fact_id: str) -> dict:
        return self.post("/approve", {"user_id": self.user, "fact_id": fact_id})

    def history(self, subject: str, predicate: str) -> list[dict]:
        return self.get("/history", user_id=self.user, subject=subject, predicate=predicate) or []

    def as_of(self, at: str, *, as_believed_at: str | None = None, subject: str | None = None,
              predicate: str | None = None, query: str | None = None) -> list[dict]:
        body: dict[str, Any] = {"user_id": self.user, "at": at}
        for k, v in (("as_believed_at", as_believed_at), ("subject", subject),
                     ("predicate", predicate), ("query", query)):
            if v:
                body[k] = v
        return self.post("/as_of", body) or []

    def list_episodes(self, *, session_id: str | None = None, kind: str | None = None,
                      limit: int | None = None) -> list[dict]:
        return self.get("/episodes", user_id=self.user, session_id=session_id, kind=kind,
                        limit=limit) or []

    def delete_episode(self, episode_id: str) -> dict:
        return self.delete(f"/episodes/{episode_id}", user_id=self.user)

    def predicates(self) -> list[dict]:
        return self.get("/predicates") or []

    def set_predicate(self, name: str, *, cardinality: str | None = None,
                      layer_hint: str | None = None) -> dict:
        body = {k: v for k, v in (("cardinality", cardinality), ("layer_hint", layer_hint)) if v}
        return self.patch(f"/predicates/{name}", body)

    def audit(self, limit: int | None = None) -> list[dict]:
        return self.get("/audit", user_id=self.user, limit=limit) or []

    # graph layer + aliases
    def graph(self, node: str, *, depth: int | None = None, fanout: int | None = None) -> dict:
        return self.get("/graph", user_id=self.user, node=node, depth=depth, fanout=fanout)

    def list_edges(self, *, node: str | None = None, relation: str | None = None, depth: int | None = None,
                   status: str | None = None, limit: int | None = None) -> list[dict]:
        return self.get("/edges", user_id=self.user, node=node, relation=relation, depth=depth,
                        status=status, limit=limit) or []

    def add_edge(self, src: str, relation: str, dst: str, **extra: Any) -> dict:
        body = {"user_id": self.user, "src": src, "relation": relation, "dst": dst}
        body.update({k: v for k, v in extra.items() if v is not None})
        return self.post("/edges", body)

    def delete_edge(self, edge_id: str, mode: str = "retract") -> dict:
        return self.delete(f"/edges/{edge_id}", user_id=self.user, mode=mode)

    def list_aliases(self, *, canonical: str | None = None) -> list[dict]:
        return self.get("/aliases", user_id=self.user, canonical=canonical) or []

    def add_alias(self, alias: str, canonical: str) -> dict:
        return self.post("/aliases", {"user_id": self.user, "alias": alias, "canonical": canonical})

    def delete_alias(self, alias: str) -> dict:
        return self.delete(f"/aliases/{alias}", user_id=self.user)

    def op(self, action: str, **fields: Any) -> Any:
        return self.post("/op", {"action": action, "user_id": self.user, **fields})

    def wipe_user(self, user_id: str) -> dict:
        return self.delete(f"/users/{user_id}")

    # --------------------------------------------------------------- helpers
    def resolve_fact_id(self, ident: str) -> str:
        """Accept a full UUID or a unique short prefix (as printed in tables)."""
        ident = ident.strip()
        if len(ident) >= 32:
            return ident
        matches: list[str] = []
        for status in ("any",):
            rows = self.list_facts(status=status, limit=2000)
            matches = sorted({str(r.get("id")) for r in rows
                              if str(r.get("id", "")).startswith(ident)})
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ApiError(f"no fact id starts with '{ident}'", exit_code=EXIT_ERROR)
        raise ApiError(f"ambiguous short id '{ident}' ({len(matches)} matches) — give more chars",
                       exit_code=EXIT_ERROR)
