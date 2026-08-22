"""Recall — the read path (CONTRACT "Recall algorithm"). No LLM, a handful of SQL round-trips.

    candidates  = top-40 cosine (HNSW, ef_search=64, min cosine 0.45) ⊕ top-40 BM25 (ts_rank_cd)
                  over facts (layers ∩ {profile,semantic,procedural}) and, unless facts_only,
                  top-20 ⊕ top-20 over episodes (summary/note/import/turn, not this session's turns)
    rrf         = Σ 1/(60+rank) over the lists an entity appears in
    score       = rrf × (0.25 + 0.25·recency + 0.25·importance + 0.25·trust)
    collapse    = one row per FUNCTIONAL (subject,predicate); all rows for SET keys
    budget      = facts first, then ≤3 episodes; ≈chars/4 ≤ max_tokens; ≤ limit items
    stale_hint  = a newer active episode mentions the key but not the value (FTS + ILIKE)
    side-effects= one `snapshot` row; access_count/last_seen bump on what was shown
TEI down → BM25-only + health.degraded. `as_of` → facts.as_of rows ranked by BM25 only.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg

from astoria.config import settings
from astoria.core.embed import embed_one
from astoria.store import facts

log = logging.getLogger("astoria.recall")

FACT_LAYERS = ("profile", "semantic", "procedural")
DEFAULT_LAYERS = ("profile", "semantic", "procedural", "episodic")
EPISODE_KINDS = ("summary", "note", "import", "turn")
RRF_K = 60
VEC_TOPN_FACTS, BM25_TOPN_FACTS = 40, 40
VEC_TOPN_EPIS, BM25_TOPN_EPIS = 20, 20
MAX_EPISODES = 3
EPISODE_TRUST = 0.6
HALF_LIFE_DAYS = {"episodic": 30.0, "semantic": 180.0, "belief": 60.0}
CONTEXT_HEADER = "Relevant memory (current facts are authoritative; past conversation may be outdated):"

# projections (never the vectors / tsvectors)
FACT_COLS = ("id, user_id, subject, predicate, cardinality, value, hook, detail, layer, valid_from, valid_to, "
             "asserted_at, ingested_at, expired_at, status, confidence, source, source_kind, source_trust, "
             "is_belief, importance, last_seen, access_count, corroborations, tags, origin_episode, evidence, ref, meta")
EPISODE_COLS = ("id, user_id, kind, hook, body, occurred_at, ingested_at, source, session_id, importance, "
                "access_count, last_seen, status, tags, meta")


# ---------------------------------------------------------------------------
# small helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dt(v: Any) -> datetime | None:
    """Accept datetime or ISO string; naive → UTC."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        from dateutil import parser as dtp
        v = dtp.isoparse(v)
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _or_tsquery(query: str) -> str | None:
    """'what beer do I like' → 'what | beer | do | i | like' (to_tsquery stems + drops stopwords).
    OR semantics: natural-language questions rarely AND-match a 4-word fact hook."""
    words = [w for w in re.findall(r"\w+", query or "") if len(w) <= 64]
    return " | ".join(dict.fromkeys(w.lower() for w in words)) or None


def _recency(age_days: float, half_life: float | None) -> float:
    if half_life is None or half_life <= 0:
        return 1.0
    return math.exp(-math.log(2) * max(0.0, age_days) / half_life)


def _age_days(ts: datetime | None, now: datetime) -> float:
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 86400.0


def _fact_weight(r: dict, now: datetime) -> float:
    if r["layer"] in ("profile", "procedural"):
        rec = 1.0
    else:
        hl = HALF_LIFE_DAYS["belief"] if r.get("is_belief") else HALF_LIFE_DAYS["semantic"]
        rec = _recency(_age_days(r.get("last_seen") or r.get("asserted_at"), now), hl)
    trust = float(r.get("confidence") or 0.0) * float(r.get("source_trust") or 0.0)
    return 0.25 + 0.25 * rec + 0.25 * float(r.get("importance") or 0.0) + 0.25 * trust


