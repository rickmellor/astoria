"""Curator maintenance — deterministic, LLM-free housekeeping run by the worker loop.

    embed_backfill      fill NULL embeddings (facts + episodes) after a TEI outage
    rederive_profile    profile narrative = TEMPLATE over active profile-layer facts (versioned)
    prune_snapshots     drop recall snapshots older than N days
    archive_old_turns   working-memory turns older than N days → status='archived'
"""
from __future__ import annotations

import logging
import re

import psycopg

from astoria.core.embed import embed_texts

log = logging.getLogger("astoria.curator")

EMBED_BATCH = 8


# ---------------------------------------------------------------------------
# embeddings

def embed_backfill(c: psycopg.Connection, limit: int = 200) -> dict:
    """Embed up to `limit` facts + `limit` episodes whose embedding is NULL. Returns counts."""
    done = {"facts": 0, "episodes": 0, "pending_facts": 0, "pending_episodes": 0}
    facts_rows = c.execute(
        "SELECT id, hook FROM fact WHERE embedding IS NULL AND status <> 'deleted' ORDER BY ingested_at LIMIT %s",
        (limit,)).fetchall()
    for i in range(0, len(facts_rows), EMBED_BATCH):
        chunk = facts_rows[i:i + EMBED_BATCH]
        vecs = embed_texts([r["hook"] for r in chunk])
        for r, v in zip(chunk, vecs):
            if v is not None:
                c.execute("UPDATE fact SET embedding=%s WHERE id=%s AND embedding IS NULL", (v, r["id"]))
                done["facts"] += 1
    ep_rows = c.execute(
        "SELECT id, hook FROM episode WHERE embedding IS NULL AND status <> 'deleted' ORDER BY ingested_at LIMIT %s",
        (limit,)).fetchall()
    for i in range(0, len(ep_rows), EMBED_BATCH):
        chunk = ep_rows[i:i + EMBED_BATCH]
        vecs = embed_texts([r["hook"] for r in chunk])
        for r, v in zip(chunk, vecs):
            if v is not None:
                c.execute("UPDATE episode SET embedding=%s WHERE id=%s AND embedding IS NULL", (v, r["id"]))
                done["episodes"] += 1
    done["pending_facts"] = c.execute(
        "SELECT count(*) AS n FROM fact WHERE embedding IS NULL AND status <> 'deleted'").fetchone()["n"]
    done["pending_episodes"] = c.execute(
        "SELECT count(*) AS n FROM episode WHERE embedding IS NULL AND status <> 'deleted'").fetchone()["n"]
    return done


# ---------------------------------------------------------------------------
# retention

def prune_snapshots(c: psycopg.Connection, days: int = 90) -> int:
    return c.execute("DELETE FROM snapshot WHERE created_at < now() - make_interval(days => %s)", (days,)).rowcount


def archive_old_turns(c: psycopg.Connection, days: int = 14) -> int:
    return c.execute(
        "UPDATE episode SET status='archived' WHERE kind='turn' AND status='active' "
        "AND occurred_at < now() - make_interval(days => %s)", (days,)).rowcount


# ---------------------------------------------------------------------------
# profile narrative (template, no LLM)

# set predicates with a natural verb phrase; anything else falls back to "<Name> <pred words>: a, b."
_SET_PHRASES = {
    "likes": "{n} likes: {v}.",
    "dislikes": "{n} dislikes: {v}.",
    "interested_in": "{n} is interested in: {v}.",
    "has_skill": "{n} has skills in: {v}.",
    "knows_person": "{n} knows: {v}.",
    "uses_tool": "{n} uses: {v}.",
    "owns_hardware": "{n} owns: {v}.",
    "runs_service": "{n} runs: {v}.",
    "works_on_project": "{n} works on: {v}.",
    "goal": "{n}'s goals: {v}.",
}
_FUNC_PHRASES = {
    "name": "{n}'s name is {v}.",
    "location": "{n} lives in {v}.",
    "timezone": "{n}'s timezone is {v}.",
    "employer": "{n} works at {v}.",
    "role": "{n}'s role is {v}.",
}


