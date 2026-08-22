"""Graph store — typed edges between fact/entity nodes, the entity registry and subject aliases.

Nodes are addressed as `kind:id` strings:
    fact:<uuid>        a fact row (id = uuid text)
    entity:<name>      an entity keyed by its canonical lower-case name (== fact.subject spelling)
A bare uuid parses as a fact node, anything else as an entity name. Entity endpoints are run through
`resolve_alias` so an edge to an alias lands on the canonical entity.

Contract with the fact store: `resolve_alias(c, user_id, name) -> canonical | None` is called from
`facts.canon_subject`, so an aliased subject is rewritten to its canonical form on every write/read.
Aliases are kept FLAT (no chains): `add_alias` collapses `alias → canonical` through any existing
alias of `canonical`, and re-points aliases whose canonical was the new alias.

Edges are idempotent on the active key (user, src, dst, relation): a re-assert bumps asserted_at /
confidence (corroboration when from an independent episode) instead of inserting a duplicate.
`neighbors` is a bounded recursive CTE (max_depth hops, max_fanout strongest edges per node per hop,
per-path visited set for cycle safety) returning reached nodes with hop distance and path relations.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from astoria.config import settings

log = logging.getLogger("astoria.graph")

NODE_KINDS = ("fact", "entity")
EDGE_STATUSES = ("active", "superseded", "retracted", "archived")
KIND_CONF = {"explicit": 0.90, "detector": 0.80, "extracted": 0.60, "imported": 0.45, "curator": 0.50}
EDGE_COLS = ("id, user_id, src_kind, src_id, dst_kind, dst_id, relation, weight, valid_from, valid_to, asserted_at, "
             "status, superseded_by, source, source_kind, confidence, origin_episode, evidence, meta")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# node / name canonicalization

def canon_entity(name: str | None) -> str:
    """Entity-name canonicalization: whitespace-collapsed, lower-case, ≤ 64 chars (mirrors the
    non-user branch of facts.canon_subject; user aliases are the caller's business)."""
    t = re.sub(r"\s+", " ", (name or "").strip()).lower()
    if not t:
        raise ValueError("empty entity name")
    return t[:64]


def canon_relation(rel: str | None) -> str:
    t = re.sub(r"[^a-z0-9_]+", "_", (rel or "").strip().lower()).strip("_")
    return (t or "related_to")[:64]


def parse_node(node: Any) -> tuple[str, str]:
    """'fact:<uuid>' | 'entity:<name>' | bare uuid (→ fact) | bare name (→ entity) → (kind, id)."""
    s = str(node or "").strip()
    if not s:
        raise ValueError("empty node")
    kind, sep, rest = s.partition(":")
    if sep and kind.lower() in NODE_KINDS:
        kind, rest = kind.lower(), rest.strip()
        if kind == "fact":
            return "fact", str(uuid.UUID(rest))
        return "entity", canon_entity(rest)
    if _UUID_RE.match(s):
        return "fact", str(uuid.UUID(s))
    return "entity", canon_entity(s)


def node_ref(kind: str, node_id: Any) -> str:
    return f"{kind}:{node_id}"


def _audit(cur, user_id: str, actor: str | None, op: str, target: Any, detail: dict) -> None:
    cur.execute("INSERT INTO audit(user_id, actor, op, target, detail) VALUES (%s,%s,%s,%s,%s)",
                (user_id, actor or "anonymous", op, target, Jsonb(detail)))


def _lock_key(*parts: str) -> int:
    h = hashlib.blake2b("|".join(parts).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


# ---------------------------------------------------------------------------
# aliases

def resolve_alias(c: psycopg.Connection, user_id: str, name: str | None) -> str | None:
    """Canonical subject for `name` if an alias row exists (case-insensitive), else None.
    Wired into facts.canon_subject by the orchestrator."""
    if not name:
        return None
    t = re.sub(r"\s+", " ", str(name).strip()).lower()
    if not t:
        return None
    row = c.execute("SELECT canonical FROM alias WHERE user_id=%s AND alias=%s", (user_id, t[:64])).fetchone()
    return row["canonical"] if row else None


def add_alias(c: psycopg.Connection, *, user_id: str, alias: str, canonical: str, source: str = "api",
              source_kind: str = "explicit", actor: str | None = None) -> dict:
    """alias → canonical (both lower-cased). Flattens chains, refuses self/user aliases.
    Returns {"alias": row, "action": inserted|updated|noop, "repointed": n}."""
    a = canon_entity(alias)
    canon = canon_entity(canonical)
    if a == user_id.lower():
        raise ValueError("cannot alias the user_id itself")
    # flatten: if the canonical is itself an alias, point at ITS canonical
    up = resolve_alias(c, user_id, canon)
    if up:
        canon = up
    if a == canon:
        raise ValueError("alias and canonical are the same name")
    with c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key("alias", user_id),))
        old = cur.execute("SELECT * FROM alias WHERE user_id=%s AND alias=%s", (user_id, a)).fetchone()
        if old and old["canonical"] == canon:
            return {"alias": dict(old), "action": "noop", "repointed": 0}
        row = cur.execute(
            "INSERT INTO alias(user_id, alias, canonical, source, source_kind) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, alias) DO UPDATE SET canonical=EXCLUDED.canonical, source=EXCLUDED.source, "
            "source_kind=EXCLUDED.source_kind, created_at=now() RETURNING *",
            (user_id, a, canon, source or "api", source_kind)).fetchone()
        # keep flat: anything that pointed at the new alias now points at its canonical
        cur.execute("UPDATE alias SET canonical=%s WHERE user_id=%s AND canonical=%s AND alias<>%s",
                    (canon, user_id, a, canon))
        repointed = cur.rowcount
        ensure_entity(c, user_id=user_id, name=canon)
        _audit(cur, user_id, actor, "alias_add", None,
               {"alias": a, "canonical": canon, "source_kind": source_kind, "repointed": repointed})
    return {"alias": dict(row), "action": "updated" if old else "inserted", "repointed": repointed}


