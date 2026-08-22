#!/usr/bin/env python3
"""Load generator / measurement phases for the Astoria performance validation.

Every phase prints a markdown-ish block and (with --out FILE) appends a JSON record so a report can be
assembled later. All user_ids must start with `bench-` (enforced).

Phases (positional):
  tei                       TEI query-embed latency floor (sequential + 4/8-way concurrent)
  baseline  USER            30 recalls e2e + DB-only, 30 captures (cognify=false), 30 POST /facts
  real-seed USER [--n 500]  write realistic triples through POST /facts (real TEI vectors)
  recall    USER [--n 50]   recall p50/p95/p99 e2e + DB-only + semantic hit-rate on REAL_QUERIES
                            (DB-only also re-runs with hnsw.iterative_scan=relaxed_order for comparison)
  concurrency USER [--clients 1,4,8,16] [--seconds 60]   recall-only throughput sweep (+ docker stats)
  mixed     USER [--seconds 60]  8 recall + 4 capture + 2 POST /facts clients
  correct   USER            20 concurrent POST /correct on ONE functional key → exactly-1-active check
  chain     USER            /history and /as_of on the 50-long supersede chain (bench-u00 current_focus)
  explain   USER            EXPLAIN (ANALYZE, BUFFERS) for the HNSW and GIN candidate queries
  db-concurrency USER [--clients 1,4,8,16] [--seconds 20]   DB-only recall with N parallel connections
                            (inside the container; TEI excluded) — the store's own concurrent capacity
  filter    USER [--n 20]   filtered-HNSW check on a bulk user: candidates of LIMIT 40 surviving the user
                            filter, iterative_scan off vs relaxed_order (run on a bench-uNN, not bench-real)
  worker    USER [--turns 100] [--seconds 60]   enqueue cognify=true turns; recall p95 while the worker drains
  wipe-http USER            DELETE /users/{USER} (the REST wipe path)
  embed-floor               query-embed latency per embedding endpoint (workstation seat via SAINT vs NAS TEI), 1/4/8-way
  rerank-floor              cross-encoder latency per reranker endpoint (30 hooks/request), 1/4/8-way
  embed-gap USER [--n 20]   async write path: capture N turns + POST N facts, then poll until the worker has
                            embedded them (time-to-embedded = the recall gap of the async write path)
  correct-seq USER [--n 50] N sequential POST /correct on ONE functional key (the API-built supersede chain
                            with belief-axis versioning) → latency, rows per supersede, /history, /as_of

Global flags: --rerank on|off (adds `rerank: true|false` to every /recall body; default = service setting),
              --unique (append a per-call nonce to recall queries so the service's query-embed / rerank LRU
              caches never hit — the cold-path number; default re-uses the 30 fixed queries = warm caches).

Environment: ASTORIA_URL, BENCH_DSN (DB-only phases), TEI_URL, BENCH_SSH — see common.py.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ASTORIA_URL, BENCH_DSN, BENCH_PREFIX, QUERIES, REAL_QUERIES, TABLE_HEADER, StatsSampler,  # noqa: E402
                    fmt_row, oom_events, real_facts, summarize, tei_embed)

HDR = {"X-Astoria-Client": "bench"}
RERANK: bool | None = None       # --rerank on|off → every /recall body carries rerank: true|false
UNIQUE = False                   # --unique → nonce per recall query (defeats the service's embed/rerank caches)
_NONCE = [int(time.time() * 1000) % 10**7]
_NONCE_LOCK = threading.Lock()
EMBED_ENDPOINTS = os.environ.get("BENCH_EMBED_ENDPOINTS",
                                 "workstation=http://192.168.1.221:4000|saint-local-embed,nas=http://192.168.1.134:8931|nomic")
RERANK_ENDPOINTS = os.environ.get("BENCH_RERANK_ENDPOINTS",
                                  "workstation=http://192.168.1.221:8935,nas=http://192.168.1.134:8935")


def _recall_body(u: str, q: str, **extra) -> dict:
    """/recall body honoring --rerank / --unique."""
    if UNIQUE:
        with _NONCE_LOCK:
            _NONCE[0] += 1
            q = f"{q} #{_NONCE[0]}"
    body = {"user_id": u, "query": q, "limit": 12, **extra}
    if RERANK is not None:
        body["rerank"] = RERANK
    return body


def _parse_endpoints(spec: str) -> list[tuple[str, str, str]]:
    """'name=url|model,name=url' → [(name, url, model)]"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, rest = part.partition("=")
        url, _, model = rest.partition("|")
        out.append((name.strip(), url.strip().rstrip("/"), model.strip()))
    return out


def _client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(base_url=ASTORIA_URL, timeout=timeout, headers=HDR)


def _check_user(u: str) -> str:
    if not u.startswith(BENCH_PREFIX):
        raise SystemExit(f"refusing to touch non-bench user {u!r} (must start with {BENCH_PREFIX})")
    return u


def _emit(out: str | None, rec: dict) -> None:
    rec["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(json.dumps(rec, default=str))
    if out:
        with open(out, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")


def _timed(fn) -> tuple[bool, float]:
    t = time.perf_counter()
    try:
        ok = fn()
    except Exception:  # noqa: BLE001
        ok = False
    return bool(ok), (time.perf_counter() - t) * 1000


# ---------------------------------------------------------------------------
# DB-only probe: runs scripts/bench/db_probe.py INSIDE the service container (BENCH_DB_EXEC) so the
# numbers exclude the ssh tunnel; falls back to running it locally against BENCH_DSN.

# Default: ALWAYS in-container (a BENCH_DSN export for seed.py must not silently move the DB-only probe onto the
# ssh tunnel — that path adds 40-100 ms per vector query and once produced 375 ms "DB-only" recalls that were
# really 30 ms). Set BENCH_DB_EXEC="" explicitly to run the probe locally against BENCH_DSN.
BENCH_DB_EXEC = os.environ.get("BENCH_DB_EXEC", "ssh -o BatchMode=yes root-dxp4800gt docker exec -i astoria python -")
PROBE_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "db_probe.py")).read()


