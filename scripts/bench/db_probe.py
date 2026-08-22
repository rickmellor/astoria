#!/usr/bin/env python3
"""DB-only probe — runs the service's OWN recall code against Postgres with pre-embedded query vectors.

Why a separate file: the NAS Postgres is loopback-only and an ssh tunnel adds 40-100 ms stalls on the
large vector-parameter packets, which would swamp a "DB-only" number. loadgen therefore ships this
script (plus a JSON payload appended as `main({...})`) to `docker exec -i astoria python -` so it runs
in the service container, on the service's network path, with the service's psycopg/pgvector.
It also works locally (BENCH_DSN) for dev setups where Postgres is reachable directly.

Payload ops (all return one JSON object on the LAST stdout line):
  {"op": "recall", "user_id", "queries": [[text, vec|null], ...], "session_id"?, "limit"?,
   "modes": ["off", "relaxed_order"], "repeat": 1, "rerank": false}
                                                             → per mode: lat_ms[], items' predicates,
                                                             vector candidates-of-40 per query, hnsw gucs
  {"op": "recall_concurrent", "user_id", "queries", "clients": [1,4,8], "seconds": 20}
                                                             → DB-only recall with N parallel connections
  {"op": "filter", "user_id", "n": 20}                        → filtered-HNSW candidates of k per iterative_scan mode
  {"op": "breakdown", "user_id", "queries": [...]}           → per-SQL-step medians (vec/bm25/stale/snapshot)
  {"op": "explain", "user_id", "query", "vec", "tsq"}        → EXPLAIN (ANALYZE, BUFFERS) texts + gucs
  {"op": "count_active", "user_id", "subject", "predicate"}   → {"n": int}
  {"op": "sql", "sql", "args"?}                               → rows (read-only ad-hoc, e.g. sizes)
"""
from __future__ import annotations

import json
import os
import sys
import time


def _conn():
    import psycopg
    from pgvector.psycopg import register_vector
    from psycopg.rows import dict_row
    dsn = os.environ.get("BENCH_DSN") or os.environ.get("ASTORIA_DB_DSN")
    if not dsn:
        raise SystemExit("BENCH_DSN / ASTORIA_DB_DSN required")
    c = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    register_vector(c)
    return c


def _recall_mod():
    for p in ("/app", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))):
        if p not in sys.path:
            sys.path.insert(0, p)
    from astoria.retrieval import recall as R
    return R


def _rerank_kw(R, p: dict) -> dict:
    """DB-only = the store alone: the cross-encoder stage is OFF unless the payload says {"rerank": true}
    (only when the deployed recall() has the parameter)."""
    import inspect
    if "rerank" not in inspect.signature(R.recall).parameters:
        return {}
    return {"rerank": bool(p.get("rerank", False))}


def _pct(xs, p):
    s = sorted(xs)
    if not s:
        return None
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _gucs(c) -> dict:
    out = {}
    for k in ("hnsw.ef_search", "hnsw.iterative_scan", "hnsw.max_scan_tuples", "hnsw.scan_mem_multiplier",
              "shared_buffers", "work_mem", "maintenance_work_mem", "effective_cache_size", "max_connections"):
        try:
            out[k] = c.execute(f"SHOW {k}").fetchone()[k]
        except Exception as e:  # noqa: BLE001
            c.rollback(); out[k] = f"? {type(e).__name__}"
    c.commit()
    return out