def list_aliases(c: psycopg.Connection, *, user_id: str, canonical: str | None = None,
                 limit: int = 500, offset: int = 0) -> list[dict]:
    if canonical:
        return c.execute("SELECT * FROM alias WHERE user_id=%s AND canonical=%s ORDER BY alias LIMIT %s OFFSET %s",
                         (user_id, canon_entity(canonical), limit, offset)).fetchall()
    return c.execute("SELECT * FROM alias WHERE user_id=%s ORDER BY canonical, alias LIMIT %s OFFSET %s",
                     (user_id, limit, offset)).fetchall()


def delete_alias(c: psycopg.Connection, *, user_id: str, alias: str, actor: str | None = None) -> dict | None:
    a = canon_entity(alias)
    with c.cursor() as cur:
        row = cur.execute("DELETE FROM alias WHERE user_id=%s AND alias=%s RETURNING *", (user_id, a)).fetchone()
        if row:
            _audit(cur, user_id, actor, "alias_delete", None, {"alias": a, "canonical": row["canonical"]})
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# entities

def ensure_entity(c: psycopg.Connection, *, user_id: str, name: str, kind: str | None = None,
                  summary: str | None = None) -> dict:
    """Upsert an entity node keyed by canonical name; fills kind/summary only when given (never blanks)."""
    n = canon_entity(name)
    row = c.execute(
        "INSERT INTO entity(user_id, name, kind, summary) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (user_id, name) DO UPDATE SET kind=COALESCE(EXCLUDED.kind, entity.kind), "
        "summary=COALESCE(EXCLUDED.summary, entity.summary) RETURNING *",
        (user_id, n, (kind or None) and str(kind).strip().lower()[:32], (summary or None) and str(summary)[:500])
    ).fetchone()
    return dict(row)


def get_entity(c: psycopg.Connection, *, user_id: str, name: str) -> dict | None:
    r = c.execute("SELECT * FROM entity WHERE user_id=%s AND name=%s", (user_id, canon_entity(name))).fetchone()
    return dict(r) if r else None


def list_entities(c: psycopg.Connection, *, user_id: str, q: str | None = None, limit: int = 200,
                  offset: int = 0) -> list[dict]:
    if q:
        return c.execute("SELECT * FROM entity WHERE user_id=%s AND name ILIKE %s ORDER BY name LIMIT %s OFFSET %s",
                         (user_id, f"%{q}%", limit, offset)).fetchall()
    return c.execute("SELECT * FROM entity WHERE user_id=%s ORDER BY name LIMIT %s OFFSET %s",
                     (user_id, limit, offset)).fetchall()