def _episode_weight(r: dict, now: datetime) -> float:
    rec = _recency(_age_days(r.get("occurred_at"), now), HALF_LIFE_DAYS["episodic"])
    return 0.25 + 0.25 * rec + 0.25 * float(r.get("importance") or 0.0) + 0.25 * EPISODE_TRUST


def _rrf(*ranked: Iterable[dict]) -> dict[Any, tuple[float, dict]]:
    """Reciprocal-rank fusion over several ranked row lists → {id: (rrf, row)}."""
    out: dict[Any, tuple[float, dict]] = {}
    for rows in ranked:
        for i, r in enumerate(rows, start=1):
            s, row = out.get(r["id"], (0.0, r))
            out[r["id"]] = (s + 1.0 / (RRF_K + i), row)
    return out


# ---------------------------------------------------------------------------
# candidate generation

def _fact_vec(c: psycopg.Connection, user_id: str, qvec: list[float], layers: list[str],
              min_cos: float, topn: int) -> list[dict]:
    c.execute("SET LOCAL hnsw.ef_search = 64")
    rows = c.execute(
        f"SELECT {FACT_COLS}, 1 - (embedding <=> %s::vector) AS cosine FROM fact "
        "WHERE user_id=%s AND status='active' AND (valid_to IS NULL OR valid_to > now()) "
        "AND layer = ANY(%s) AND embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::vector LIMIT %s",
        (qvec, user_id, layers, qvec, topn)).fetchall()
    return [r for r in rows if (r["cosine"] or 0.0) >= min_cos]


def _fact_bm25(c: psycopg.Connection, user_id: str, tsq: str, layers: list[str], topn: int) -> list[dict]:
    return c.execute(
        f"SELECT {FACT_COLS}, ts_rank_cd(tsv, to_tsquery('english', %s)) AS rank FROM fact "
        "WHERE user_id=%s AND status='active' AND (valid_to IS NULL OR valid_to > now()) "
        "AND layer = ANY(%s) AND tsv @@ to_tsquery('english', %s) "
        "ORDER BY rank DESC, asserted_at DESC LIMIT %s",
        (tsq, user_id, layers, tsq, topn)).fetchall()


def _episode_filter(session_id: str | None, as_of: datetime | None) -> tuple[str, list]:
    where = "user_id=%s AND status='active' AND kind = ANY(%s)"
    args: list = [list(EPISODE_KINDS)]
    if session_id:
        where += " AND NOT (kind='turn' AND session_id=%s)"
        args.append(session_id)
    if as_of is not None:
        where += " AND occurred_at <= %s"
        args.append(as_of)
    return where, args


def _episode_vec(c: psycopg.Connection, user_id: str, qvec: list[float], min_cos: float, topn: int,
                 session_id: str | None, as_of: datetime | None) -> list[dict]:
    where, args = _episode_filter(session_id, as_of)
    c.execute("SET LOCAL hnsw.ef_search = 64")
    rows = c.execute(
        f"SELECT {EPISODE_COLS}, 1 - (embedding <=> %s::vector) AS cosine FROM episode "
        f"WHERE {where} AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s",
        (qvec, user_id, *args, qvec, topn)).fetchall()
    return [r for r in rows if (r["cosine"] or 0.0) >= min_cos]


def _episode_bm25(c: psycopg.Connection, user_id: str, tsq: str, topn: int,
                  session_id: str | None, as_of: datetime | None) -> list[dict]:
    where, args = _episode_filter(session_id, as_of)
    return c.execute(
        f"SELECT {EPISODE_COLS}, ts_rank_cd(tsv, to_tsquery('english', %s)) AS rank FROM episode "
        f"WHERE {where} AND tsv @@ to_tsquery('english', %s) ORDER BY rank DESC, occurred_at DESC LIMIT %s",
        (tsq, user_id, *args, tsq, topn)).fetchall()