def _probe(payload: dict, timeout: float = 1800) -> dict:
    """Run one db_probe op; returns its result dict."""
    import subprocess
    script = PROBE_SRC + "\nmain(" + json.dumps(payload) + ")\n"
    if BENCH_DB_EXEC:
        r = subprocess.run(BENCH_DB_EXEC.split(), input=script, capture_output=True, text=True, timeout=timeout)
        out = r.stdout
    else:
        r = subprocess.run([sys.executable, "-"], input=script, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "BENCH_DSN": BENCH_DSN})
        out = r.stdout
    lines = [l for l in out.splitlines() if l.strip()]
    if r.returncode != 0 or not lines:
        raise RuntimeError(f"db_probe failed rc={r.returncode}: {r.stderr[-2000:]}")
    return json.loads(lines[-1])


def _embed_queries(qs: list[str], random_vectors: bool = False) -> tuple[list[tuple[str, list[float] | None]], list[float]]:
    """TEI-embed the queries; with random_vectors=True use random unit vectors instead (for bulk bench
    users whose stored vectors are random — puts the query in the same distribution as the data)."""
    out, lat = [], []
    rng = random.Random(42)
    for q in qs:
        if random_vectors:
            v = [rng.gauss(0, 1) for _ in range(768)]; n = sum(x * x for x in v) ** 0.5
            out.append((q, [x / n for x in v])); lat.append(0.0)
        else:
            v, ms = tei_embed(q, query=True)
            out.append((q, v)); lat.append(ms)
    return out, lat


def _probe_recall(user_id: str, qs: list[tuple[str, list[float] | None]], modes=("off",), repeat: int = 1) -> dict:
    return _probe({"op": "recall", "user_id": user_id, "queries": qs, "modes": list(modes), "repeat": repeat})


def _mode_summary(m: dict) -> dict:
    return summarize(m["lat_ms"])


# ---------------------------------------------------------------------------
# phases

def phase_tei(a) -> dict:
    seq = [tei_embed(q)[1] for q in QUERIES[:20]]
    res = {"phase": "tei", "sequential": summarize(seq)}
    for conc in (4, 8):
        lat: list[float] = []
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(tei_embed, QUERIES[i % len(QUERIES)]) for i in range(conc * 5)]
            for f in as_completed(futs):
                lat.append(f.result()[1])
        res[f"concurrent_{conc}"] = summarize(lat, elapsed_s=time.perf_counter() - t0)
    print(TABLE_HEADER)
    print(fmt_row("TEI embed (1 client)", res["sequential"]))
    print(fmt_row("TEI embed (4 clients)", res["concurrent_4"]))
    print(fmt_row("TEI embed (8 clients)", res["concurrent_8"]))
    return res


def phase_baseline(a) -> dict:
    u = _check_user(a.user)
    res: dict = {"phase": "baseline", "user": u}
    with _client() as cl:
        lat, err = [], 0
        for q in QUERIES:
            ok, ms = _timed(lambda: cl.post("/recall", json=_recall_body(u, q)).status_code == 200)
            lat.append(ms); err += (not ok)
        res["recall_e2e"] = summarize(lat, err); res["rerank"] = RERANK; res["unique"] = UNIQUE
        for variant, sync in (("capture", None), ("capture_sync", True)):
            lat, err = [], 0
            tag = f"base{int(time.time() * 1000)}"   # unique per run: identical text would hit the idem_key DEDUPE path (~6 ms, no embed)
            for i in range(30):
                body = {"user_id": u, "kind": "turn", "session_id": f"{u}-{tag}", "cognify": False,
                        "user_input": f"{tag} turn {i}: what did we decide about {QUERIES[i % len(QUERIES)]}?",
                        "agent_response": "We decided to measure it first and keep the NAS as the permanent store."}
                if sync is not None:
                    body["sync"] = sync
                ok, ms = _timed(lambda: cl.post("/capture", json=body).status_code == 200)
                lat.append(ms); err += (not ok)
            res[variant] = summarize(lat, err)
        for variant, sync in (("facts_post", None), ("facts_post_sync", True)):
            lat, err, actions = [], 0, {}
            tag = f"base{int(time.time() * 1000)}"
            for i in range(30):   # novel set-facts → the real insert path (embed [sync] + supersede check + insert)
                body = {"user_id": u, "subject": f"bench-ent-{i:03d}", "predicate": "bench_attr_00",
                        "value": f"{tag} {QUERIES[i % len(QUERIES)]} #{i}"}
                if sync is not None:
                    body["sync"] = sync
                t = time.perf_counter()
                try:
                    r = cl.post("/facts", json=body); ok = r.status_code == 200
                    act = r.json().get("action") if ok else f"http{r.status_code}"
                except Exception as e:  # noqa: BLE001
                    ok, act = False, type(e).__name__
                lat.append((time.perf_counter() - t) * 1000); err += (not ok); actions[act] = actions.get(act, 0) + 1
            res[variant] = summarize(lat, err); res[variant + "_actions"] = actions
    # DB-only recall (probe inside the service container)
    qs, tei_lat = _embed_queries(QUERIES)
    res["tei_query_embed"] = summarize(tei_lat)
    pr = _probe_recall(u, qs)
    res["recall_db_only"] = _mode_summary(pr["modes"]["off"])
    res["gucs"] = pr["gucs_before"]
    print(TABLE_HEADER)
    for k in ("recall_e2e", "recall_db_only", "tei_query_embed", "capture", "capture_sync", "facts_post", "facts_post_sync"):
        print(fmt_row(k, res[k]))
    return res