def op_recall(c, R, p: dict) -> dict:
    u, qs = p["user_id"], p["queries"]
    limit = int(p.get("limit", 12)); sid = p.get("session_id")
    out: dict = {"gucs_before": _gucs(c), "modes": {}}
    for mode in p.get("modes", ["off"]):
        c.execute(f"SET hnsw.iterative_scan = {mode}"); c.commit()
        lats, preds, vec_n = [], [], []
        for _ in range(int(p.get("repeat", 1))):
            for q, vec in qs:
                R.embed_one = lambda text, query=True, _v=vec: _v
                t = time.perf_counter()
                res = R.recall(c, user_id=u, query=q, session_id=sid, limit=limit, **_rerank_kw(R, p))
                c.commit()
                lats.append((time.perf_counter() - t) * 1000)
                preds.append([it.get("predicate") for it in res["items"]])
        # how many HNSW candidates survive the user/status/layer filter (the < k question)
        for q, vec in qs:
            if vec is None:
                continue
            c.execute("SET LOCAL hnsw.ef_search = 64")
            n = c.execute("SELECT count(*) AS n FROM (SELECT id FROM fact WHERE user_id=%s AND status='active' AND "
                          "(valid_to IS NULL OR valid_to > now()) AND layer = ANY(%s) AND embedding IS NOT NULL "
                          "ORDER BY embedding <=> %s::vector LIMIT 40) s",
                          (u, ["profile", "semantic", "procedural"], vec)).fetchone()["n"]
            c.commit(); vec_n.append(n)
        out["modes"][mode] = {"lat_ms": [round(x, 2) for x in lats], "items_predicates": preds,
                              "vec_candidates_of_40": vec_n,
                              "p50": round(_pct(lats, .5), 1), "p95": round(_pct(lats, .95), 1),
                              "p99": round(_pct(lats, .99), 1), "max": round(max(lats), 1)}
    c.execute("SET hnsw.iterative_scan = off"); c.commit()
    return out


def op_recall_concurrent(c, R, p: dict) -> dict:
    """DB-only recall with N parallel connections for `seconds` (the store's own concurrent capacity,
    TEI excluded). Each thread: own connection, the service's recall(), pre-embedded vectors."""
    import random
    import threading
    u, qs = p["user_id"], p["queries"]
    out = {}
    for n in p.get("clients", [1, 4, 8]):
        lats: list[float] = []; errs = [0]; lock = threading.Lock()
        stop = time.perf_counter() + float(p.get("seconds", 20))

        def worker(k):
            cc = _conn(); rnd = random.Random(k)
            cc.execute(f"SET hnsw.iterative_scan = {p.get('iterative_scan', 'off')}"); cc.commit()
            RR = _recall_mod()
            while time.perf_counter() < stop:
                q, vec = rnd.choice(qs)
                RR.embed_one = lambda text, query=True, _v=vec: _v
                t = time.perf_counter()
                try:
                    RR.recall(cc, user_id=u, query=q, limit=12, **_rerank_kw(RR, p)); cc.commit()
                except Exception:  # noqa: BLE001
                    cc.rollback()
                    with lock:
                        errs[0] += 1
                with lock:
                    lats.append((time.perf_counter() - t) * 1000)
            cc.close()

        t0 = time.perf_counter()
        ths = [threading.Thread(target=worker, args=(k,)) for k in range(n)]
        [t.start() for t in ths]; [t.join() for t in ths]
        el = time.perf_counter() - t0
        out[str(n)] = {"n": len(lats), "errors": errs[0], "p50_ms": round(_pct(lats, .5), 1), "p95_ms": round(_pct(lats, .95), 1),
                       "p99_ms": round(_pct(lats, .99), 1), "max_ms": round(max(lats), 1), "elapsed_s": round(el, 1),
                       "rps": round(len(lats) / el, 2), "clients": n}
    return out


def op_breakdown(c, R, p: dict) -> dict:
    u, qs = p["user_id"], p["queries"]
    layers = ["profile", "semantic", "procedural"]
    steps: dict[str, list[float]] = {}

    def T(name, fn):
        t = time.perf_counter(); r = fn(); steps.setdefault(name, []).append((time.perf_counter() - t) * 1000); return r

    for q, vec in qs:
        tsq = R._or_tsquery(q)
        with c.transaction():
            fv = T("fact_vec(hnsw)", lambda: R._fact_vec(c, u, vec, layers, 0.48, 40)) if vec else []
            fb = T("fact_bm25(gin)", lambda: R._fact_bm25(c, u, tsq, layers, 40)) if tsq else []
            ev = T("episode_vec(hnsw)", lambda: R._episode_vec(c, u, vec, 0.48, 20, None, None)) if vec else []
            eb = T("episode_bm25(gin)", lambda: R._episode_bm25(c, u, tsq, 20, None, None)) if tsq else []
            now = R._now()
            fr = T("score+collapse(py)", lambda: R._score_facts(R._rrf(fv, fb), now))
            sel = fr[:12]
            try:   # graph expansion (bounded walk from the top-10 seeds + facts about their subjects) — present since 003
                from astoria.retrieval.graph import expand_candidates
                T("graph_expand(sql)", lambda: expand_candidates(c, u, [r["id"] for r in fr[:10]]))
            except Exception:  # noqa: BLE001
                pass
            if sel:
                T("stale_hints(sql)", lambda: R._stale_hints(c, u, sel))
            T("snapshot+touch(sql)", lambda: (
                c.execute("INSERT INTO snapshot(user_id, client, query, fact_ids, episode_ids) VALUES (%s,'bench',%s,%s,%s) RETURNING id",
                          (u, q, [r["id"] for r in sel], [])).fetchone(),
                c.execute("UPDATE fact SET access_count=access_count+1, last_seen=now() WHERE id = ANY(%s)", ([r["id"] for r in sel],))))
            steps.setdefault("_counts", []).append(len(fv) + len(fb) + len(ev) + len(eb))
    return {k: {"p50": round(_pct(v, .5), 2), "p95": round(_pct(v, .95), 2), "n": len(v)} for k, v in steps.items()}


