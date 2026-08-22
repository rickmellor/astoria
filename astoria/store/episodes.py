"""Episode store — non-lossy raw captures (turns, summaries, notes, imports).

Episodes are written FIRST and durably (they survive an LLM/TEI outage); cognify later lifts
durable facts out of them. Replay-safe: idem_key = sha256(user_id|session_id|kind|body), so
re-sending the same turn returns the existing row (deduped=True) instead of a duplicate.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from astoria.core.embed import embed_one

log = logging.getLogger("astoria.episodes")

KINDS = ("turn", "summary", "note", "import")
HOOK_CHARS = 400


def _collapse(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def render_body(text: str | None = None, user_input: str | None = None, agent_response: str | None = None) -> str:
    if text is not None and str(text).strip():
        return str(text).strip()
    return f"User: {(user_input or '').strip()}\nAssistant: {(agent_response or '').strip()}"


def render_hook(text: str | None = None, user_input: str | None = None) -> str:
    src = text if (text is not None and str(text).strip()) else user_input
    return _collapse(src)[:HOOK_CHARS]


def idem_key(user_id: str, session_id: str | None, kind: str, body: str) -> str:
    return hashlib.sha256(f"{user_id}|{session_id or ''}|{kind}|{body}".encode()).hexdigest()


def _parse_turn(body: str) -> tuple[str | None, str | None]:
    """Best-effort split of a rendered turn body back into (user_input, agent_response)."""
    m = re.match(r"^User: (.*?)\nAssistant: (.*)$", body, re.DOTALL)
    if m:
        return m.group(1), m.group(2)
    return None, None


# ---------------------------------------------------------------------------
# write path

def add_episode(c: psycopg.Connection, *, user_id: str, kind: str = "turn", text: str | None = None,
                user_input: str | None = None, agent_response: str | None = None, source: str = "api",
                session_id: str | None = None, occurred_at: datetime | None = None, importance: float = 0.5,
                tags: Iterable[str] = (), meta: dict | None = None, embed: bool = True) -> dict:
    """Insert (or return the existing replay of) an episode. Returns {"episode": row, "deduped": bool}.

    A turn is given as user_input (+ agent_response); other kinds as text. user_input/agent_response are
    kept in meta so recall can return them cleanly without re-parsing the body.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if (text is None or not str(text).strip()) and not (user_input or "").strip() and not (agent_response or "").strip():
        raise ValueError("episode needs text or user_input/agent_response")
    body = render_body(text, user_input, agent_response)
    hook = render_hook(text, user_input) or _collapse(body)[:HOOK_CHARS]
    key = idem_key(user_id, session_id, kind, body)
    meta = dict(meta or {})
    if user_input is not None and "user_input" not in meta:
        meta["user_input"] = user_input
    if agent_response is not None and "agent_response" not in meta:
        meta["agent_response"] = agent_response

    with c.cursor() as cur:
        existing = cur.execute("SELECT * FROM episode WHERE idem_key=%s", (key,)).fetchone()
        if existing:
            row = cur.execute(
                "UPDATE episode SET last_seen=now(), access_count=access_count+1 WHERE id=%s RETURNING *",
                (existing["id"],)).fetchone()
            log.debug("episode replay deduped user=%s kind=%s id=%s", user_id, kind, row["id"])
            return {"episode": row, "deduped": True}

        vec = None
        if embed:
            try:
                vec = embed_one(hook)
            except Exception as e:  # noqa: BLE001 — embed_one never raises, but belt and braces
                log.warning("episode embed failed (degrading): %s", e)
                vec = None
        cur.execute(
            "INSERT INTO episode(user_id, kind, hook, body, embedding, occurred_at, source, session_id, importance, "
            "idem_key, tags, meta) VALUES (%s,%s,%s,%s,%s,COALESCE(%s, now()),%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (idem_key) DO UPDATE SET last_seen=now(), access_count=episode.access_count+1 "
            "RETURNING *, (xmax = 0) AS _inserted",
            (user_id, kind, hook, body, vec, occurred_at, source, session_id, float(importance), key,
             list(tags), Jsonb(meta)))
        row = cur.fetchone()
        inserted = bool(row.pop("_inserted", True))
        return {"episode": row, "deduped": not inserted}