def phase_real_seed(a) -> dict:
    u = _check_user(a.user)
    rows = real_facts(u, a.n)
    lat: list[float] = []
    err = 0
    actions: dict[str, int] = {}
    lock = threading.Lock()

    def one(f: dict) -> None:
        nonlocal err
        with _client() as cl:
            t = time.perf_counter()
            try:
                r = cl.post("/facts", json={"user_id": u, **f})
                ok = r.status_code == 200
                act = r.json().get("action") if ok else f"http{r.status_code}"
            except Exception as e:  # noqa: BLE001
                ok, act = False, type(e).__name__
            ms = (time.perf_counter() - t) * 1000
        with lock:
            lat.append(ms); err += (not ok); actions[act] = actions.get(act, 0) + 1

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, rows))
    res = {"phase": "real-seed", "user": u, "n": len(rows), "workers": a.workers,
           "post_facts": summarize(lat, err, time.perf_counter() - t0), "actions": actions}
    print(TABLE_HEADER); print(fmt_row(f"POST /facts real x{len(rows)} ({a.workers} workers)", res["post_facts"]))
    print("actions:", actions)
    return res


def _hit(res: dict, pred: str) -> bool:
    return any(it.get("predicate") == pred for it in res.get("items", []))


def phase_recall(a) -> dict:
    u = _check_user(a.user)
    qs_text = [q for q, _ in REAL_QUERIES] + QUERIES
    qs_text = (qs_text * ((a.n // len(qs_text)) + 1))[: a.n]
    res: dict = {"phase": "recall", "user": u, "n": a.n}
    # e2e
    lat, err, hits = [], 0, {}
    with _client() as cl:
        for q in qs_text:
            t = time.perf_counter()
            try:
                r = cl.post("/recall", json=_recall_body(u, q))
                ok = r.status_code == 200
                if ok:
                    for rq, pred in REAL_QUERIES:
                        if rq == q:
                            hits.setdefault(q, _hit(r.json(), pred))
            except Exception:  # noqa: BLE001
                ok = False
            lat.append((time.perf_counter() - t) * 1000); err += (not ok)
    res["e2e"] = summarize(lat, err); res["rerank"] = RERANK; res["unique"] = UNIQUE
    res["e2e_hit_rate"] = round(sum(hits.values()) / max(1, len(hits)), 2)
    res["e2e_misses"] = [q for q, h in hits.items() if not h]
    # DB-only (vectors pre-embedded), default GUCs vs iterative scan — probe runs inside the container
    uniq = list(dict.fromkeys(qs_text))
    qs, tei_lat = _embed_queries(uniq, a.random_vectors)
    res["tei_query_embed"] = summarize(tei_lat); res["random_vectors"] = a.random_vectors
    repeat = max(1, a.n // len(qs))
    pr = _probe_recall(u, qs, modes=("off", "relaxed_order"), repeat=repeat)
    res["gucs"] = pr["gucs_before"]
    for mode, key in (("off", "db_only"), ("relaxed_order", "db_only_iterative")):
        m = pr["modes"][mode]
        res[key] = _mode_summary(m)
        hits = {}
        for (q, _), preds in zip(qs * repeat, m["items_predicates"]):
            for rq, pred in REAL_QUERIES:
                if rq == q:
                    hits.setdefault(q, pred in preds)
        res[key + "_hit_rate"] = round(sum(hits.values()) / max(1, len(hits)), 2)
        res[key + "_misses"] = [q for q, h in hits.items() if not h]
        vc = m["vec_candidates_of_40"]
        res[key + "_vec_candidates_of_40"] = {"min": min(vc), "mean": round(sum(vc) / len(vc), 1), "max": max(vc), "lt40": sum(1 for x in vc if x < 40)}
    res["breakdown"] = _probe({"op": "breakdown", "user_id": u, "queries": qs})
    print(TABLE_HEADER)
    print(fmt_row("recall e2e", res["e2e"])); print(fmt_row("recall DB-only (iterative_scan=off)", res["db_only"]))
    print(fmt_row("recall DB-only (iterative_scan=relaxed_order)", res["db_only_iterative"]))
    print(fmt_row("TEI query embed", res["tei_query_embed"]))
    print(f"semantic hit-rate: e2e {res['e2e_hit_rate']}  db_only {res['db_only_hit_rate']}  iterative {res['db_only_iterative_hit_rate']}")
    print(f"vector candidates of 40 (user filter): off {res['db_only_vec_candidates_of_40']}  relaxed {res['db_only_iterative_vec_candidates_of_40']}")
    print("DB-only step breakdown (p50/p95 ms):", {k: (v["p50"], v["p95"]) for k, v in res["breakdown"].items() if isinstance(v, dict) and not k.startswith("_")})
    return res


def _run_clients(n_clients: int, seconds: float, make_req, label: str) -> dict:
    """n threads hammering make_req(client, i) for `seconds`; returns summarize()."""
    lat: list[float] = []
    errs = [0]
    lock = threading.Lock()
    stop = time.perf_counter() + seconds

    def worker(k: int):
        rnd = random.Random(k)
        with _client() as cl:
            i = 0
            while time.perf_counter() < stop:
                t = time.perf_counter()
                try:
                    ok = make_req(cl, rnd, i)
                except Exception:  # noqa: BLE001
                    ok = False
                ms = (time.perf_counter() - t) * 1000
                with lock:
                    lat.append(ms); errs[0] += (not ok)
                i += 1

    t0 = time.perf_counter()
    ths = [threading.Thread(target=worker, args=(k,)) for k in range(n_clients)]
    [t.start() for t in ths]; [t.join() for t in ths]
    s = summarize(lat, errs[0], time.perf_counter() - t0)
    s["clients"] = n_clients; s["label"] = label
    return s


def _recall_req(u: str):
    def f(cl, rnd, i):
        q = rnd.choice(QUERIES)
        return cl.post("/recall", json=_recall_body(u, q)).status_code == 200
    return f


def _capture_req(u: str, tag: str):
    def f(cl, rnd, i):
        body = {"user_id": u, "kind": "turn", "session_id": f"{u}-{tag}-{rnd.randint(0, 9)}", "cognify": False,
                "user_input": f"{tag} turn {i} {rnd.random():.6f}: how do I {rnd.choice(QUERIES)} on the nas?",
                "agent_response": f"Use the {rnd.choice(QUERIES)} path; measured {rnd.randint(1, 900)} ms."}
        return cl.post("/capture", json=body).status_code == 200
    return f


def _facts_req(u: str, tag: str):
    def f(cl, rnd, i):
        body = {"user_id": u, "subject": f"bench-ent-{rnd.randint(0, 199):03d}", "predicate": "bench_attr_00",
                "value": f"{tag} {rnd.choice(QUERIES)} #{rnd.randint(0, 10**9)}"}
        return cl.post("/facts", json=body).status_code == 200
    return f


def phase_concurrency(a) -> dict:
    u = _check_user(a.user)
    res: dict = {"phase": "concurrency", "user": u, "seconds": a.seconds, "runs": [], "rerank": RERANK, "unique": UNIQUE}
    print(TABLE_HEADER)
    for n in [int(x) for x in a.clients.split(",")]:
        st = StatsSampler().start()
        s = _run_clients(n, a.seconds, _recall_req(u), f"recall x{n}")
        s["stats"] = st.stop()
        res["runs"].append(s)
        print(fmt_row(f"recall {n} clients", s), " peak:", {k: (v["cpu_peak"], round(v["mem_peak_mib"])) for k, v in s["stats"].items()})
        time.sleep(3)
    res["oom"] = oom_events()
    return res


def phase_mixed(a) -> dict:
    u = _check_user(a.user)
    st = StatsSampler().start()
    res: dict = {"phase": "mixed", "user": u, "seconds": a.seconds, "rerank": RERANK, "unique": UNIQUE}
    outs: dict[str, dict] = {}
    lock = threading.Lock()

    def run(label, n, fn):
        s = _run_clients(n, a.seconds, fn, label)
        with lock:
            outs[label] = s

    ths = [threading.Thread(target=run, args=("recall x8", 8, _recall_req(u))),
           threading.Thread(target=run, args=("capture x4", 4, _capture_req(u, "mixed"))),
           threading.Thread(target=run, args=("facts x2", 2, _facts_req(u, "mixed")))]
    [t.start() for t in ths]; [t.join() for t in ths]
    res["runs"] = outs
    res["stats"] = st.stop()
    res["oom"] = oom_events()
    print(TABLE_HEADER)
    for k, s in outs.items():
        print(fmt_row(k, s))
    print("peak:", {k: (v["cpu_peak"], round(v["mem_peak_mib"])) for k, v in res["stats"].items()})
    return res


def phase_correct(a) -> dict:
    u = _check_user(a.user)
    subj, pred = "bench-race-subject", "favorite_race_color"
    n = 20
    lat, codes = [], {}
    lock = threading.Lock()

    def one(i):
        with _client() as cl:
            t = time.perf_counter()
            r = cl.post("/correct", json={"user_id": u, "subject": subj, "predicate": pred, "value": f"color-{i:02d}"})
            ms = (time.perf_counter() - t) * 1000
        with lock:
            lat.append(ms); codes[r.status_code] = codes.get(r.status_code, 0) + 1

    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(one, range(n)))
    with _client() as cl:
        rows = cl.get("/facts", params={"user_id": u, "subject": subj, "predicate": pred, "status": "active", "limit": 100}).json()
        hist = cl.get("/history", params={"user_id": u, "subject": subj, "predicate": pred}).json()
    active_api = len(rows) if isinstance(rows, list) else None
    try:
        active_db = _probe({"op": "count_active", "user_id": u, "subject": subj, "predicate": pred})["n"]
    except Exception as e:  # noqa: BLE001
        print("count_active probe failed:", e); active_db = None
    res = {"phase": "correct", "user": u, "concurrent": n, "latency": summarize(lat), "codes": codes,
           "active_after_api": active_api, "active_after_db": active_db,
           "history_len": len(hist) if isinstance(hist, list) else None,
           "pass": active_api == 1 and (active_db in (None, 1)) and codes.get(200) == n}
    print(TABLE_HEADER); print(fmt_row("POST /correct x20 concurrent (1 key)", res["latency"]))
    print(f"codes={codes} active_after api={active_api} db={active_db} history_len={res['history_len']} PASS={res['pass']}")
    return res


def phase_chain(a) -> dict:
    u = _check_user(a.user)
    subj, pred = u, "current_focus"
    res: dict = {"phase": "chain", "user": u}
    with _client() as cl:
        lat = []
        for _ in range(20):
            ok, ms = _timed(lambda: cl.get("/history", params={"user_id": u, "subject": subj, "predicate": pred}).status_code == 200)
            lat.append(ms)
        hist = cl.get("/history", params={"user_id": u, "subject": subj, "predicate": pred}).json()
        res["history"] = summarize(lat); res["history_len"] = len(hist)
        res["history_active"] = sum(1 for r in hist if r.get("status") == "active")
        # as_of at several points along the chain (valid axis) and with the belief axis
        ats = [(datetime.now(timezone.utc) - timedelta(days=d)).isoformat() for d in (1, 60, 120, 200, 300)]
        lat, vals = [], []
        for at in ats:
            t = time.perf_counter()
            r = cl.post("/as_of", json={"user_id": u, "at": at, "subject": subj, "predicate": pred})
            lat.append((time.perf_counter() - t) * 1000)
            j = r.json()
            vals.append(len(j) if isinstance(j, list) else j)
        res["as_of"] = summarize(lat); res["as_of_rows_per_query"] = vals
        lat = []
        for at in ats:
            ok, ms = _timed(lambda: cl.post("/as_of", json={"user_id": u, "at": at, "as_believed_at": at, "subject": subj, "predicate": pred}).status_code == 200)
            lat.append(ms)
        res["as_of_believed"] = summarize(lat)
        # unscoped as_of (whole user, limit 50) — the expensive shape
        lat = []
        for at in ats:
            ok, ms = _timed(lambda: cl.post("/as_of", json={"user_id": u, "at": at}).status_code == 200)
            lat.append(ms)
        res["as_of_unscoped"] = summarize(lat)
    print(TABLE_HEADER)
    for k in ("history", "as_of", "as_of_believed", "as_of_unscoped"):
        print(fmt_row(k, res[k]))
    print(f"history_len={res['history_len']} active={res['history_active']} as_of rows/query={res['as_of_rows_per_query']}")
    return res


def phase_explain(a) -> dict:
    u = _check_user(a.user)
    q = "what beer do I like"
    v, _ = tei_embed(q)
    words = [w for w in re.findall(r"\w+", q) if len(w) <= 64]
    tsq = " | ".join(dict.fromkeys(w.lower() for w in words))
    pr = _probe({"op": "explain", "user_id": u, "query": q, "vec": v, "tsq": tsq,
                 "chain_subject": a.chain_user or u, "chain_predicate": "current_focus"})
    out: dict = {"phase": "explain", "user": u, "query": q, "plans": pr["plans"], "gucs": pr["gucs"]}
    for name, txt in pr["plans"].items():
        print(f"\n### {name}\n```\n{txt}\n```")
    print("\nGUCs:", out["gucs"])
    return out


def phase_db_concurrency(a) -> dict:
    """DB-only recall (TEI excluded) with 1/4/8/16 parallel connections inside the container — the
    store's own concurrent capacity, to separate it from the TEI ceiling seen in `concurrency`."""
    u = _check_user(a.user)
    qs, _ = _embed_queries(QUERIES, a.random_vectors)
    clients = [int(x) for x in a.clients.split(",")]
    st = StatsSampler().start()
    pr = _probe({"op": "recall_concurrent", "user_id": u, "queries": qs, "clients": clients, "seconds": a.seconds,
                 "iterative_scan": "relaxed_order" if a.iterative else "off"})
    stats = st.stop()
    res = {"phase": "db-concurrency", "user": u, "seconds": a.seconds, "runs": [pr[str(n)] for n in clients], "stats": stats,
           "iterative_scan": "relaxed_order" if a.iterative else "off", "random_vectors": a.random_vectors}
    print(TABLE_HEADER)
    for run in res["runs"]:
        print(fmt_row(f"DB-only recall {run['clients']} clients", run))
    print("peak:", {k: (v["cpu_peak"], round(v["mem_peak_mib"])) for k, v in stats.items()})
    return res


def phase_filter(a) -> dict:
    """Filtered-HNSW check on a bulk (random-vector) user: candidates surviving the user filter of
    LIMIT 40 at ef_search=64, iterative_scan off vs relaxed_order, + per-query cost."""
    u = _check_user(a.user)
    pr = _probe({"op": "filter", "user_id": u, "n": a.n, "modes": ["off", "relaxed_order"]})
    res = {"phase": "filter", "user": u, "n": a.n, **pr}
    print(f"user rows {pr['user_rows']} of {pr['total_rows']} facts")
    print("| mode | fact cands of 40 (min/mean/max, <40) | fact_vec ms p50/p95 | episode cands of 20 (min/mean/max, <20) | episode_vec ms p50/p95 |")
    print("|---|---|---|---|---|")
    for mode in ("off", "relaxed_order"):
        m = pr[mode]; f = m["fact_candidates_of_40"]; e = m["episode_candidates_of_20"]
        print(f"| {mode} | {f['min']}/{f['mean']}/{f['max']}, {f['lt_k']} | {m['fact_vec_ms']['p50']}/{m['fact_vec_ms']['p95']} | "
              f"{e['min']}/{e['mean']}/{e['max']}, {e['lt_k']} | {m['episode_vec_ms']['p50']}/{m['episode_vec_ms']['p95']} |")
    return res


def phase_worker(a) -> dict:
    u = _check_user(a.user)
    res: dict = {"phase": "worker", "user": u, "turns": a.turns, "seconds": a.seconds, "rerank": RERANK, "unique": UNIQUE}
    # control: recall x4 with an idle worker
    st = StatsSampler().start()
    res["recall_idle_worker"] = _run_clients(4, a.seconds, _recall_req(u), "recall x4 idle")
    res["recall_idle_worker"]["stats"] = st.stop()
    # enqueue cognify=true turns (real LLM work for the worker)
    topics = ["favorite beer is now a hazy IPA", "I switched my editor to Helix", "the NAS got a second 10GbE link",
              "we pinned vLLM ROCm at 0.20.2", "decided to skip Gorgon Halo", "my timezone is America/Los_Angeles"]
    lat, err, queued = [], 0, 0
    with _client() as cl:
        for i in range(a.turns):
            body = {"user_id": u, "kind": "turn", "session_id": f"{u}-cog-{i // 4}", "cognify": True, "priority": "normal",
                    "user_input": f"turn {i}: by the way, {topics[i % len(topics)]} (note {random.random():.5f})",
                    "agent_response": "Got it, I'll remember that."}
            t = time.perf_counter()
            try:
                r = cl.post("/capture", json=body); ok = r.status_code == 200
                queued += int(bool(ok and r.json().get("queued")))
            except Exception:  # noqa: BLE001
                ok = False
            lat.append((time.perf_counter() - t) * 1000); err += (not ok)
        q0 = cl.get("/health").json().get("queue", {})
    res["enqueue"] = summarize(lat, err); res["queued"] = queued; res["queue_before"] = q0
    # recall x4 while the worker drains
    st = StatsSampler().start()
    res["recall_draining"] = _run_clients(4, a.seconds, _recall_req(u), "recall x4 draining")
    res["recall_draining"]["stats"] = st.stop()
    with _client() as cl:
        res["queue_after"] = cl.get("/health").json().get("queue", {})
    print(TABLE_HEADER)
    print(fmt_row("recall x4, worker idle", res["recall_idle_worker"]))
    print(fmt_row("capture cognify=true x%d" % a.turns, res["enqueue"]))
    print(fmt_row("recall x4, worker draining", res["recall_draining"]))
    print("queue before:", q0, " after:", res["queue_after"])
    print("peak idle:", {k: (v["cpu_peak"], round(v["mem_peak_mib"])) for k, v in res["recall_idle_worker"]["stats"].items()})
    print("peak drain:", {k: (v["cpu_peak"], round(v["mem_peak_mib"])) for k, v in res["recall_draining"]["stats"].items()})
    return res


def phase_wipe_http(a) -> dict:
    u = _check_user(a.user)
    with _client(timeout=300) as cl:
        t = time.perf_counter()
        r = cl.delete(f"/users/{u}")
        ms = (time.perf_counter() - t) * 1000
    res = {"phase": "wipe-http", "user": u, "status": r.status_code, "ms": round(ms, 1), "body": r.json() if r.status_code == 200 else r.text[:200]}
    print(res)
    return res


def _embed_at(base: str, model: str, text: str, timeout: float = 30.0) -> tuple[bool, float]:
    t = time.perf_counter()
    try:
        r = httpx.post(f"{base}/v1/embeddings", json={"input": ["search_query: " + text], "model": model}, timeout=timeout)
        ok = r.status_code == 200 and len(r.json()["data"][0]["embedding"]) == 768
    except Exception:  # noqa: BLE001
        ok = False
    return ok, (time.perf_counter() - t) * 1000


def _floor_sweep(fn, label: str, conc_levels=(4, 8), seq_n: int = 20, per_client: int = 5) -> dict:
    """Sequential seq_n calls, then conc-way bursts of conc*per_client calls; fn(i) -> (ok, ms)."""
    seq = [fn(i) for i in range(seq_n)]
    out = {"sequential": summarize([m for _, m in seq], sum(1 for ok, _ in seq if not ok))}
    print(fmt_row(f"{label}, 1 client", out["sequential"]))
    for conc in conc_levels:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=conc) as ex:
            res = list(ex.map(fn, range(conc * per_client)))
        out[f"concurrent_{conc}"] = summarize([m for _, m in res], sum(1 for ok, _ in res if not ok), time.perf_counter() - t0)
        print(fmt_row(f"{label}, {conc} clients", out[f"concurrent_{conc}"]))
    return out


def phase_embed_floor(a) -> dict:
    """Query-embed latency floor per embedding endpoint (the service tries them in this order)."""
    res: dict = {"phase": "embed-floor", "endpoints": {}}
    print(TABLE_HEADER)
    for name, base, model in _parse_endpoints(a.endpoints or EMBED_ENDPOINTS):
        nonce = int(time.time())
        res["endpoints"][name] = {"url": base, "model": model,
                                  **_floor_sweep(lambda i, b=base, m=model: _embed_at(b, m, f"{QUERIES[i % len(QUERIES)]} {nonce}-{i}"),
                                                 f"embed [{name}]")}
    return res


def _rerank_docs(n: int = 30) -> list[str]:
    """n hook-shaped texts (fact hooks ~60-120 chars + a few episode hooks ~240 chars, the service's cap)."""
    rng = random.Random(5)
    facts = real_facts("bench-real", 400)
    docs = [f"{f['subject']} {f['predicate'].replace('_', ' ')}: {f['value']}" for f in rng.sample(facts, n - 6)]
    for i in range(6):
        docs.append((f"User: remind me how we {QUERIES[i]} last time on the nas?\n\nAssistant: we decided to measure it first, "
                     f"keep the NAS as the permanent store, and re-run the bench after deploy; numbers were fine.")[:240])
    return docs


def phase_rerank_floor(a) -> dict:
    """Cross-encoder latency per reranker endpoint: one /rerank call = 1 query × 30 texts (recall's top_n
    facts + 6 episode hooks), uncached (fresh query text per call)."""
    res: dict = {"phase": "rerank-floor", "endpoints": {}, "texts_per_call": 30}
    docs = _rerank_docs(30)
    print(TABLE_HEADER)

    def one(base: str, i: int) -> tuple[bool, float]:
        q = f"{QUERIES[i % len(QUERIES)]} {int(time.time() * 1000)}-{i}"
        t = time.perf_counter()
        try:
            r = httpx.post(f"{base}/rerank", json={"query": q, "texts": docs, "truncate": True, "raw_scores": True}, timeout=30)
            ok = r.status_code == 200 and len(r.json()) == len(docs)
        except Exception:  # noqa: BLE001
            ok = False
        return ok, (time.perf_counter() - t) * 1000

    for name, base, _ in _parse_endpoints(a.endpoints or RERANK_ENDPOINTS):
        try:
            info = httpx.get(f"{base}/info", timeout=5).json()
            model = info.get("model_id"); mbt = info.get("max_batch_tokens")
        except Exception:  # noqa: BLE001
            model, mbt = None, None
        res["endpoints"][name] = {"url": base, "model": model, "max_batch_tokens": mbt,
                                  **_floor_sweep(lambda i, b=base: one(b, i), f"rerank 30 texts [{name}]")}
    return res


def phase_embed_gap(a) -> dict:
    """Async write path: write N turns (capture) + N facts with the default (async) path, then poll the
    store until every row has an embedding. time-to-embedded = how long a new memory is BM25-only."""
    u = _check_user(a.user)
    n = a.n
    tag = f"gap{int(time.time() * 1000)}"
    res: dict = {"phase": "embed-gap", "user": u, "n": n}
    ep_ids, f_ids = [], []
    lat_c, lat_f, err = [], [], 0
    with _client() as cl:
        t_write0 = time.perf_counter()
        for i in range(n):
            body = {"user_id": u, "kind": "turn", "session_id": f"{u}-{tag}", "cognify": False,
                    "user_input": f"{tag} turn {i}: is the embedding of {QUERIES[i % len(QUERIES)]} backfilled yet?",
                    "agent_response": "The worker fills it on its next tick."}
            t = time.perf_counter()
            try:
                r = cl.post("/capture", json=body); ok = r.status_code == 200
                if ok and r.json().get("episode_id"):
                    ep_ids.append(r.json()["episode_id"])
            except Exception:  # noqa: BLE001
                ok = False
            lat_c.append((time.perf_counter() - t) * 1000); err += (not ok)
        for i in range(n):
            body = {"user_id": u, "subject": f"bench-gap-{i:03d}", "predicate": "bench_attr_01", "value": f"{tag} {QUERIES[i % len(QUERIES)]} #{i}"}
            t = time.perf_counter()
            try:
                r = cl.post("/facts", json=body); ok = r.status_code == 200
                fid = (r.json().get("fact") or {}).get("id") if ok else None
                if fid:
                    f_ids.append(fid)
            except Exception:  # noqa: BLE001
                ok = False
            lat_f.append((time.perf_counter() - t) * 1000); err += (not ok)
        t_write1 = time.perf_counter()
    res["capture"] = summarize(lat_c); res["facts_post"] = summarize(lat_f); res["errors"] = err
    res["writes_s"] = round(t_write1 - t_write0, 2)
    # poll (through the container probe: 1 SQL every 2 s) until all embedded or timeout
    sql = ("SELECT (SELECT count(*) FROM episode WHERE id = ANY(%s::uuid[]) AND embedding IS NOT NULL) AS ep, "
           "(SELECT count(*) FROM fact WHERE id = ANY(%s::uuid[]) AND embedding IS NOT NULL) AS f")
    t0 = time.perf_counter(); done_ep = done_f = None; samples = []
    first_ep = first_f = all_ep = all_f = None
    deadline = t0 + a.seconds
    while time.perf_counter() < deadline:
        row = _probe({"op": "sql", "sql": sql, "args": [ep_ids, f_ids]})["rows"][0]
        el = round(time.perf_counter() - t_write1, 1)
        samples.append((el, row["ep"], row["f"]))
        if row["ep"] and first_ep is None:
            first_ep = el
        if row["f"] and first_f is None:
            first_f = el
        if row["ep"] >= len(ep_ids) and all_ep is None:
            all_ep = el
        if row["f"] >= len(f_ids) and all_f is None:
            all_f = el
        if all_ep is not None and all_f is not None:
            break
        time.sleep(2)
    res["embedded_at_write"] = {"episodes": samples[0][1] if samples else None, "facts": samples[0][2] if samples else None}
    res["first_embedded_s"] = {"episodes": first_ep, "facts": first_f}
    res["all_embedded_s"] = {"episodes": all_ep, "facts": all_f}
    res["samples"] = samples[:60]
    print(TABLE_HEADER); print(fmt_row(f"capture async × {n}", res["capture"])); print(fmt_row(f"POST /facts async × {n}", res["facts_post"]))
    print(f"embedded at write: {res['embedded_at_write']}  first embedded after: {res['first_embedded_s']} s  all embedded after: {res['all_embedded_s']} s")
    return res


def phase_correct_seq(a) -> dict:
    """N sequential POST /correct on ONE functional key (API-built chain; each supersede writes the
    belief-axis versioned copy + the new row), then /history + /as_of on that chain and the row count."""
    u = _check_user(a.user)
    subj, pred = "bench-chain-subject", "preferred_chain_color"
    n = a.n
    lat, codes, actions = [], {}, {}
    with _client() as cl:
        t0 = time.perf_counter()
        for i in range(n):
            t = time.perf_counter()
            r = cl.post("/correct", json={"user_id": u, "subject": subj, "predicate": pred, "value": f"chain-color-{i:03d}"})
            lat.append((time.perf_counter() - t) * 1000); codes[r.status_code] = codes.get(r.status_code, 0) + 1
            if r.status_code == 200:
                act = r.json().get("action"); actions[act] = actions.get(act, 0) + 1
        el = time.perf_counter() - t0
        res: dict = {"phase": "correct-seq", "user": u, "n": n, "correct": summarize(lat, n - codes.get(200, 0), el),
                     "codes": codes, "actions": actions}
        hl = []
        for _ in range(20):
            ok, ms = _timed(lambda: cl.get("/history", params={"user_id": u, "subject": subj, "predicate": pred}).status_code == 200)
            hl.append(ms)
        hist = cl.get("/history", params={"user_id": u, "subject": subj, "predicate": pred}).json()
        res["history"] = summarize(hl); res["history_len"] = len(hist) if isinstance(hist, list) else None
        res["history_active"] = sum(1 for r in hist if r.get("status") == "active") if isinstance(hist, list) else None
        # as_of along the (seconds-apart) chain: valid axis + belief axis
        now = datetime.now(timezone.utc)
        ats = [(now - timedelta(seconds=s)).isoformat() for s in (0, max(1, el * 0.25), max(1, el * 0.5), max(1, el * 0.75), el + 60)]
        l1, vals = [], []
        for at in ats:
            t = time.perf_counter()
            r = cl.post("/as_of", json={"user_id": u, "at": at, "subject": subj, "predicate": pred})
            l1.append((time.perf_counter() - t) * 1000); j = r.json(); vals.append(len(j) if isinstance(j, list) else j)
        res["as_of"] = summarize(l1); res["as_of_rows_per_query"] = vals
        l2 = []
        for at in ats:
            ok, ms = _timed(lambda: cl.post("/as_of", json={"user_id": u, "at": at, "as_believed_at": at, "subject": subj, "predicate": pred}).status_code == 200)
            l2.append(ms)
        res["as_of_believed"] = summarize(l2)
    try:
        rows = _probe({"op": "sql", "sql": "SELECT status, (meta ? 'version_of') AS versioned, (meta ? 'belief_closed_by') AS closed, count(*) AS n "
                       "FROM fact WHERE user_id=%s AND subject=%s AND predicate=%s GROUP BY 1,2,3 ORDER BY 1,2,3",
                       "args": [u, subj, pred]})["rows"]
        res["db_rows_by_status"] = rows; res["db_rows_total"] = sum(r["n"] for r in rows)
    except Exception as e:  # noqa: BLE001
        res["db_rows_by_status"] = str(e)
    print(TABLE_HEADER); print(fmt_row(f"POST /correct × {n} sequential (1 key)", res["correct"]))
    print(fmt_row("GET /history", res["history"])); print(fmt_row("POST /as_of scoped", res["as_of"])); print(fmt_row("POST /as_of scoped + as_believed_at", res["as_of_believed"]))
    print(f"codes={codes} actions={actions} history_len={res['history_len']} (active {res['history_active']}) "
          f"db rows total={res.get('db_rows_total')} by status={res['db_rows_by_status']} as_of rows/query={vals}")
    return res


PHASES = {"tei": phase_tei, "embed-floor": phase_embed_floor, "rerank-floor": phase_rerank_floor, "embed-gap": phase_embed_gap,
          "correct-seq": phase_correct_seq, "baseline": phase_baseline, "real-seed": phase_real_seed, "recall": phase_recall,
          "concurrency": phase_concurrency, "mixed": phase_mixed, "correct": phase_correct, "chain": phase_chain,
          "explain": phase_explain, "filter": phase_filter, "db-concurrency": phase_db_concurrency, "worker": phase_worker, "wipe-http": phase_wipe_http}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=PHASES)
    ap.add_argument("user", nargs="?", default=f"{BENCH_PREFIX}real")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--clients", default="1,4,8,16")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--turns", type=int, default=100)
    ap.add_argument("--random-vectors", action="store_true", help="recall/db-concurrency: random unit query vectors (bulk bench users)")
    ap.add_argument("--iterative", action="store_true", help="db-concurrency: SET hnsw.iterative_scan=relaxed_order on each connection")
    ap.add_argument("--chain-user", default=None, help="explain: user holding the 50-long chain (default: USER)")
    ap.add_argument("--out", default=os.environ.get("BENCH_OUT"))
    ap.add_argument("--label", default=os.environ.get("BENCH_LABEL", ""), help="free tag stored in the record (e.g. 'pre-load', '150k')")
    ap.add_argument("--rerank", choices=["default", "on", "off"], default=os.environ.get("BENCH_RERANK", "default"),
                    help="add rerank:true|false to every /recall body (default: the service setting)")
    ap.add_argument("--unique", action="store_true", help="nonce per recall query: defeats the service's query-embed / rerank caches")
    ap.add_argument("--endpoints", default=None, help="embed-floor / rerank-floor: 'name=url|model,...' (default: workstation + NAS)")
    a = ap.parse_args(argv)
    global RERANK, UNIQUE
    RERANK = None if a.rerank == "default" else (a.rerank == "on")
    UNIQUE = bool(a.unique)
    rec = PHASES[a.phase](a)
    rec["label"] = a.label
    _emit(a.out, rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