def op_filter(c, R, p: dict) -> dict:
    """The filtered-HNSW question: with N users sharing one index, how many of LIMIT k survive the
    user_id/status/layer post-filter at ef_search=64, per iterative_scan mode — and what does it cost?
    Uses RANDOM unit query vectors against `user_id` so the query sits in the same distribution as
    the bulk data (bench users' vectors are random); `bench-real` would be misleading here."""
    import random
    u = p["user_id"]; n = int(p.get("n", 20)); k = int(p.get("k", 40)); ef = int(p.get("ef_search", 64))
    rng = random.Random(int(p.get("seed", 1)))
    layers = ["profile", "semantic", "procedural"]
    out: dict = {}
    vecs = []
    for _ in range(n):
        v = [rng.gauss(0, 1) for _ in range(768)]; s = sum(x * x for x in v) ** 0.5
        vecs.append([x / s for x in v])
    for mode in p.get("modes", ["off", "relaxed_order"]):
        c.execute(f"SET hnsw.iterative_scan = {mode}"); c.commit()
        cnts, lats, ecnts, elats = [], [], [], []
        for v in vecs:
            t = time.perf_counter()
            rows = R._fact_vec(c, u, v, layers, -1.0, k)       # min_cos -1: count everything HNSW hands back
            c.commit(); lats.append((time.perf_counter() - t) * 1000); cnts.append(len(rows))
            t = time.perf_counter()
            rows = R._episode_vec(c, u, v, -1.0, 20, None, None)
            c.commit(); elats.append((time.perf_counter() - t) * 1000); ecnts.append(len(rows))
        out[mode] = {"fact_candidates_of_%d" % k: {"min": min(cnts), "mean": round(sum(cnts) / n, 1), "max": max(cnts), "lt_k": sum(1 for x in cnts if x < k)},
                     "fact_vec_ms": {"p50": round(_pct(lats, .5), 1), "p95": round(_pct(lats, .95), 1)},
                     "episode_candidates_of_20": {"min": min(ecnts), "mean": round(sum(ecnts) / n, 1), "max": max(ecnts), "lt_k": sum(1 for x in ecnts if x < 20)},
                     "episode_vec_ms": {"p50": round(_pct(elats, .5), 1), "p95": round(_pct(elats, .95), 1)}}
    c.execute("SET hnsw.iterative_scan = off"); c.commit()
    out["user_rows"] = c.execute("SELECT count(*) AS n FROM fact WHERE user_id=%s AND status='active'", (u,)).fetchone()["n"]
    out["total_rows"] = c.execute("SELECT count(*) AS n FROM fact").fetchone()["n"]
    return out