def _display_name(user_id: str, by_pred: dict[str, list[str]]) -> str:
    if by_pred.get("name"):
        return by_pred["name"][-1]
    return user_id[:1].upper() + user_id[1:] if user_id else "The user"


def render_profile_narrative(user_id: str, rows: list[dict]) -> str:
    """Deterministic narrative from active profile-layer facts (rows need predicate, cardinality, value)."""
    by_pred: dict[str, list[str]] = {}
    card: dict[str, str] = {}
    for r in rows:
        by_pred.setdefault(r["predicate"], []).append(r["value"])
        card[r["predicate"]] = r["cardinality"]
    if not by_pred:
        return ""
    n = _display_name(user_id, by_pred)
    poss = n + ("'" if n.endswith("s") else "'s")
    sentences: list[str] = []
    for pred in sorted(by_pred):
        vals = by_pred[pred]
        words = pred.replace("_", " ")
        if card.get(pred) == "functional":
            v = vals[-1]
            tpl = _FUNC_PHRASES.get(pred, "{p} {w} is {v}.")
            sentences.append(tpl.format(n=n, v=v, p=poss, w=words))
        else:
            seen, uniq = set(), []
            for v in vals:
                k = re.sub(r"\s+", " ", v.strip().lower())
                if k not in seen:
                    seen.add(k); uniq.append(v.strip())
            v = ", ".join(uniq)
            tpl = _SET_PHRASES.get(pred, "{p} {w}: {v}.")
            sentences.append(tpl.format(n=n, v=v, p=poss, w=words))
    return " ".join(sentences)


def rederive_profile(c: psycopg.Connection, user_id: str) -> dict:
    """Rebuild the profile narrative from active profile-layer facts; bump version + history only when
    the text changed. Always stamps rederived_at (so users_with_profile_changes settles)."""
    rows = c.execute(
        "SELECT predicate, cardinality, value FROM fact WHERE user_id=%s AND subject=%s AND layer='profile' "
        "AND status='active' ORDER BY predicate, asserted_at, ingested_at", (user_id, user_id)).fetchall()
    narrative = render_profile_narrative(user_id, rows)
    cur = c.execute("SELECT narrative, version FROM profile WHERE user_id=%s FOR UPDATE", (user_id,)).fetchone()
    if cur is None:
        c.execute("INSERT INTO profile(user_id, narrative, version, rederived_at, source) "
                  "VALUES (%s, '', 0, NULL, 'template') ON CONFLICT (user_id) DO NOTHING", (user_id,))
        cur = {"narrative": "", "version": 0}
    changed = narrative != cur["narrative"]
    version = cur["version"]
    if changed:
        version += 1
        c.execute("UPDATE profile SET narrative=%s, version=%s, rederived_at=now(), source='template' WHERE user_id=%s",
                  (narrative, version, user_id))
        c.execute("INSERT INTO profile_history(user_id, version, narrative) VALUES (%s,%s,%s) "
                  "ON CONFLICT (user_id, version) DO UPDATE SET narrative=EXCLUDED.narrative, created_at=now()",
                  (user_id, version, narrative))
    else:
        c.execute("UPDATE profile SET rederived_at=now() WHERE user_id=%s", (user_id,))
    return {"user_id": user_id, "version": version, "changed": changed, "narrative": narrative, "facts": len(rows)}


def users_with_profile_changes(c: psycopg.Connection) -> list[str]:
    """Users whose profile-layer facts were inserted/closed since their last re-derive (or never derived)."""
    rows = c.execute(
        "SELECT DISTINCT f.user_id FROM fact f LEFT JOIN profile p ON p.user_id=f.user_id "
        "WHERE f.layer='profile' AND (p.rederived_at IS NULL OR f.ingested_at > p.rederived_at "
        "OR (f.expired_at IS NOT NULL AND f.expired_at > p.rederived_at)) ORDER BY f.user_id").fetchall()
    return [r["user_id"] for r in rows]