def touch(c: psycopg.Connection, ids: Iterable[Any]) -> int:
    """Bump access_count/last_seen for recalled episodes. Returns rows touched."""
    ids = [str(i) for i in ids if i]
    if not ids:
        return 0
    with c.cursor() as cur:
        cur.execute("UPDATE episode SET access_count=access_count+1, last_seen=now() WHERE id = ANY(%s::uuid[])", (ids,))
        return cur.rowcount


def archive_episode(c: psycopg.Connection, *, user_id: str, episode_id: str) -> dict | None:
    return c.execute("UPDATE episode SET status='archived' WHERE id=%s AND user_id=%s RETURNING *",
                     (episode_id, user_id)).fetchone()


def delete_episode(c: psycopg.Connection, *, user_id: str, episode_id: str) -> bool:
    """Hard delete (queue rows cascade; facts keep lineage NULLed by FK)."""
    with c.cursor() as cur:
        cur.execute("DELETE FROM episode WHERE id=%s AND user_id=%s", (episode_id, user_id))
        return cur.rowcount > 0


def enqueue_cognify(c: psycopg.Connection, *, user_id: str, episode_id: str, session_id: str | None = None,
                    priority: int = 5, occurred_at: datetime | None = None, payload: dict | None = None,
                    kind: str = "extract") -> dict:
    """Queue an LLM-at-write job for an episode (priority 1 = corrections first)."""
    return c.execute(
        "INSERT INTO cognify_queue(user_id, episode_id, session_id, kind, priority, occurred_at, payload) "
        "VALUES (%s,%s,%s,%s,%s,COALESCE(%s, now()),%s) RETURNING *",
        (user_id, episode_id, session_id, kind, int(priority), occurred_at, Jsonb(payload or {}))).fetchone()


# ---------------------------------------------------------------------------
# read path

def get_episode(c: psycopg.Connection, *, user_id: str, episode_id: str) -> dict | None:
    return c.execute("SELECT * FROM episode WHERE id=%s AND user_id=%s", (episode_id, user_id)).fetchone()


def list_episodes(c: psycopg.Connection, *, user_id: str, session_id: str | None = None, kind: str | None = None,
                  status: str | None = "active", limit: int = 50, offset: int = 0) -> list[dict]:
    where, args = ["user_id=%s"], [user_id]
    if session_id:
        where.append("session_id=%s"); args.append(session_id)
    if kind:
        where.append("kind=%s"); args.append(kind)
    if status and status != "any":
        where.append("status=%s"); args.append(status)
    sql = f"SELECT * FROM episode WHERE {' AND '.join(where)} ORDER BY occurred_at DESC, ingested_at DESC LIMIT %s OFFSET %s"
    return c.execute(sql, (*args, limit, offset)).fetchall()


def recent_turns(c: psycopg.Connection, *, user_id: str, session_id: str | None, n: int = 4) -> list[dict]:
    """Working memory: the newest n active turns of a session, returned OLDEST-first, each row carrying
    `user_input` / `agent_response` (from meta when stored, else parsed from the body)."""
    if not session_id:
        return []
    rows = c.execute(
        "SELECT * FROM episode WHERE user_id=%s AND session_id=%s AND kind='turn' AND status='active' "
        "ORDER BY occurred_at DESC, ingested_at DESC LIMIT %s", (user_id, session_id, int(n))).fetchall()
    out = []
    for r in reversed(rows):
        r = dict(r)
        m = r.get("meta") or {}
        ui, ar = m.get("user_input"), m.get("agent_response")
        if ui is None and ar is None:
            ui, ar = _parse_turn(r["body"])
        r["user_input"], r["agent_response"] = ui, ar
        out.append(r)
    return out


def row_public(r: dict | None) -> dict | None:
    """JSON-safe projection (no embedding / tsv)."""
    if r is None:
        return None
    out = {}
    for k, v in r.items():
        if k in ("embedding", "tsv"):
            continue
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif k in ("id", "episode_id") and v is not None:
            v = str(v)
        out[k] = v
    return out