def _fact_as_of(c: psycopg.Connection, user_id: str, at: datetime, as_believed_at: datetime | None,
                tsq: str | None, layers: list[str], topn: int) -> list[dict]:
    """Point-in-time candidates: facts.as_of rows, then BM25-ranked by the query (no vector)."""
    rows = facts.as_of(c, user_id=user_id, at=at, as_believed_at=as_believed_at, limit=500)
    rows = [r for r in rows if r["layer"] in layers]
    if not rows or not tsq:
        return []
    ids = [r["id"] for r in rows]
    ranked = c.execute(
        "SELECT id, ts_rank_cd(tsv, to_tsquery('english', %s)) AS rank FROM fact "
        "WHERE id = ANY(%s) AND tsv @@ to_tsquery('english', %s) ORDER BY rank DESC LIMIT %s",
        (tsq, ids, tsq, topn)).fetchall()
    by_id = {r["id"]: r for r in rows}
    out = []
    for x in ranked:
        r = dict(by_id[x["id"]]); r.pop("embedding", None); r.pop("tsv", None); r["rank"] = x["rank"]
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# scoring / shaping

def _score_facts(cands: dict, now: datetime) -> list[dict]:
    scored = []
    for rrf, r in cands.values():
        r = dict(r); r["_kind"] = "fact"; r["score"] = rrf * _fact_weight(r, now); scored.append(r)
    scored.sort(key=lambda r: (-r["score"], str(r["id"])))
    # collapse by (subject, predicate): functional → best row; set → all
    out, seen_func = [], set()
    for r in scored:
        key = (r["subject"], r["predicate"])
        if r["cardinality"] == "functional":
            if key in seen_func:
                continue
            seen_func.add(key)
        out.append(r)
    return out


def _score_episodes(cands: dict, now: datetime) -> list[dict]:
    scored = []
    for rrf, r in cands.values():
        r = dict(r); r["_kind"] = "episode"; r["score"] = rrf * _episode_weight(r, now); scored.append(r)
    scored.sort(key=lambda r: (-r["score"], str(r["id"])))
    return scored


def _fact_item(r: dict) -> dict:
    return {
        "id": str(r["id"]), "layer": r["layer"], "kind": "fact", "text": r["hook"],
        "subject": r["subject"], "predicate": r["predicate"], "value": r["value"],
        "score": round(float(r["score"]), 6), "confidence": float(r["confidence"]),
        "source": r["source"], "source_trust": float(r["source_trust"]),
        "asserted_at": _iso(r.get("asserted_at")), "valid_from": _iso(r.get("valid_from")),
        "valid_to": _iso(r.get("valid_to")), "occurred_at": None, "last_seen": _iso(r.get("last_seen")),
        "is_belief": bool(r.get("is_belief")), "stale_hint": bool(r.get("stale_hint", False)),
        "cardinality": r["cardinality"], "status": r.get("status", "active"),
    }


def _episode_item(r: dict) -> dict:
    return {
        "id": str(r["id"]), "layer": "episodic", "kind": "episode", "text": r["hook"],
        "subject": None, "predicate": None, "value": None,
        "score": round(float(r["score"]), 6), "confidence": EPISODE_TRUST,
        "source": r["source"], "source_trust": EPISODE_TRUST,
        "asserted_at": None, "valid_from": None, "valid_to": None,
        "occurred_at": _iso(r.get("occurred_at")), "last_seen": _iso(r.get("last_seen")),
        "is_belief": False, "stale_hint": False,
        "episode_kind": r["kind"], "session_id": r.get("session_id"),
    }


def render_fact_line(r: dict) -> str:
    tag = f"{r['layer']} · {float(r['confidence']):.2f}" + (" · stale?" if r.get("stale_hint") else "")
    return f"- {r['subject']} {r['predicate'].replace('_', ' ')}: {r['value']}  [{tag}]"