def op_explain(c, R, p: dict) -> dict:
    u, v, tsq = p["user_id"], p["vec"], p["tsq"]
    layers = ["profile", "semantic", "procedural"]
    kinds = ["summary", "note", "import", "turn"]
    plans: dict[str, str] = {}

    def run(name, sql, args, pre=None):
        with c.transaction():
            for s in (pre or []):
                c.execute(s)
            rows = c.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql, args).fetchall()
        import re
        txt = "\n".join(r["QUERY PLAN"] for r in rows)
        plans[name] = re.sub(r"'\[[-0-9.e,+]+\]'::vector", "'[<768-d query vector>]'::vector", txt)

    vec_sql = ("SELECT id, 1 - (embedding <=> %s::vector) AS cosine FROM fact WHERE user_id=%s AND status='active' "
               "AND (valid_to IS NULL OR valid_to > now()) AND layer = ANY(%s) AND embedding IS NOT NULL "
               "ORDER BY embedding <=> %s::vector LIMIT 40")
    run("fact vector (ef_search=64, iterative_scan=off)", vec_sql, (v, u, layers, v), ["SET LOCAL hnsw.ef_search = 64"])
    run("fact vector (ef_search=64, iterative_scan=relaxed_order)", vec_sql, (v, u, layers, v),
        ["SET LOCAL hnsw.ef_search = 64", "SET LOCAL hnsw.iterative_scan = relaxed_order"])
    run("fact vector (ef_search=40 default)", vec_sql, (v, u, layers, v))
    run("fact BM25 (GIN)", "SELECT id, ts_rank_cd(tsv, to_tsquery('english', %s)) AS rank FROM fact WHERE user_id=%s AND "
        "status='active' AND (valid_to IS NULL OR valid_to > now()) AND layer = ANY(%s) AND tsv @@ to_tsquery('english', %s) "
        "ORDER BY rank DESC, asserted_at DESC LIMIT 40", (tsq, u, layers, tsq))
    run("episode vector (ef_search=64)", "SELECT id FROM episode WHERE user_id=%s AND status='active' AND kind = ANY(%s) "
        "AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT 20", (u, kinds, v), ["SET LOCAL hnsw.ef_search = 64"])
    run("episode BM25 (GIN)", "SELECT id, ts_rank_cd(tsv, to_tsquery('english', %s)) AS rank FROM episode WHERE user_id=%s "
        "AND status='active' AND kind = ANY(%s) AND tsv @@ to_tsquery('english', %s) ORDER BY rank DESC, occurred_at DESC LIMIT 20",
        (tsq, u, kinds, tsq))
    run("history (fact_key)", "SELECT id FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s ORDER BY asserted_at DESC, ingested_at DESC",
        (u, p.get("chain_subject", u), p.get("chain_predicate", "current_focus")))
    run("as_of unscoped (limit 50)", "SELECT DISTINCT ON (subject, predicate, CASE WHEN cardinality='set' THEN value_norm ELSE '' END) id "
        "FROM fact WHERE user_id=%s AND valid_from <= now() AND (valid_to IS NULL OR valid_to > now()) AND status IN ('active','superseded') "
        "ORDER BY subject, predicate, CASE WHEN cardinality='set' THEN value_norm ELSE '' END, asserted_at DESC LIMIT 50", (u,))
    run("stale_hints (1 key)", "SELECT 1 FROM episode e WHERE e.user_id=%s AND e.status='active' AND e.occurred_at > now() - interval '400 days' "
        "AND e.tsv @@ plainto_tsquery('english', 'favorite beer') AND NOT ((e.hook||' '||e.body) ILIKE '%%IPA%%') LIMIT 1", (u,))
    return {"plans": plans, "gucs": _gucs(c)}


def main(p: dict) -> None:
    c = _conn()
    R = _recall_mod()
    op = p["op"]
    t0 = time.perf_counter()
    if op == "recall":
        out = op_recall(c, R, p)
    elif op == "recall_concurrent":
        out = op_recall_concurrent(c, R, p)
    elif op == "filter":
        out = op_filter(c, R, p)
    elif op == "breakdown":
        out = op_breakdown(c, R, p)
    elif op == "explain":
        out = op_explain(c, R, p)
    elif op == "count_active":
        out = {"n": c.execute("SELECT count(*) AS n FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s AND status='active'",
                              (p["user_id"], p["subject"], p["predicate"])).fetchone()["n"]}
    elif op == "sql":
        out = {"rows": c.execute(p["sql"], p.get("args")).fetchall()}
    else:
        out = {"error": f"unknown op {op}"}
    out["probe_s"] = round(time.perf_counter() - t0, 2)
    c.close()
    sys.stdout.write("\n" + json.dumps(out, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__" and len(sys.argv) > 1:
    main(json.loads(sys.argv[1]))