# ---------------------------------------------------------------------------
# edges

def _endpoint(c: psycopg.Connection, user_id: str, node: Any, kind: str | None) -> tuple[str, str]:
    """Resolve one endpoint → (kind, id). Entities: alias → canonical, auto-registered. Facts: must exist."""
    if kind:
        k = str(kind).lower()
        if k not in NODE_KINDS:
            raise ValueError(f"node kind must be one of {NODE_KINDS}")
        nid = str(uuid.UUID(str(node).split(":", 1)[-1] if str(node).lower().startswith("fact:") else str(node))) \
            if k == "fact" else canon_entity(str(node).split(":", 1)[-1] if str(node).lower().startswith("entity:") else str(node))
    else:
        k, nid = parse_node(node)
    if k == "entity":
        nid = resolve_alias(c, user_id, nid) or nid
        ensure_entity(c, user_id=user_id, name=nid)
    else:
        ok = c.execute("SELECT 1 FROM fact WHERE id=%s AND user_id=%s", (nid, user_id)).fetchone()
        if not ok:
            raise LookupError(f"fact {nid}")
    return k, nid


def _bust_edge_cache(user_id: str) -> None:
    try:
        from astoria.retrieval import recall as _r
        _r._EDGE_CACHE.pop(user_id, None)
    except Exception:  # noqa: BLE001
        pass


def add_edge(c: psycopg.Connection, *, user_id: str, src: Any, relation: str, dst: Any,
             src_kind: str | None = None, dst_kind: str | None = None, weight: float = 1.0,
             valid_from: datetime | None = None, valid_to: datetime | None = None,
             asserted_at: datetime | None = None, source: str = "api", source_kind: str = "explicit",
             confidence: float | None = None, origin_episode: str | None = None, evidence: str | None = None,
             meta: dict | None = None, actor: str | None = None) -> dict:
    """Insert-or-bump an edge. Returns {"edge": row, "action": inserted|noop}.
    Idempotent on the active (src, dst, relation) key: a re-assert bumps asserted_at, keeps the max
    weight/confidence (corroborates when it comes from a different episode AND client), fills evidence."""
    s = settings()
    sk, sid = _endpoint(c, user_id, src, src_kind)
    dk, did = _endpoint(c, user_id, dst, dst_kind)
    rel = canon_relation(relation)
    if (sk, sid) == (dk, did):
        raise ValueError("self-loop edge")
    if source_kind not in KIND_CONF:
        raise ValueError(f"source_kind must be one of {tuple(KIND_CONF)}")
    conf = confidence if confidence is not None else KIND_CONF[source_kind]
    conf = max(s.confidence_floor, min(s.confidence_cap, float(conf)))
    aa = asserted_at or now_utc()
    vf = valid_from or aa
    try:
        w = float(weight)
    except (TypeError, ValueError):
        w = 1.0
    w = max(0.0, min(10.0, w))
    meta = dict(meta or {})
    with c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key("edge", user_id, sk, sid, dk, did, rel),))
        active = cur.execute(
            f"SELECT {EDGE_COLS} FROM edge WHERE user_id=%s AND src_kind=%s AND src_id=%s AND dst_kind=%s "
            f"AND dst_id=%s AND relation=%s AND status='active'", (user_id, sk, sid, dk, did, rel)).fetchone()
        if active:
            independent = (origin_episode is not None and str(active.get("origin_episode")) != str(origin_episode)
                           and active.get("source") != source)
            new_conf = max(float(active["confidence"]), conf)
            if independent:
                new_conf = min(s.confidence_cap, 1 - (1 - new_conf) * 0.6)
            row = cur.execute(
                f"UPDATE edge SET asserted_at=GREATEST(asserted_at, %s), weight=GREATEST(weight, %s), confidence=%s, "
                f"evidence=COALESCE(evidence, %s), source_kind=CASE WHEN %s='explicit' THEN 'explicit' ELSE source_kind END, "
                f"valid_to=CASE WHEN %s::timestamptz IS NULL THEN valid_to ELSE %s END "
                f"WHERE id=%s RETURNING {EDGE_COLS}",
                (aa, w, new_conf, evidence, source_kind, valid_to, valid_to, active["id"])).fetchone()
            _audit(cur, user_id, actor, "edge_noop", row["id"], {"src": node_ref(sk, sid), "dst": node_ref(dk, did),
                                                                  "relation": rel, "corroborated": independent})
            return {"edge": dict(row), "action": "noop"}
        row = cur.execute(
            f"INSERT INTO edge(user_id, src_kind, src_id, dst_kind, dst_id, relation, weight, valid_from, valid_to, "
            f"asserted_at, source, source_kind, confidence, origin_episode, evidence, meta) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {EDGE_COLS}",
            (user_id, sk, sid, dk, did, rel, w, vf, valid_to, aa, source or "api", source_kind, conf,
             origin_episode, (evidence or None) and str(evidence)[:500], Jsonb(meta))).fetchone()
        _audit(cur, user_id, actor, "edge_add", row["id"], {"src": node_ref(sk, sid), "dst": node_ref(dk, did),
                                                             "relation": rel, "source_kind": source_kind})
    _bust_edge_cache(user_id)
    return {"edge": dict(row), "action": "inserted"}


