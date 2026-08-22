"""Graph expansion for recall + the /graph rendering (read path, no LLM).

`expand_candidates` is wired in by recall AFTER RRF: the fused fact candidates are the seeds, the
facts reachable over ACTIVE edges (fact→fact, fact→entity→fact, and the facts *about* a reached
entity — subject == entity name) come back as extra candidates carrying `graph_hops`, so the caller
can add them with a hop penalty. Everything is bounded: `settings.graph_max_depth` hops,
`settings.graph_max_fanout` edges per node per hop, `max_results` rows total.

Hop accounting:
    seed fact                         hop 0
    entity:<seed.subject>             hop 0   (implicit fact→its subject link; the user hub is skipped)
    node reached over k edges         hop k
    fact about a reached entity       hop(entity) + 1  (never beyond `depth`)
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

import psycopg

from astoria.config import settings
from astoria.store import graph as gstore

log = logging.getLogger("astoria.retrieval.graph")

FACT_COLS = ("id, user_id, subject, predicate, cardinality, value, hook, detail, layer, valid_from, valid_to, "
             "asserted_at, ingested_at, expired_at, status, confidence, source, source_kind, source_trust, "
             "is_belief, importance, last_seen, access_count, corroborations, tags, origin_episode, evidence, ref, meta")


def _fact_rows(c: psycopg.Connection, user_id: str, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = c.execute(f"SELECT {FACT_COLS} FROM fact WHERE user_id=%s AND status='active' AND id = ANY(%s::uuid[])",
                     (user_id, ids)).fetchall()
    return {str(r["id"]): dict(r) for r in rows}


def _subject_facts(c: psycopg.Connection, user_id: str, subject: str, exclude: Iterable[str], limit: int) -> list[dict]:
    rows = c.execute(
        f"SELECT {FACT_COLS} FROM fact WHERE user_id=%s AND status='active' AND subject=%s "
        f"AND NOT (id = ANY(%s::uuid[])) ORDER BY importance DESC, confidence DESC, last_seen DESC LIMIT %s",
        (user_id, subject, list(exclude) or [], limit)).fetchall()
    return [dict(r) for r in rows]


def expand_candidates(c: psycopg.Connection, user_id: str, seed_fact_ids: Iterable[Any],
                      depth: int | None = None, fanout: int | None = None, max_results: int | None = None,
                      seed_subjects: bool = True) -> list[dict]:
    """Facts reachable from `seed_fact_ids` through the graph, as fact rows (FACT_COLS projection)
    + `graph_hops` (int ≥ 1), `graph_via` (relation of the last hop or 'subject'), `graph_path`
    (node refs from the seed). Seeds themselves are never returned; each fact appears once (nearest
    hop). Sorted by hops asc, then importance desc. Bounded by depth/fanout/max_results."""
    s = settings()
    depth = int(s.graph_max_depth if depth is None else depth)
    fanout = int(s.graph_max_fanout if fanout is None else fanout)
    cap = int(max_results or max(fanout * 2, 1))
    seeds: list[str] = []
    for x in seed_fact_ids or ():
        try:
            k, nid = gstore.parse_node(x)
        except ValueError:
            continue
        if k == "fact" and nid not in seeds:
            seeds.append(nid)
    if not seeds or depth <= 0:
        return []

    start_nodes = [gstore.node_ref("fact", fid) for fid in seeds]
    entity_hops: dict[str, tuple[int, str | None, list[str]]] = {}     # entity name → (hops, via, path)
    if seed_subjects:
        subs = c.execute("SELECT id, subject FROM fact WHERE user_id=%s AND id = ANY(%s::uuid[])",
                         (user_id, seeds)).fetchall()
        for r in subs:
            subj = r["subject"]
            if subj and subj != user_id:
                ref = gstore.node_ref("entity", subj)
                if ref not in start_nodes:
                    start_nodes.append(ref)
                entity_hops.setdefault(subj, (0, None, [gstore.node_ref("fact", str(r["id"])), ref]))

    reached = gstore.neighbors(c, user_id, start_nodes, max_depth=depth, max_fanout=fanout,
                               max_results=max(cap * 4, fanout * depth * 2))
    fact_hits: dict[str, tuple[int, str | None, list[str]]] = {}      # fact id → (hops, via, path)
    for nb in reached:
        if nb["kind"] == "fact":
            if nb["id"] not in seeds:
                cur = fact_hits.get(nb["id"])
                if cur is None or nb["hops"] < cur[0]:
                    fact_hits[nb["id"]] = (nb["hops"], nb["via"], nb["path"])
        else:
            cur = entity_hops.get(nb["id"])
            if cur is None or nb["hops"] < cur[0]:
                entity_hops[nb["id"]] = (nb["hops"], nb["via"], nb["path"])

    out: list[dict] = []
    rows = _fact_rows(c, user_id, list(fact_hits))
    for fid, (hops, via, path) in fact_hits.items():
        r = rows.get(fid)
        if r:
            r.update(graph_hops=hops, graph_via=via, graph_path=path)
            out.append(r)
    have = set(seeds) | set(fact_hits)
    for ent, (hops, via, path) in sorted(entity_hops.items(), key=lambda kv: kv[1][0]):
        if hops + 1 > depth:
            continue
        if ent == user_id:          # the user hub would pull in the whole profile — skip
            continue
        for r in _subject_facts(c, user_id, ent, have, fanout):
            fid = str(r["id"])
            have.add(fid)
            r.update(graph_hops=hops + 1, graph_via="subject", graph_path=path + [gstore.node_ref("fact", fid)])
            out.append(r)
    out.sort(key=lambda r: (r["graph_hops"], -float(r.get("importance") or 0), str(r["id"])))
    return out[:cap]


# ---------------------------------------------------------------------------
# /graph rendering

def _node_label(c: psycopg.Connection, user_id: str, kind: str, nid: str) -> dict:
    if kind == "fact":
        r = c.execute("SELECT id, subject, predicate, value, hook, status, layer, confidence FROM fact "
                      "WHERE id=%s AND user_id=%s", (nid, user_id)).fetchone()
        if r:
            return {"label": r["hook"], "subject": r["subject"], "predicate": r["predicate"], "value": r["value"],
                    "status": r["status"], "layer": r["layer"], "confidence": float(r["confidence"])}
        return {"label": f"fact {nid[:8]} (missing)", "status": "missing"}
    e = gstore.get_entity(c, user_id=user_id, name=nid)
    aliases = [a["alias"] for a in gstore.list_aliases(c, user_id=user_id, canonical=nid, limit=50)]
    n_facts = c.execute("SELECT count(*) AS n FROM fact WHERE user_id=%s AND subject=%s AND status='active'",
                        (user_id, nid)).fetchone()["n"]
    return {"label": nid, "entity_kind": (e or {}).get("kind"), "summary": (e or {}).get("summary"),
            "aliases": aliases, "facts": int(n_facts)}


def render_graph(c: psycopg.Connection, user_id: str, node: Any, depth: int | None = None,
                 fanout: int | None = None) -> dict:
    """Induced subgraph around `node` within `depth` hops:
    {"root": ref, "depth", "nodes": [{id(ref), kind, name, hops, via, path, label, ...}], "edges": [edge rows]}."""
    s = settings()
    depth = int(s.graph_max_depth if depth is None else depth)
    fanout = int(s.graph_max_fanout if fanout is None else fanout)
    k, nid = gstore.parse_node(node)
    if k == "entity":
        nid = gstore.resolve_alias(c, user_id, nid) or nid
    root = gstore.node_ref(k, nid)
    nodes: list[dict] = [{"id": root, "kind": k, "name": nid, "hops": 0, "via": None, "path": [root],
                          **_node_label(c, user_id, k, nid)}]
    for nb in gstore.neighbors(c, user_id, [root], max_depth=depth, max_fanout=fanout):
        nodes.append({"id": nb["node"], "kind": nb["kind"], "name": nb["id"], "hops": nb["hops"], "via": nb["via"],
                      "direction": nb["direction"], "path": nb["path"], **_node_label(c, user_id, nb["kind"], nb["id"])})
    refs = [n["id"] for n in nodes]
    edges = []
    if refs:
        rows = c.execute(
            f"SELECT {gstore.EDGE_COLS} FROM edge WHERE user_id=%s AND status='active' "
            f"AND (src_kind || ':' || src_id) = ANY(%s) AND (dst_kind || ':' || dst_id) = ANY(%s) "
            f"ORDER BY weight DESC, asserted_at DESC LIMIT %s", (user_id, refs, refs, max(50, fanout * len(refs)))).fetchall()
        edges = [gstore.row_public(dict(r)) for r in rows]
    return {"root": root, "depth": depth, "nodes": nodes, "edges": edges,
            "counts": {"nodes": len(nodes), "edges": len(edges)}}