def render_episode_line(r: dict) -> str:
    occ = r.get("occurred_at")
    if isinstance(occ, str):
        occ = _as_dt(occ)
    day = occ.strftime("%Y-%m-%d") if occ else "unknown date"
    return f"- from a past session ({day}): {r['text'] if 'text' in r else r['hook']}  [episodic]"


def render_context(items: list[dict]) -> str:
    if not items:
        return ""
    lines = [CONTEXT_HEADER]
    for it in items:
        lines.append(render_fact_line(it) if it["kind"] == "fact" else render_episode_line(it))
    return "\n".join(lines)


def _stale_hints(c: psycopg.Connection, user_id: str, sel: list[dict]) -> set:
    """One query: for each selected FUNCTIONAL fact, is there a newer active episode that mentions the
    key (FTS on the predicate words) and the subject but NOT the current value? The owner's own
    first-person episodes rarely name the user_id, so the subject check is skipped for subject==user_id."""
    func = [r for r in sel if r["cardinality"] == "functional"]
    if not func:
        return set()
    ids = [r["id"] for r in func]
    since = [r["asserted_at"] for r in func]
    pq = [r["predicate"].replace("_", " ") for r in func]
    subj = [None if r["subject"] == user_id else r["subject"] for r in func]
    val = [r["value"] for r in func]
    rows = c.execute(
        "SELECT f.id FROM unnest(%s::uuid[], %s::timestamptz[], %s::text[], %s::text[], %s::text[]) "
        "AS f(id, since, pq, subj, val) WHERE EXISTS ("
        "  SELECT 1 FROM episode e WHERE e.user_id=%s AND e.status='active' AND e.occurred_at > f.since"
        "  AND e.tsv @@ plainto_tsquery('english', f.pq)"
        "  AND (f.subj IS NULL OR (e.hook||' '||e.body) ILIKE '%%'||f.subj||'%%')"
        "  AND NOT ((e.hook||' '||e.body) ILIKE '%%'||f.val||'%%'))",
        (ids, since, pq, subj, val, user_id)).fetchall()
    return {r["id"] for r in rows}


def _parse_turn(r: dict) -> dict:
    meta = r.get("meta") or {}
    ui, ar = meta.get("user_input"), meta.get("agent_response")
    if ui is None and ar is None:
        body = r.get("body") or ""
        m = re.match(r"\s*User:\s*(.*?)\n+\s*Assistant:\s*(.*)\Z", body, re.S)
        if m:
            ui, ar = m.group(1).strip(), m.group(2).strip()
        else:
            ui, ar = body, ""
    return {"user_input": ui or "", "agent_response": ar or "", "occurred_at": _iso(r.get("occurred_at"))}


def _working(c: psycopg.Connection, user_id: str, session_id: str, n: int = 4) -> list[dict]:
    rows = None
    try:
        from astoria.store import episodes  # built concurrently; optional
        fn = getattr(episodes, "recent_turns", None)
        if fn is not None:
            with c.transaction():  # savepoint: a failure here must not poison the recall txn
                rows = fn(c, user_id=user_id, session_id=session_id, n=n)
    except Exception as e:  # noqa: BLE001
        log.debug("episodes.recent_turns unavailable (%s); querying directly", e)
        rows = None
    if rows is None:
        rows = c.execute(
            "SELECT id, kind, hook, body, occurred_at, meta FROM episode WHERE user_id=%s AND session_id=%s "
            "AND kind='turn' AND status='active' ORDER BY occurred_at DESC, ingested_at DESC LIMIT %s",
            (user_id, session_id, n)).fetchall()
        rows = list(reversed(rows))
    return [_parse_turn(r) for r in rows]


def _profile(c: psycopg.Connection, user_id: str, limit: int = 30) -> dict:
    row = c.execute("SELECT narrative, version FROM profile WHERE user_id=%s", (user_id,)).fetchone()
    rows = c.execute(
        f"SELECT {FACT_COLS} FROM fact WHERE user_id=%s AND status='active' AND layer='profile' "
        "AND (valid_to IS NULL OR valid_to > now()) ORDER BY predicate, value LIMIT %s", (user_id, limit)).fetchall()
    return {"narrative": (row or {}).get("narrative", "") or "", "version": (row or {}).get("version", 0) or 0,
            "facts": [facts.row_public(r) for r in rows]}