def get_edge(c: psycopg.Connection, *, user_id: str, edge_id: str) -> dict | None:
    r = c.execute(f"SELECT {EDGE_COLS} FROM edge WHERE id=%s AND user_id=%s", (str(uuid.UUID(str(edge_id))), user_id)).fetchone()
    return dict(r) if r else None


def retract_edge(c: psycopg.Connection, *, user_id: str, edge_id: str, actor: str | None = None,
                 mode: str = "retract") -> dict | None:
    """mode=retract → status='retracted' (kept for history); archive → 'archived'; hard → DELETE.
    Returns the (final) row or None when not found."""
    eid = str(uuid.UUID(str(edge_id)))
    with c.cursor() as cur:
        if mode == "hard":
            row = cur.execute(f"DELETE FROM edge WHERE id=%s AND user_id=%s RETURNING {EDGE_COLS}", (eid, user_id)).fetchone()
        else:
            st = "archived" if mode == "archive" else "retracted"
            row = cur.execute(f"UPDATE edge SET status=%s WHERE id=%s AND user_id=%s AND status='active' RETURNING {EDGE_COLS}",
                              (st, eid, user_id)).fetchone()
            if not row:  # already non-active: report it unchanged (idempotent)
                row = cur.execute(f"SELECT {EDGE_COLS} FROM edge WHERE id=%s AND user_id=%s", (eid, user_id)).fetchone()
        if row:
            _audit(cur, user_id, actor, f"edge_{mode}", row["id"],
                   {"src": node_ref(row["src_kind"], row["src_id"]), "dst": node_ref(row["dst_kind"], row["dst_id"]),
                    "relation": row["relation"]})
    return dict(row) if row else None


def list_edges(c: psycopg.Connection, *, user_id: str, node: Any = None, relation: str | None = None,
               depth: int = 0, status: str | None = "active", limit: int = 200, offset: int = 0,
               max_fanout: int | None = None) -> list[dict]:
    """Edges of a user. `node` → edges touching it; `depth` > 0 → edges touching any node within
    `depth` hops of it (undirected walk). `relation` filters; `status` 'any' lifts the active filter."""
    where, args = ["user_id=%s"], [user_id]
    if node is not None and str(node).strip():
        k, nid = parse_node(node)
        if k == "entity":
            nid = resolve_alias(c, user_id, nid) or nid
        refs = {node_ref(k, nid)}
        if depth and int(depth) > 0:
            for nb in neighbors(c, user_id, [node_ref(k, nid)], max_depth=int(depth),
                                max_fanout=max_fanout or settings().graph_max_fanout):
                refs.add(nb["node"])
        where.append("((src_kind || ':' || src_id) = ANY(%s) OR (dst_kind || ':' || dst_id) = ANY(%s))")
        args += [sorted(refs), sorted(refs)]
    if relation:
        where.append("relation=%s"); args.append(canon_relation(relation))
    if status and status != "any":
        where.append("status=%s"); args.append(status)
    rows = c.execute(f"SELECT {EDGE_COLS} FROM edge WHERE {' AND '.join(where)} "
                     f"ORDER BY weight DESC, asserted_at DESC LIMIT %s OFFSET %s", (*args, limit, offset)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# traversal

def neighbors(c: psycopg.Connection, user_id: str, node_ids: Iterable[Any], max_depth: int = 2,
              max_fanout: int = 20, max_results: int | None = None) -> list[dict]:
    """Undirected bounded walk from `node_ids` ('kind:id' strings) over ACTIVE edges.
    Per hop each node follows at most `max_fanout` strongest edges (weight, confidence, recency);
    a per-path visited set keeps cycles from looping. Returns reached nodes (seeds excluded), nearest
    hop first: [{node, kind, id, hops, via, direction, path: [nodes...], relations: [...]}].
    `max_results` caps the output (default max_fanout * max_depth * 2)."""
    seeds: list[tuple[str, str]] = []
    seen: set[str] = set()
    for n in node_ids or ():
        k, nid = parse_node(n)
        ref = node_ref(k, nid)
        if ref not in seen:
            seen.add(ref); seeds.append((k, nid))
    if not seeds:
        return []
    depth = max(0, min(int(max_depth), 6))
    fan = max(1, min(int(max_fanout), 200))
    cap = int(max_results or fan * max(1, depth) * 2)
    if depth == 0:
        return []
    seed_refs = [node_ref(k, i) for k, i in seeds]
    sql = """
WITH RECURSIVE walk(node, kind, id, hops, via, direction, path, rels) AS (
  SELECT s.kind || ':' || s.id, s.kind, s.id, 0, NULL::text, NULL::text, ARRAY[s.kind || ':' || s.id], ARRAY[]::text[]
  FROM unnest(%(kinds)s::text[], %(ids)s::text[]) AS s(kind, id)
  UNION ALL
  SELECT nb.node, nb.kind, nb.id, w.hops + 1, nb.relation, nb.direction, w.path || nb.node, w.rels || nb.relation
  FROM walk w
  JOIN LATERAL (
    SELECT CASE WHEN e.src_kind = w.kind AND e.src_id = w.id THEN e.dst_kind ELSE e.src_kind END AS kind,
           CASE WHEN e.src_kind = w.kind AND e.src_id = w.id THEN e.dst_id   ELSE e.src_id   END AS id,
           CASE WHEN e.src_kind = w.kind AND e.src_id = w.id THEN e.dst_kind || ':' || e.dst_id
                ELSE e.src_kind || ':' || e.src_id END AS node,
           CASE WHEN e.src_kind = w.kind AND e.src_id = w.id THEN 'out' ELSE 'in' END AS direction,
           e.relation
    FROM edge e
    WHERE e.user_id = %(uid)s AND e.status = 'active'
      AND ((e.src_kind = w.kind AND e.src_id = w.id) OR (e.dst_kind = w.kind AND e.dst_id = w.id))
    ORDER BY e.weight DESC, e.confidence DESC, e.asserted_at DESC
    LIMIT %(fan)s
  ) nb ON TRUE
  WHERE w.hops < %(depth)s AND NOT (nb.node = ANY(w.path))
)
SELECT DISTINCT ON (node) node, kind, id, hops, via, direction, path, rels
FROM walk
WHERE hops > 0 AND NOT (node = ANY(%(seeds)s::text[]))
ORDER BY node, hops, array_length(path, 1)
"""
    rows = c.execute(sql, {"kinds": [k for k, _ in seeds], "ids": [i for _, i in seeds], "uid": user_id,
                           "fan": fan, "depth": depth, "seeds": seed_refs}).fetchall()
    out = [{"node": r["node"], "kind": r["kind"], "id": r["id"], "hops": int(r["hops"]), "via": r["via"],
            "direction": r["direction"], "path": list(r["path"]), "relations": list(r["rels"])} for r in rows]
    out.sort(key=lambda r: (r["hops"], r["node"]))
    return out[:cap]


def row_public(r: dict | None) -> dict | None:
    """JSON-safe edge/alias/entity row + 'src'/'dst' node refs for edges."""
    if r is None:
        return None
    out: dict[str, Any] = {}
    for k, v in r.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif isinstance(v, uuid.UUID):
            v = str(v)
        out[k] = v
    if "src_kind" in out and "src_id" in out:
        out["src"] = node_ref(out["src_kind"], out["src_id"])
        out["dst"] = node_ref(out["dst_kind"], out["dst_id"])
    return out