# ---------------------------------------------------------------------------
# public API

def recall(c: psycopg.Connection, *, user_id: str, query: str, session_id: str | None = None,
           layers: Iterable[str] | None = DEFAULT_LAYERS, max_tokens: int = 1000, limit: int = 12,
           facts_only: bool = False, include_profile: bool = False, as_of: Any = None,
           as_believed_at: Any = None, client: str | None = None, min_cosine: float | None = None) -> dict:
    """Hybrid recall → the REST /recall response dict (items, working, profile, context, health, snapshot_id)."""
    now = _now()
    query = (query or "").strip()
    layers = tuple(layers) if layers else DEFAULT_LAYERS
    fact_layers = [l for l in FACT_LAYERS if l in layers]
    want_episodes = ("episodic" in layers) and not facts_only
    at = _as_dt(as_of)
    believed = _as_dt(as_believed_at)
    min_cos = float(min_cosine if min_cosine is not None else getattr(settings(), "recall_min_cosine", 0.45))
    max_tokens = int(max_tokens or 0) or 1000
    limit = max(0, int(limit if limit is not None else 12))
    tsq = _or_tsquery(query)

    # --- query embedding (degrade to BM25-only) ---------------------------------
    qvec = None
    if query and at is None:
        qvec = embed_one(query, query=True)
    degraded = bool(query) and at is None and qvec is None
    health = {"tei": "down" if degraded else "ok", "degraded": degraded}

    # --- candidates --------------------------------------------------------------
    fact_vec: list[dict] = []
    fact_bm: list[dict] = []
    epi_vec: list[dict] = []
    epi_bm: list[dict] = []
    if query and fact_layers:
        if at is not None:
            fact_bm = _fact_as_of(c, user_id, at, believed, tsq, fact_layers, BM25_TOPN_FACTS)
        else:
            if qvec is not None:
                fact_vec = _fact_vec(c, user_id, qvec, fact_layers, min_cos, VEC_TOPN_FACTS)
            if tsq:
                fact_bm = _fact_bm25(c, user_id, tsq, fact_layers, BM25_TOPN_FACTS)
    if query and want_episodes:
        if qvec is not None:
            epi_vec = _episode_vec(c, user_id, qvec, min_cos, VEC_TOPN_EPIS, session_id, at)
        if tsq:
            epi_bm = _episode_bm25(c, user_id, tsq, BM25_TOPN_EPIS, session_id, at)

    fact_rows = _score_facts(_rrf(fact_vec, fact_bm), now)
    epi_rows = _score_episodes(_rrf(epi_vec, epi_bm), now)[:MAX_EPISODES]

    # --- budget: facts first, then episodes --------------------------------------
    selected: list[dict] = []
    used = 0
    for r in fact_rows + epi_rows:
        if len(selected) >= limit:
            break
        cost = max(1, len(r["hook"] or "") // 4)
        if used + cost > max_tokens:
            continue
        used += cost
        selected.append(r)

    sel_facts = [r for r in selected if r["_kind"] == "fact"]
    sel_epis = [r for r in selected if r["_kind"] == "episode"]

    # --- stale hints (current facts only) -----------------------------------------
    if sel_facts and at is None:
        stale = _stale_hints(c, user_id, sel_facts)
        for r in sel_facts:
            r["stale_hint"] = r["id"] in stale

    items = [(_fact_item(r) if r["_kind"] == "fact" else _episode_item(r)) for r in selected]

    # --- working memory / profile ----------------------------------------------------
    working = _working(c, user_id, session_id) if session_id else []
    profile = _profile(c, user_id) if include_profile else None

    # --- snapshot + touch ------------------------------------------------------------
    fact_ids = [r["id"] for r in sel_facts]
    epi_ids = [r["id"] for r in sel_epis]
    snap = c.execute(
        "INSERT INTO snapshot(user_id, session_id, client, query, fact_ids, episode_ids) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (user_id, session_id, client, query, fact_ids, epi_ids)).fetchone()
    if at is None:
        if fact_ids:
            c.execute("UPDATE fact SET access_count=access_count+1, last_seen=now() WHERE id = ANY(%s)", (fact_ids,))
        if epi_ids:
            c.execute("UPDATE episode SET access_count=access_count+1, last_seen=now() WHERE id = ANY(%s)", (epi_ids,))

    return {
        "user_id": user_id, "query": query, "items": items, "working": working, "profile": profile,
        "context": render_context(items), "health": health, "snapshot_id": str(snap["id"]),
        "as_of": _iso(at), "as_believed_at": _iso(believed),
    }


def briefing(c: psycopg.Connection, *, user_id: str, max_tokens: int = 1200) -> dict:
    """Stable, cache-friendly prefix: narrative + all active profile facts + top-10 semantic facts
    by importance×confidence×recency. Rendered as 'Known about <user> (authoritative, as of DATE):'."""
    now = _now()
    prof = _profile(c, user_id, limit=200)
    sem = c.execute(
        f"SELECT {FACT_COLS} FROM fact WHERE user_id=%s AND status='active' AND layer='semantic' "
        "AND (valid_to IS NULL OR valid_to > now())", (user_id,)).fetchall()
    for r in sem:
        hl = HALF_LIFE_DAYS["belief"] if r.get("is_belief") else HALF_LIFE_DAYS["semantic"]
        rec = _recency(_age_days(r.get("last_seen") or r.get("asserted_at"), now), hl)
        r["score"] = float(r["importance"]) * float(r["confidence"]) * rec
    sem.sort(key=lambda r: (-r["score"], r["predicate"], r["value"]))
    top_sem = sem[:10]

    lines = [f"Known about {user_id} (authoritative, as of {now:%Y-%m-%d}):"]
    if prof["narrative"]:
        lines.append(prof["narrative"].strip())
    budget = max(0, int(max_tokens or 1200))
    used = sum(len(l) // 4 for l in lines)
    shown: list[dict] = []
    for r in [*prof["facts"], *top_sem]:
        line = render_fact_line(r)
        cost = max(1, len(line) // 4)
        if used + cost > budget:
            break
        used += cost
        lines.append(line)
        shown.append(r)
    pub = [facts.row_public({k: v for k, v in r.items() if k != "score"}) for r in shown]
    context = "\n".join(lines) if (prof["narrative"] or shown) else ""
    return {"narrative": prof["narrative"], "facts": pub, "context": context}


def search_facts_simple(c: psycopg.Connection, *, user_id: str, query: str, limit: int = 20,
                        min_cosine: float | None = None) -> list[dict]:
    """Facts-only hybrid search (no budget/snapshot/touch) — used by forget-by-query. Rows as
    facts.row_public + 'score'."""
    now = _now()
    query = (query or "").strip()
    if not query:
        return []
    min_cos = float(min_cosine if min_cosine is not None else getattr(settings(), "recall_min_cosine", 0.45))
    qvec = embed_one(query, query=True)
    tsq = _or_tsquery(query)
    vec = _fact_vec(c, user_id, qvec, list(FACT_LAYERS), min_cos, VEC_TOPN_FACTS) if qvec is not None else []
    bm = _fact_bm25(c, user_id, tsq, list(FACT_LAYERS), BM25_TOPN_FACTS) if tsq else []
    rows = _score_facts(_rrf(vec, bm), now)
    out = []
    for r in rows[: max(0, int(limit))]:
        p = facts.row_public({k: v for k, v in r.items() if k not in ("score", "cosine", "rank", "_kind")})
        p["score"] = round(float(r["score"]), 6)
        out.append(p)
    return out
