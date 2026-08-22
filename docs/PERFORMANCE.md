# Astoria — performance and scale validation

> **Numbers from the 2026-08 validation run.** Every table in §2 was produced by the harness in
> `scripts/bench/` (`report.py` over `scripts/bench/results/2026-08-22.jsonl`) against a live deployment on a
> small NAS-class host; re-run the harness (README there) to refresh them for your hardware. §6 records the
> reranker evaluation. **§7 is the second run on the feature-complete build** (asynchronous write path, rerank
> stage on/off under concurrency, workstation embedding seat, graph expansion, belief-axis versioning) with
> before/after tables against §2 and an updated verdict — read it first; §0–§5 are the run-1 baseline.

## 0. Verdict (short)

**The store does not collapse under its own weight — a CPU embedder does.** At 150 k embedded rows (100 k
facts + 50 k episodes, 20 users) the Postgres/pgvector side of a recall costs **49 ms p50 / 68 ms p95**
(DB-only, real-embedding user), BM25 and HNSW plans stay index-backed, the store alone sustains **~44
recalls/s with 8 parallel connections (p95 232 ms)** and 16 connections (p95 478 ms) with zero errors, 20
concurrent `/correct` on one key leave exactly one active row, and nothing was OOM-killed. Single-client
end-to-end `/recall` is **205 ms p50 / 225–313 ms p95** — of which ~170 ms is the query embedding on a CPU
TEI — and did not move between 500 rows and 150 k rows.

What fails the contract is the **embedder-inclusive concurrent numbers**: a CPU nomic container tops out
at **~6.5 embeds/s**, so 8 concurrent recall clients see **p95 1 406 ms** (Postgres at 12–23 % CPU, the
embedder at 400 %), and the mixed load (14 clients) puts every call at ~3 s. That ceiling is fixed and
independent of store size; it is not fixable inside Postgres — it is fixed by a faster embedding endpoint
(now first in `ASTORIA_EMBED_URLS`), the asynchronous write path, and the query-embedding cache.

| contract threshold | measured @150 k | result |
|---|---|---|
| recall p95 < 500 ms e2e, **8 concurrent clients** (embedder-inclusive) | **1 406 ms** (1 client: 313 ms; DB-only 8 conns: 232 ms) | **FAIL — embedder-bound** |
| DB-only recall p95 < 80 ms | **67.7 ms** (55.8 ms with the iterative-scan fix) | PASS |
| capture p95 < 400 ms | **270 ms** single client (3 236 ms under the 14-client mixed load — embedder-bound; the asynchronous write path removes the embed from the request) | PASS (single) / FAIL (mixed, pre-async) |
| zero errors | 0 errors over ~6 000 HTTP requests + ~8 000 DB-only recalls | PASS |
| no OOM | `OOMKilled=false`, 0 restarts on both containers (Postgres peaked at 1 009 MiB of 1 GiB in the worst-case DB-only hammer) | PASS |
| exactly 1 active under 20 concurrent `/correct` | 1 (API and DB), history 20, 20×200 | PASS |

**One real defect found and fixed in code:** with pgvector's default `hnsw.iterative_scan=off`, a user
holding a small share of the shared HNSW index gets almost no vector candidates back (**mean 1.9 of 40**
for a 4 %-share user; semantic recall silently degrades to BM25). `recall.py` now sets `hnsw.iterative_scan
= relaxed_order` alongside `ef_search` (→ 40/40; free for the dominant user; 204/269 ms DB-only for the
4 %-share user). The e2e numbers below are pre-fix; the DB-only numbers were measured with the setting
applied at session level, which is exactly what the patched code does per transaction.

**Scale knee:** not 1 M rows — ~200 k on this host. HNSW + TOAST cost ~7.9 KB per embedded row and the
Postgres cgroup's usable cache was ~650 MB, so past ~170 k rows vector probes start hitting disk (a cold
probe measured 1.39 s with 12 k page reads on spinning storage). Raise the Postgres limits and/or halve
the vector footprint before then (§4).

## 1. Environment (as measured)

| component | detail |
|---|---|
| service | `astoria` container, 768 MiB limit, 1 uvicorn worker (sync FastAPI routes → anyio threadpool), psycopg pool max 8, in-process cognify worker |
| store | `astoria-postgres` — Postgres 18.6 + pgvector 0.8.6, **1 GiB cgroup limit**, `shared_buffers=256MB`, `work_mem=4MB`, `maintenance_work_mem=128MB`, `effective_cache_size=4GB` (over-stated vs the 1 GiB cgroup), no CPU cap |
| HNSW | `fact_vec` / `episode_vec` = `hnsw(embedding vector_cosine_ops)`, defaults **m=16, ef_construction=64**; recall sets `SET LOCAL hnsw.ef_search=64`; server default `hnsw.iterative_scan=off`, `hnsw.max_scan_tuples=20000` |
| embeddings | TEI nomic-embed-text-v1.5 on the host CPU (`tei-embed`, 3 GiB limit, float32, max_batch_requests 4, max_client_batch_size 8) |
| host | NAS-class x86_64, 8 cores, 7.2 GiB RAM (~3.5 GiB available, swap in use before the run), other services resident |
| client | a workstation over a LAN; `POST` via httpx; DB-only probes executed *inside* the `astoria` container (an ssh tunnel adds 40–100 ms stalls to vector-parameter queries, so it is never on the DB-only timing path) |
| data | 20 bench users × 5 000 facts + 2 500 episodes via direct COPY (random unit vectors, 60 predicates mixed functional/set, ~200 entity subjects, 3-year `asserted_at` spread, ~8 % superseded chains + 2 % retracted, 10 % beliefs; episodes 70 % turns in sessions of 8 / 20 % summaries / 10 % notes) **+ one real-embedding user `bench-real` with 500 realistic triples written through `POST /facts`** (true embeddings) for semantic-quality probes; the live user untouched |

Method: every number is wall-clock from the client unless marked DB-only; DB-only = the service's own
`recall()` code run in the container with the query vector pre-embedded (so the embedder is excluded).
p50/p95/p99 over the stated n. Concurrency phases run N threads each with its own HTTP client for 60 s.

## 2. Results — every table (generated by `scripts/bench/report.py` from `scripts/bench/results/2026-08-22.jsonl`)

Labels: `pre-load` = 500 real-embedding facts for `bench-real`, ~0 other rows; `150k` = after the deep load (100 010 bench facts + 50 000
bench episodes + the 500 real ones); `150k-bulk-user` = a random-vector user with 4 339 active facts (4 % of the index).
Note the e2e numbers were taken against the deployed container (pre-fix code, `hnsw.iterative_scan=off`); the DB-only rows labelled
`relaxed_order` / `(fix)` apply the new setting at session level — identical in effect to the patched `recall.py`.

#### TEI query-embed floor (fixed per-request cost, independent of store size)

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| TEI embed, 1 client (sequential) | 20 | 163.0 | 191.6 | 193.9 | 194.5 |  | 0 |
| TEI embed, 4 concurrent clients | 20 | 566.2 | 720.5 | 720.6 | 720.6 | 6.63 | 0 |
| TEI embed, 8 concurrent clients | 40 | 1154.4 | 1272.6 | 1507.8 | 1507.9 | 6.58 | 0 |

#### /recall, /capture, POST /facts — before vs after the deep load (single client)

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| [pre-load] /recall e2e (30 varied queries) | 30 | 199.5 | 234.7 | 246.4 | 251.0 |  | 0 |
| [pre-load] /recall DB-only (same 30, pre-embedded, in-container) | 30 | 54.5 | 63.3 | 72.6 | 75.9 |  | 0 |
| [pre-load] TEI query embed (same 30) | 30 | 168.9 | 191.5 | 197.7 | 199.8 |  | 0 |
| [pre-load] /capture cognify=false (turn) | 30 | 221.2 | 256.9 | 260.7 | 261.3 |  | 0 |
| [pre-load] POST /facts novel set-fact (TEI embed + insert) {'inserted': 30} | 30 | 265.4 | 308.0 | 323.2 | 328.5 |  | 0 |
| [150k] /recall e2e (30 varied queries) | 30 | 204.9 | 224.6 | 231.2 | 233.2 |  | 0 |
| [150k] /recall DB-only (same 30, pre-embedded, in-container) | 30 | 63.4 | 81.1 | 87.9 | 89.7 |  | 0 |
| [150k] TEI query embed (same 30) | 30 | 182.5 | 292.2 | 627.1 | 763.5 |  | 0 |
| [150k] /capture cognify=false (turn) | 30 | 234.0 | 269.7 | 273.9 | 275.2 |  | 0 |
| [150k] POST /facts novel set-fact (TEI embed + insert) {'inserted': 30} | 30 | 262.9 | 288.9 | 301.7 | 305.9 |  | 0 |
| [pre-load] /recall e2e (real-embedding user, 50 queries) | 50 | 203.7 | 232.5 | 239.3 | 239.4 |  | 0 |
| [pre-load] /recall DB-only iterative_scan=off | 48 | 52.9 | 61.0 | 73.1 | 82.3 |  | 0 |
| [pre-load] /recall DB-only iterative_scan=relaxed_order | 48 | 49.9 | 60.3 | 66.2 | 71.2 |  | 0 |
| [pre-load] TEI query embed | 48 | 171.4 | 189.8 | 192.8 | 193.4 |  | 0 |
| [150k] /recall e2e (real-embedding user, 50 queries) | 50 | 209.6 | 312.8 | 454.8 | 544.1 |  | 0 |
| [150k] /recall DB-only iterative_scan=off | 48 | 49.2 | 67.7 | 91.7 | 104.3 |  | 0 |
| [150k] /recall DB-only iterative_scan=relaxed_order | 48 | 49.5 | 55.8 | 57.3 | 57.7 |  | 0 |
| [150k] TEI query embed | 48 | 170.5 | 189.6 | 199.0 | 201.7 |  | 0 |
| [150k-bulk-user] /recall e2e (real-embedding user, 20 queries) | 20 | 271.6 | 372.2 | 376.7 | 377.8 |  | 0 |
| [150k-bulk-user] /recall DB-only iterative_scan=off | 20 | 91.0 | 144.1 | 191.4 | 203.3 |  | 0 |
| [150k-bulk-user] /recall DB-only iterative_scan=relaxed_order | 20 | 203.8 | 269.1 | 270.6 | 271.0 |  | 0 |
| [150k-bulk-user] TEI query embed | 20 | 0.0 | 0.0 | 0.0 | 0.0 |  | 0 |

[pre-load] semantic hit-rate (expected predicate in items, 20 probes): e2e **1.0**, DB-only 1.0, iterative 1.0
[pre-load] HNSW candidates returned of LIMIT 40 after the user/status/layer filter: off min 40 mean 40.0 (queries <40: 0); relaxed_order min 40 mean 40.0 (<40: 0)

[150k] semantic hit-rate (expected predicate in items, 20 probes): e2e **1.0**, DB-only 1.0, iterative 1.0
[150k] HNSW candidates returned of LIMIT 40 after the user/status/layer filter: off min 40 mean 40.0 (queries <40: 0); relaxed_order min 40 mean 40.0 (<40: 0)

[150k-bulk-user] semantic hit-rate (expected predicate in items, 20 probes): e2e **0.75**, DB-only 0.75, iterative 0.75; misses: ['programming language preference', 'what GPUs do I own', 'decisions about the platform', 'where do I live', 'what is my current focus']
[150k-bulk-user] HNSW candidates returned of LIMIT 40 after the user/status/layer filter: off min 0 mean 2.5 (queries <40: 20); relaxed_order min 40 mean 40.0 (<40: 0)

DB-only per-step cost (in-container, ms p50 / p95):

| step | pre-load | 150k | 150k-bulk-user |
|---|---|---|---|
| fact_vec(hnsw) | 32.88 / 35.62 | 24.99 / 30.42 | 25.36 / 38.11 |
| fact_bm25(gin) | 2.61 / 5.87 | 4.72 / 9.62 | 5.46 / 13.97 |
| episode_vec(hnsw) | 12.19 / 14.14 | 11.61 / 13.23 | 25.36 / 36.62 |
| episode_bm25(gin) | 1.03 / 2.34 | 1.05 / 2.37 | 1.18 / 8.53 |
| score+collapse(py) | 0.76 / 1.97 | 0.72 / 1.69 | 0.61 / 0.83 |
| stale_hints(sql) | 0.99 / 3.2 | 1.33 / 4.11 | 1.54 / 3.21 |
| snapshot+touch(sql) | 1.96 / 3.3 | 2.07 / 5.55 | 1.89 / 37.14 |

#### Deep load (direct COPY through the tunnel, in-place HNSW inserts)

| users | facts | facts/s | episodes | episodes/s | VACUUM ANALYZE s |
|---|---|---|---|---|---|
| 20 | 100010 | 107 | 50000 | 121 | 2.2 |

Sizes after load (db total 1320 MB; bench rows {'fact': 100557, 'episode': 50030}):

| table | rows | total | heap | toast |
|---|---|---|---|---|
| fact | 100571 | 868 MB | 45 MB | 397 MB |
| episode | 50032 | 443 MB | 33 MB | 198 MB |
| snapshot | 770 | 496 kB | 336 kB | 8192 bytes |
| audit | 630 | 344 kB | 216 kB | 8192 bytes |
| cognify_queue | 1 | 64 kB | 8192 bytes | 8192 bytes |

| index | size |
|---|---|
| fact_vec | 393 MB |
| episode_vec | 195 MB |
| fact_one_active_set_value | 7296 kB |
| fact_valid | 6424 kB |
| fact_tsv | 6152 kB |
| episode_idem_key_key | 6024 kB |
| fact_key | 5744 kB |
| fact_pkey | 4160 kB |
| episode_session | 3624 kB |
| episode_tsv | 3104 kB |
| episode_user_time | 2592 kB |
| episode_pkey | 2208 kB |
| fact_one_active_functional | 1608 kB |
| fact_user_status | 1056 kB |
| fact_origin | 656 kB |

#### Concurrency sweep — /recall only, 60 s each [150k]

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| recall × 1 clients | 279 | 214.0 | 250.6 | 285.6 | 306.8 | 4.64 | 0 |
| recall × 4 clients | 377 | 638.5 | 700.9 | 807.4 | 884.5 | 6.23 | 0 |
| recall × 8 clients | 375 | 1290.6 | 1406.0 | 1542.2 | 2178.5 | 6.13 | 0 |
| recall × 16 clients | 386 | 2557.1 | 2712.6 | 2951.2 | 3355.0 | 6.21 | 0 |

- 1 clients peak: astoria: cpu 25% mem 115 MiB/768MiB; astoria-postgres: cpu 12% mem 697 MiB/1GiB; tei-embed: cpu 387% mem 480 MiB/3GiB
- 4 clients peak: astoria: cpu 56% mem 116 MiB/768MiB; astoria-postgres: cpu 23% mem 698 MiB/1GiB; tei-embed: cpu 414% mem 482 MiB/3GiB
- 8 clients peak: astoria: cpu 49% mem 123 MiB/768MiB; astoria-postgres: cpu 118% mem 705 MiB/1GiB; tei-embed: cpu 412% mem 487 MiB/3GiB
- 16 clients peak: astoria: cpu 79% mem 142 MiB/768MiB; astoria-postgres: cpu 21% mem 690 MiB/1GiB; tei-embed: cpu 412% mem 486 MiB/3GiB
- OOM check: `/astoria OOMKilled=false restarts=0
/astoria-postgres OOMKilled=false restarts=0`

#### DB-only concurrency — the store alone (in-container, pre-embedded queries, TEI excluded), 20 s each [150k real-user scan=off] — user `bench-real`, iterative_scan=off, random_vectors=False

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| DB-only recall × 1 connections | 397 | 54.3 | 72.2 | 78.3 | 103.6 | 19.79 | 0 |
| DB-only recall × 4 connections | 776 | 103.5 | 130.4 | 146.1 | 169.4 | 38.62 | 0 |
| DB-only recall × 8 connections | 895 | 178.0 | 232.2 | 253.7 | 290.2 | 44.57 | 0 |
| DB-only recall × 16 connections | 873 | 363.5 | 478.3 | 527.1 | 586.4 | 43.24 | 0 |

- peak: astoria: cpu 144% mem 220 MiB/768MiB; astoria-postgres: cpu 171% mem 876 MiB/1GiB; tei-embed: cpu 1% mem 480 MiB/3GiB

#### DB-only concurrency — the store alone (in-container, pre-embedded queries, TEI excluded), 20 s each [150k real-user scan=relaxed_order (fix)] — user `bench-real`, iterative_scan=relaxed_order, random_vectors=False

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| DB-only recall × 1 connections | 377 | 57.6 | 73.0 | 81.2 | 179.8 | 18.82 | 0 |
| DB-only recall × 4 connections | 757 | 105.0 | 134.4 | 144.9 | 175.5 | 37.81 | 0 |
| DB-only recall × 8 connections | 866 | 183.9 | 238.7 | 274.2 | 306.7 | 43.1 | 0 |
| DB-only recall × 16 connections | 867 | 363.6 | 491.6 | 544.4 | 583.2 | 43.0 | 0 |

- peak: astoria: cpu 115% mem 221 MiB/768MiB; astoria-postgres: cpu 162% mem 804 MiB/1GiB; tei-embed: cpu 1% mem 480 MiB/3GiB

#### DB-only concurrency — the store alone (in-container, pre-embedded queries, TEI excluded), 20 s each [150k bulk-user 4% share scan=relaxed_order (fix, worst case)] — user `bench-u05`, iterative_scan=relaxed_order, random_vectors=True

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| DB-only recall × 1 connections | 90 | 201.0 | 326.5 | 515.1 | 1028.8 | 4.5 | 0 |
| DB-only recall × 4 connections | 261 | 304.2 | 366.6 | 432.2 | 456.0 | 12.84 | 0 |
| DB-only recall × 8 connections | 330 | 482.8 | 584.4 | 637.7 | 723.4 | 16.25 | 0 |
| DB-only recall × 16 connections | 307 | 1058.0 | 1373.6 | 1470.9 | 1559.3 | 14.76 | 0 |

- peak: astoria: cpu 78% mem 221 MiB/768MiB; astoria-postgres: cpu 638% mem 1009 MiB/1GiB; tei-embed: cpu 8% mem 480 MiB/3GiB

#### Mixed load — 8 recall + 4 capture + 2 POST /facts clients, 60 s [150k]

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| facts x2 | 44 | 2864.1 | 3217.1 | 3453.2 | 3523.9 | 0.71 | 0 |
| capture x4 | 92 | 2748.1 | 3236.3 | 3350.1 | 3364.5 | 1.47 | 0 |
| recall x8 | 163 | 3096.8 | 3564.0 | 3678.1 | 3711.6 | 2.59 | 0 |

- peak: astoria: cpu 37% mem 146 MiB/768MiB; astoria-postgres: cpu 19% mem 695 MiB/1GiB; tei-embed: cpu 411% mem 492 MiB/3GiB
- OOM check: `/astoria OOMKilled=false restarts=0
/astoria-postgres OOMKilled=false restarts=0`

#### 20 concurrent POST /correct on ONE functional key [150k]

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| POST /correct × 20 (parallel) | 20 | 2153.3 | 4130.0 | 4284.3 | 4322.9 |  | 0 |

- HTTP codes {'200': 20}; active rows after: API 1, DB 1; history length 20; **PASS**

#### /history and /as_of on a 50-long supersede chain [150k]

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| GET /history (chain len 50, active 1) | 20 | 22.2 | 37.8 | 40.8 | 41.6 |  | 0 |
| POST /as_of scoped (rows/query [1, 1, 1, 1, 1]) | 5 | 5.9 | 6.5 | 6.5 | 6.5 |  | 0 |
| POST /as_of scoped + as_believed_at | 5 | 6.9 | 7.1 | 7.1 | 7.1 |  | 0 |
| POST /as_of unscoped (whole user, limit 50) | 5 | 23.8 | 58.8 | 62.2 | 63.1 |  | 0 |

#### Worker interference — recall × 4 while cognify drains 100 turns [150k]

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| recall × 4, worker idle (control) | 358 | 649.9 | 841.2 | 944.8 | 1041.3 | 5.91 | 0 |
| capture cognify=true × 100 (enqueue) | 100 | 251.7 | 377.2 | 515.1 | 1195.0 |  | 0 |
| recall × 4, worker draining | 363 | 635.4 | 753.4 | 1591.0 | 2623.6 | 6.0 | 0 |

- queue before {'pending': 96, 'dead': 0, 'by_state': {'pending': 96, 'done': 5}} → after {'pending': 92, 'dead': 0, 'by_state': {'pending': 92, 'done': 9}} (queued 100)
- peak idle: astoria: cpu 60% mem 140 MiB/768MiB; astoria-postgres: cpu 30% mem 678 MiB/1GiB; tei-embed: cpu 415% mem 482 MiB/3GiB
- peak draining: astoria: cpu 87% mem 141 MiB/768MiB; astoria-postgres: cpu 43% mem 684 MiB/1GiB; tei-embed: cpu 415% mem 484 MiB/3GiB

#### EXPLAIN (ANALYZE, BUFFERS) summaries [150k] — GUCs {'hnsw.ef_search': '40', 'hnsw.iterative_scan': 'off', 'hnsw.max_scan_tuples': '20000', 'hnsw.scan_mem_multiplier': '1', 'shared_buffers': '256MB', 'work_mem': '4MB', 'maintenance_work_mem': '128MB', 'effective_cache_size': '4GB', 'max_connections': '100'}

| query | scan | exec ms | buffers (shared hit/read) |
|---|---|---|---|
| fact vector (ef_search=64, iterative_scan=off) | other | 17.520 ms | shared hit=4918 |
| fact vector (ef_search=64, iterative_scan=relaxed_order) | other | 15.034 ms | shared hit=4884 |
| fact vector (ef_search=40 default) | other | 13.195 ms | shared hit=4884 |
| fact BM25 (GIN) | GIN | 2.142 ms | shared hit=53 |
| episode vector (ef_search=64) | other | 1.049 ms | shared hit=501 |
| episode BM25 (GIN) | GIN | 0.164 ms | shared hit=18 |
| history (fact_key) | fact_key | 0.038 ms | shared hit=3 |
| as_of unscoped (limit 50) | fact_key | 0.309 ms | shared hit=110 |
| stale_hints (1 key) | other | 0.135 ms | shared hit=115 |

<details><summary>full plans</summary>

**fact vector (ef_search=64, iterative_scan=off)**
```
Limit  (cost=988.54..988.64 rows=40 width=32) (actual time=17.390..17.408 rows=40.00 loops=1)
  Buffers: shared hit=4918
  ->  Sort  (cost=988.54..989.74 rows=478 width=32) (actual time=17.388..17.396 rows=40.00 loops=1)
        Sort Key: ((embedding <=> '[<768-d query vector>]'::vector))
        Sort Method: top-N heapsort  Memory: 30kB
        Buffers: shared hit=4918
        ->  Index Scan using fact_user_status on fact  (cost=0.29..973.43 rows=478 width=32) (actual time=0.377..16.916 rows=591.00 loops=1)
              Index Cond: ((user_id = 'bench-real'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])))
              Filter: ((embedding IS NOT NULL) AND ((valid_to IS NULL) OR (valid_to > now())))
              Index Searches: 1
              Buffers: shared hit=4915
Planning:
  Buffers: shared hit=323
Planning Time: 2.964 ms
Execution Time: 17.520 ms
```

**fact vector (ef_search=64, iterative_scan=relaxed_order)**
```
Limit  (cost=988.54..988.64 rows=40 width=32) (actual time=14.963..14.980 rows=40.00 loops=1)
  Buffers: shared hit=4884
  ->  Sort  (cost=988.54..989.74 rows=478 width=32) (actual time=14.961..14.969 rows=40.00 loops=1)
        Sort Key: ((embedding <=> '[<768-d query vector>]'::vector))
        Sort Method: top-N heapsort  Memory: 30kB
        Buffers: shared hit=4884
        ->  Index Scan using fact_user_status on fact  (cost=0.29..973.43 rows=478 width=32) (actual time=0.122..14.540 rows=591.00 loops=1)
              Index Cond: ((user_id = 'bench-real'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])))
              Filter: ((embedding IS NOT NULL) AND ((valid_to IS NULL) OR (valid_to > now())))
              Index Searches: 1
              Buffers: shared hit=4884
Planning:
  Buffers: shared hit=1
Planning Time: 0.412 ms
Execution Time: 15.034 ms
```

**fact vector (ef_search=40 default)**
```
Limit  (cost=988.54..988.64 rows=40 width=32) (actual time=13.128..13.143 rows=40.00 loops=1)
  Buffers: shared hit=4884
  ->  Sort  (cost=988.54..989.74 rows=478 width=32) (actual time=13.126..13.133 rows=40.00 loops=1)
        Sort Key: ((embedding <=> '[<768-d query vector>]'::vector))
        Sort Method: top-N heapsort  Memory: 30kB
        Buffers: shared hit=4884
        ->  Index Scan using fact_user_status on fact  (cost=0.29..973.43 rows=478 width=32) (actual time=0.112..12.740 rows=591.00 loops=1)
              Index Cond: ((user_id = 'bench-real'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])))
              Filter: ((embedding IS NOT NULL) AND ((valid_to IS NULL) OR (valid_to > now())))
              Index Searches: 1
              Buffers: shared hit=4884
Planning:
  Buffers: shared hit=1
Planning Time: 0.418 ms
Execution Time: 13.195 ms
```

**fact BM25 (GIN)**
```
Limit  (cost=136.79..136.83 rows=16 width=28) (actual time=2.085..2.092 rows=40.00 loops=1)
  Buffers: shared hit=53
  ->  Sort  (cost=136.79..136.83 rows=16 width=28) (actual time=2.084..2.087 rows=40.00 loops=1)
        Sort Key: (ts_rank_cd(tsv, '''beer'' | ''like'''::tsquery)) DESC, asserted_at DESC
        Sort Method: quicksort  Memory: 28kB
        Buffers: shared hit=53
        ->  Bitmap Heap Scan on fact  (cost=66.97..136.47 rows=16 width=28) (actual time=1.665..2.031 rows=48.00 loops=1)
              Recheck Cond: ((user_id = 'bench-real'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])) AND (tsv @@ '''beer'' | ''like'''::tsquery))
              Filter: ((valid_to IS NULL) OR (valid_to > now()))
              Heap Blocks: exact=38
              Buffers: shared hit=47
              ->  BitmapAnd  (cost=66.97..66.97 rows=18 width=0) (actual time=1.629..1.630 rows=0.00 loops=1)
                    Buffers: shared hit=9
                    ->  Bitmap Index Scan on fact_user_status  (cost=0.00..19.59 rows=536 width=0) (actual time=0.061..0.061 rows=797.00 loops=1)
                          Index Cond: ((user_id = 'bench-real'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])))
                          Index Searches: 1
                          Buffers: shared hit=2
                    ->  Bitmap Index Scan on fact_tsv  (cost=0.00..47.12 rows=3442 width=0) (actual time=1.541..1.541 rows=3522.00 loops=1)
                          Index Cond: (tsv @@ '''beer'' | ''like'''::tsquery)
                          Index Searches: 1
                          Buffers: shared hit=7
Planning:
  Buffers: shared hit=76
Planning Time: 1.776 ms
Execution Time: 2.142 ms
```

**episode vector (ef_search=64)**
```
Limit  (cost=67.30..67.35 rows=20 width=24) (actual time=1.026..1.030 rows=20.00 loops=1)
  Buffers: shared hit=501
  ->  Sort  (cost=67.30..67.36 rows=26 width=24) (actual time=1.025..1.027 rows=20.00 loops=1)
        Sort Key: ((embedding <=> '[<768-d query vector>]'::vector))
        Sort Method: top-N heapsort  Memory: 27kB
        Buffers: shared hit=501
        ->  Index Scan using episode_session on episode  (cost=0.41..66.69 rows=26 width=24) (actual time=0.099..0.991 rows=122.00 loops=1)
              Index Cond: (user_id = 'bench-real'::text)
              Filter: ((embedding IS NOT NULL) AND (status = 'active'::text) AND (kind = ANY ('{summary,note,import,turn}'::text[])))
              Index Searches: 1
              Buffers: shared hit=501
Planning:
  Buffers: shared hit=99
Planning Time: 0.355 ms
Execution Time: 1.049 ms
```

**episode BM25 (GIN)**
```
Limit  (cost=58.28..58.29 rows=1 width=28) (actual time=0.143..0.145 rows=6.00 loops=1)
  Buffers: shared hit=18
  ->  Sort  (cost=58.28..58.29 rows=1 width=28) (actual time=0.142..0.144 rows=6.00 loops=1)
        Sort Key: (ts_rank_cd(tsv, '''beer'' | ''like'''::tsquery)) DESC, occurred_at DESC
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=18
        ->  Bitmap Heap Scan on episode  (cost=54.25..58.27 rows=1 width=28) (actual time=0.113..0.137 rows=6.00 loops=1)
              Recheck Cond: ((user_id = 'bench-real'::text) AND (tsv @@ '''beer'' | ''like'''::tsquery))
              Filter: ((status = 'active'::text) AND (kind = ANY ('{summary,note,import,turn}'::text[])))
              Heap Blocks: exact=5
              Buffers: shared hit=18
              ->  BitmapAnd  (cost=54.25..54.25 rows=1 width=0) (actual time=0.093..0.094 rows=0.00 loops=1)
                    Buffers: shared hit=13
                    ->  Bitmap Index Scan on episode_session  (cost=0.00..4.66 rows=33 width=0) (actual time=0.021..0.021 rows=122.00 loops=1)
                          Index Cond: (user_id = 'bench-real'::text)
                          Index Searches: 1
                          Buffers: shared hit=3
                    ->  Bitmap Index Scan on episode_tsv  (cost=0.00..49.34 rows=500 width=0) (actual time=0.070..0.070 rows=9.00 loops=1)
                          Index Cond: (tsv @@ '''beer'' | ''like'''::tsquery)
                          Index Searches: 1
                          Buffers: shared hit=10
Planning:
  Buffers: shared hit=7
Planning Time: 0.178 ms
Execution Time: 0.164 ms
```

**history (fact_key)**
```
Sort  (cost=8.45..8.46 rows=1 width=32) (actual time=0.027..0.028 rows=0.00 loops=1)
  Sort Key: asserted_at DESC, ingested_at DESC
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=3
  ->  Index Scan using fact_key on fact  (cost=0.42..8.44 rows=1 width=32) (actual time=0.025..0.025 rows=0.00 loops=1)
        Index Cond: ((user_id = 'bench-real'::text) AND (subject = 'bench-u00'::text) AND (predicate = 'current_focus'::text))
        Index Searches: 1
        Buffers: shared hit=3
Planning:
  Buffers: shared hit=3
Planning Time: 0.126 ms
Execution Time: 0.038 ms
```

**as_of unscoped (limit 50)**
```
Limit  (cost=2.55..108.91 rows=50 width=81) (actual time=0.179..0.279 rows=50.00 loops=1)
  Buffers: shared hit=110
  ->  Unique  (cost=2.55..1121.44 rows=526 width=81) (actual time=0.179..0.274 rows=50.00 loops=1)
        Buffers: shared hit=110
        ->  Incremental Sort  (cost=2.55..1117.48 rows=527 width=81) (actual time=0.178..0.264 rows=50.00 loops=1)
              Sort Key: subject, predicate, (CASE WHEN (cardinality = 'set'::text) THEN value_norm ELSE ''::text END), asserted_at DESC
              Presorted Key: subject, predicate
              Full-sort Groups: 2  Sort Method: quicksort  Average Memory: 27kB  Peak Memory: 27kB
              Buffers: shared hit=110
              ->  Index Scan using fact_key on fact  (cost=0.42..1094.19 rows=527 width=81) (actual time=0.078..0.168 rows=65.00 loops=1)
                    Index Cond: ((user_id = 'bench-real'::text) AND (status = ANY ('{active,superseded}'::text[])))
                    Filter: ((valid_from <= now()) AND ((valid_to IS NULL) OR (valid_to > now())))
                    Index Searches: 1
                    Buffers: shared hit=107
Planning:
  Buffers: shared hit=16
Planning Time: 0.194 ms
Execution Time: 0.309 ms
```

**stale_hints (1 key)**
```
Limit  (cost=0.42..28.71 rows=1 width=4) (actual time=0.125..0.125 rows=0.00 loops=1)
  Buffers: shared hit=115
  ->  Index Scan using episode_user_time on episode e  (cost=0.42..28.71 rows=1 width=4) (actual time=0.125..0.125 rows=0.00 loops=1)
        Index Cond: ((user_id = 'bench-real'::text) AND (occurred_at > (now() - '400 days'::interval)))
        Filter: ((tsv @@ '''favorit'' & ''beer'''::tsquery) AND (status = 'active'::text) AND (((hook || ' '::text) || body) !~~* '%IPA%'::text))
        Rows Removed by Filter: 122
        Index Searches: 1
        Buffers: shared hit=115
Planning:
  Buffers: shared hit=10
Planning Time: 0.135 ms
Execution Time: 0.135 ms
```

</details>

#### EXPLAIN (ANALYZE, BUFFERS) summaries [150k-bulk-user] — GUCs {'hnsw.ef_search': '40', 'hnsw.iterative_scan': 'off', 'hnsw.max_scan_tuples': '20000', 'hnsw.scan_mem_multiplier': '1', 'shared_buffers': '256MB', 'work_mem': '4MB', 'maintenance_work_mem': '128MB', 'effective_cache_size': '4GB', 'max_connections': '100'}

| query | scan | exec ms | buffers (shared hit/read) |
|---|---|---|---|
| fact vector (ef_search=64, iterative_scan=off) | HNSW | 8.567 ms | shared hit=1551 |
| fact vector (ef_search=64, iterative_scan=relaxed_order) | HNSW | 1386.503 ms | shared hit=11047 read=11985 |
| fact vector (ef_search=40 default) | HNSW | 2.151 ms | shared hit=1014 |
| fact BM25 (GIN) | GIN | 5.042 ms | shared hit=49 read=101 |
| episode vector (ef_search=64) | HNSW | 4.790 ms | shared hit=631 read=118 |
| episode BM25 (GIN) | GIN | 0.845 ms | shared hit=17 read=17 |
| history (fact_key) | fact_key | 0.484 ms | shared hit=1 read=2 |
| as_of unscoped (limit 50) | fact_key | 1.260 ms | shared hit=41 read=32 |
| stale_hints (1 key) | GIN | 0.526 ms | shared hit=26 |

<details><summary>full plans</summary>

**fact vector (ef_search=64, iterative_scan=off)**
```
Limit  (cost=1944.00..4294.12 rows=40 width=32) (actual time=8.436..8.438 rows=0.00 loops=1)
  Buffers: shared hit=1551
  ->  Index Scan using fact_vec on fact  (cost=1944.00..227672.91 rows=3842 width=32) (actual time=8.435..8.435 rows=0.00 loops=1)
        Order By: (embedding <=> '[<768-d query vector>]'::vector)
        Filter: ((embedding IS NOT NULL) AND (user_id = 'bench-u05'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])) AND ((valid_to IS NULL) OR (valid_to > now())))
        Rows Removed by Filter: 64
        Index Searches: 1
        Buffers: shared hit=1551
Planning:
  Buffers: shared hit=323
Planning Time: 3.098 ms
Execution Time: 8.567 ms
```

**fact vector (ef_search=64, iterative_scan=relaxed_order)**
```
Limit  (cost=1944.00..4294.12 rows=40 width=32) (actual time=72.467..1386.069 rows=40.00 loops=1)
  Buffers: shared hit=11047 read=11985
  ->  Index Scan using fact_vec on fact  (cost=1944.00..227672.91 rows=3842 width=32) (actual time=72.465..1386.027 rows=40.00 loops=1)
        Order By: (embedding <=> '[<768-d query vector>]'::vector)
        Filter: ((embedding IS NOT NULL) AND (user_id = 'bench-u05'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])) AND ((valid_to IS NULL) OR (valid_to > now())))
        Rows Removed by Filter: 1421
        Index Searches: 1
        Buffers: shared hit=11047 read=11985
Planning:
  Buffers: shared hit=1
Planning Time: 0.371 ms
Execution Time: 1386.503 ms
```

**fact vector (ef_search=40 default)**
```
Limit  (cost=1376.79..3732.82 rows=40 width=32) (actual time=2.107..2.108 rows=0.00 loops=1)
  Buffers: shared hit=1014
  ->  Index Scan using fact_vec on fact  (cost=1376.79..227672.91 rows=3842 width=32) (actual time=2.105..2.105 rows=0.00 loops=1)
        Order By: (embedding <=> '[<768-d query vector>]'::vector)
        Filter: ((embedding IS NOT NULL) AND (user_id = 'bench-u05'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])) AND ((valid_to IS NULL) OR (valid_to > now())))
        Rows Removed by Filter: 40
        Index Searches: 1
        Buffers: shared hit=1014
Planning:
  Buffers: shared hit=1
Planning Time: 0.412 ms
Execution Time: 2.151 ms
```

**fact BM25 (GIN)**
```
Limit  (cost=674.53..674.63 rows=40 width=28) (actual time=4.959..4.975 rows=40.00 loops=1)
  Buffers: shared hit=49 read=101
  ->  Sort  (cost=674.53..674.86 rows=131 width=28) (actual time=4.957..4.965 rows=40.00 loops=1)
        Sort Key: (ts_rank_cd(tsv, '''beer'' | ''like'''::tsquery)) DESC, asserted_at DESC
        Sort Method: top-N heapsort  Memory: 29kB
        Buffers: shared hit=49 read=101
        ->  Bitmap Heap Scan on fact  (cost=151.79..670.39 rows=131 width=28) (actual time=2.682..4.790 rows=158.00 loops=1)
              Recheck Cond: ((tsv @@ '''beer'' | ''like'''::tsquery) AND (user_id = 'bench-u05'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])))
              Filter: ((valid_to IS NULL) OR (valid_to > now()))
              Heap Blocks: exact=124
              Buffers: shared hit=43 read=101
              ->  BitmapAnd  (cost=151.79..151.79 rows=147 width=0) (actual time=2.486..2.489 rows=0.00 loops=1)
                    Buffers: shared hit=20
                    ->  Bitmap Index Scan on fact_tsv  (cost=0.00..72.67 rows=3452 width=0) (actual time=1.895..1.896 rows=3547.00 loops=1)
                          Index Cond: (tsv @@ '''beer'' | ''like'''::tsquery)
                          Index Searches: 1
                          Buffers: shared hit=13
                    ->  Bitmap Index Scan on fact_user_status  (cost=0.00..78.80 rows=4313 width=0) (actual time=0.347..0.347 rows=4339.00 loops=1)
                          Index Cond: ((user_id = 'bench-u05'::text) AND (status = 'active'::text) AND (layer = ANY ('{profile,semantic,procedural}'::text[])))
                          Index Searches: 1
                          Buffers: shared hit=7
Planning:
  Buffers: shared hit=76
Planning Time: 2.720 ms
Execution Time: 5.042 ms
```

**episode vector (ef_search=64)**
```
Limit  (cost=1804.26..2992.59 rows=20 width=24) (actual time=4.754..4.755 rows=0.00 loops=1)
  Buffers: shared hit=631 read=118
  ->  Index Scan using episode_vec on episode  (cost=1804.26..118853.96 rows=1970 width=24) (actual time=4.752..4.752 rows=0.00 loops=1)
        Order By: (embedding <=> '[<768-d query vector>]'::vector)
        Filter: ((embedding IS NOT NULL) AND (user_id = 'bench-u05'::text) AND (status = 'active'::text) AND (kind = ANY ('{summary,note,import,turn}'::text[])))
        Rows Removed by Filter: 63
        Index Searches: 1
        Buffers: shared hit=631 read=118
Planning:
  Buffers: shared hit=99
Planning Time: 0.724 ms
Execution Time: 4.790 ms
```

**episode BM25 (GIN)**
```
Limit  (cost=253.33..253.38 rows=20 width=28) (actual time=0.799..0.802 rows=0.00 loops=1)
  Buffers: shared hit=17 read=17
  ->  Sort  (cost=253.33..253.38 rows=20 width=28) (actual time=0.798..0.800 rows=0.00 loops=1)
        Sort Key: (ts_rank_cd(tsv, '''beer'' | ''like'''::tsquery)) DESC, occurred_at DESC
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=17 read=17
        ->  Bitmap Heap Scan on episode  (cost=158.04..252.90 rows=20 width=28) (actual time=0.791..0.792 rows=0.00 loops=1)
              Recheck Cond: ((tsv @@ '''beer'' | ''like'''::tsquery) AND (user_id = 'bench-u05'::text))
              Filter: ((status = 'active'::text) AND (kind = ANY ('{summary,note,import,turn}'::text[])))
              Buffers: shared hit=17 read=17
              ->  BitmapAnd  (cost=158.04..158.04 rows=25 width=0) (actual time=0.782..0.783 rows=0.00 loops=1)
                    Buffers: shared hit=17 read=17
                    ->  Bitmap Index Scan on episode_tsv  (cost=0.00..70.60 rows=502 width=0) (actual time=0.285..0.286 rows=32.00 loops=1)
                          Index Cond: (tsv @@ '''beer'' | ''like'''::tsquery)
                          Index Searches: 1
                          Buffers: shared hit=15
                    ->  Bitmap Index Scan on episode_user_time  (cost=0.00..87.19 rows=2503 width=0) (actual time=0.490..0.490 rows=2500.00 loops=1)
                          Index Cond: (user_id = 'bench-u05'::text)
                          Index Searches: 1
                          Buffers: shared hit=2 read=17
Planning:
  Buffers: shared hit=7
Planning Time: 0.401 ms
Execution Time: 0.845 ms
```

**history (fact_key)**
```
Sort  (cost=8.45..8.46 rows=1 width=32) (actual time=0.459..0.459 rows=0.00 loops=1)
  Sort Key: asserted_at DESC, ingested_at DESC
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=1 read=2
  ->  Index Scan using fact_key on fact  (cost=0.42..8.44 rows=1 width=32) (actual time=0.454..0.454 rows=0.00 loops=1)
        Index Cond: ((user_id = 'bench-u05'::text) AND (subject = 'bench-u00'::text) AND (predicate = 'current_focus'::text))
        Index Searches: 1
        Buffers: shared hit=1 read=2
Planning:
  Buffers: shared hit=3
Planning Time: 0.246 ms
Execution Time: 0.484 ms
```

**as_of unscoped (limit 50)**
```
Limit  (cost=2.31..83.35 rows=50 width=81) (actual time=0.885..1.209 rows=50.00 loops=1)
  Buffers: shared hit=41 read=32
  ->  Unique  (cost=2.31..6818.37 rows=4205 width=81) (actual time=0.884..1.197 rows=50.00 loops=1)
        Buffers: shared hit=41 read=32
        ->  Incremental Sort  (cost=2.31..6786.56 rows=4242 width=81) (actual time=0.882..1.170 rows=50.00 loops=1)
              Sort Key: subject, predicate, (CASE WHEN (cardinality = 'set'::text) THEN value_norm ELSE ''::text END), asserted_at DESC
              Presorted Key: subject, predicate
              Full-sort Groups: 2  Sort Method: quicksort  Average Memory: 27kB  Peak Memory: 27kB
              Buffers: shared hit=41 read=32
              ->  Index Scan using fact_key on fact  (cost=0.42..6620.80 rows=4242 width=81) (actual time=0.290..0.908 rows=65.00 loops=1)
                    Index Cond: ((user_id = 'bench-u05'::text) AND (status = ANY ('{active,superseded}'::text[])))
                    Filter: ((valid_from <= now()) AND ((valid_to IS NULL) OR (valid_to > now())))
                    Rows Removed by Filter: 9
                    Index Searches: 1
                    Buffers: shared hit=38 read=32
Planning:
  Buffers: shared hit=16
Planning Time: 0.469 ms
Execution Time: 1.260 ms
```

**stale_hints (1 key)**
```
Limit  (cost=102.27..106.31 rows=1 width=4) (actual time=0.489..0.491 rows=0.00 loops=1)
  Buffers: shared hit=26
  ->  Bitmap Heap Scan on episode e  (cost=102.27..106.31 rows=1 width=4) (actual time=0.488..0.489 rows=0.00 loops=1)
        Recheck Cond: ((user_id = 'bench-u05'::text) AND (occurred_at > (now() - '400 days'::interval)) AND (tsv @@ '''favorit'' & ''beer'''::tsquery))
        Filter: ((status = 'active'::text) AND (((hook || ' '::text) || body) !~~* '%IPA%'::text))
        Buffers: shared hit=26
        ->  BitmapAnd  (cost=102.27..102.27 rows=1 width=0) (actual time=0.478..0.479 rows=0.00 loops=1)
              Buffers: shared hit=26
              ->  Bitmap Index Scan on episode_user_time  (cost=0.00..33.62 rows=920 width=0) (actual time=0.155..0.155 rows=956.00 loops=1)
                    Index Cond: ((user_id = 'bench-u05'::text) AND (occurred_at > (now() - '400 days'::interval)))
                    Index Searches: 1
                    Buffers: shared hit=9
              ->  Bitmap Index Scan on episode_tsv  (cost=0.00..68.40 rows=64 width=0) (actual time=0.313..0.313 rows=24.00 loops=1)
                    Index Cond: (tsv @@ '''favorit'' & ''beer'''::tsquery)
                    Index Searches: 1
                    Buffers: shared hit=17
Planning:
  Buffers: shared hit=10
Planning Time: 0.372 ms
Execution Time: 0.526 ms
```

</details>

#### Filtered HNSW on a bulk user (`bench-u05`: 4339 active of 100669 facts), 20 random query vectors [150k]

| hnsw.iterative_scan | fact candidates of LIMIT 40 (min / mean / max; queries <40) | fact_vec ms p50 / p95 | episode candidates of LIMIT 20 (min / mean / max; <20) | episode_vec ms p50 / p95 |
|---|---|---|---|---|
| off | 0 / 1.9 / 5; 20 | 78.7 / 170.3 | 0 / 3.1 / 7; 20 | 35.2 / 55.2 |
| relaxed_order | 40 / 40.0 / 40; 0 | 125.2 / 249.8 | 20 / 20.0 / 20; 0 | 63.9 / 189.3 |

#### Cleanup — bench-* rows before → after wipe [150k, after adding migration 002]

| table | before | after |
|---|---|---|
| fact | 100655 | 0 |
| episode | 50255 | 0 |
| snapshot | (cleared) | 0 |
| audit | (cleared) | 0 |
| cognify_queue | (cleared) | 0 |
| tombstone | (cleared) | 0 |
| profile | 0 | 0 |
| profile_history | 0 | 0 |

- wipe took fact 7.06 s + episode 5.4 s (was >17 min and cancelled before the chain indexes); db total after: 1330 MB before VACUUM reclaim; fact 871 MB / episode 445 MB still allocated until vacuum/autovacuum returns the space

#### Verdict vs thresholds

| threshold | measured | result |
|---|---|---|
| recall p95 < 500 ms e2e, 8 concurrent clients, at scale (TEI-inclusive) | 1406.0 | FAIL |
|   (info) recall p95 e2e, 1 client, at scale | 312.8 | ok |
| DB-only recall p95 < 80 ms at scale (real-embedding user, scan=off as deployed) | 67.7 | PASS |
|   (info) same with the iterative-scan fix (scan=relaxed_order) | 55.8 | ok |
| capture p95 < 400 ms (1 client, at scale) | 269.7 | PASS |
|   (info) capture p95 under mixed load (8 recall + 4 capture + 2 facts clients) | 3236.3 | short |
| zero HTTP errors across all phases | 0 | PASS |
| no container OOM kill | 0 | PASS |
| exactly 1 active under 20 concurrent /correct | True | PASS |
|   (info) DB-only recall p95, 8 parallel connections [150k real-user scan=off] | 232.2 | ok |
|   (info) DB-only recall p95, 8 parallel connections [150k real-user scan=relaxed_order (fix)] | 238.7 | ok |
|   (info) DB-only recall p95, 8 parallel connections [150k bulk-user 4% share scan=relaxed_order (fix, worst case)] | 584.4 | short |
|   (info) HNSW candidates of 40 for a 4%-share user, scan=off [150k] (want 40) | 1.9 | short |
|   (info) same with scan=relaxed_order (the fix) | 40.0 | ok |



## 3. Defects found by the run and fixed

| defect | evidence | fix | after |
|---|---|---|---|
| **Filtered HNSW starvation** — `user_id/status/layer` post-filter on `ef_search=64` candidates; minority users get almost no vector candidates | 4 %-share user: mean **1.9 / 40** candidates (20/20 queries short), EXPLAIN `rows=0` in 8.5 ms; semantic recall silently BM25-only | `astoria/retrieval/recall.py`: `_hnsw_gucs()` sets `hnsw.iterative_scan = relaxed_order` next to `ef_search=64` in both vector candidate queries | **40 / 40**; DB-only recall for that user 204 / 269 ms p50/p95 (scan=off was 91/144 but wrong); zero cost for a well-separated/dominant user (8 conns: 232 → 239 ms p95); `tests/test_recall.py` 10/10 |
| **Unindexed self-FK on the supersede chain** — `fact.supersedes` / `fact.superseded_by` (`ON DELETE SET NULL`) had no index → a table scan per deleted fact | the first bench wipe (`DELETE FROM fact WHERE user_id LIKE 'bench-%'`, 100 655 rows) ran **17+ min and had to be cancelled**; the same cost hits `DELETE /users/{id}` and `forget mode=hard` | `astoria/sql/002_chain_indexes.sql`: partial btree indexes on both columns | **fact: 100 655 rows deleted in 7.06 s; episode: 50 255 in 5.4 s**; also speeds `history` chain walks |
| **Embedding inside the per-key advisory lock** (`facts.upsert_fact`) serialised 20 concurrent `/correct` on one key behind 20 sequential embeds | `/correct` × 20: p50 2 153 ms / p95 4 130 ms, all 200, exactly 1 active (correct, just slow) | embedding now happens **before** the lock is taken | lock hold time drops from ~200 ms to the few-ms supersede txn |
| **Embedding path** — every recall/capture/facts call paid a CPU TEI embed (170 ms single, **~6.5 embeds/s ceiling** at 400 % CPU) | concurrency sweep flat at 6.2 req/s from 4 clients; 8 clients p95 1 406 ms; mixed load ~3 s for everything | prioritised embedding endpoints (`ASTORIA_EMBED_URLS`, a GPU endpoint first, CPU TEI as the always-on fallback), the asynchronous write path (`ASTORIA_EMBED_SYNC=false`), and an LRU query-embedding cache | the ~6 req/s wall in this document is the CPU-TEI path; the store itself sustained **44.6 recalls/s** (8 parallel DB-only connections, p95 232 ms) — re-run `concurrency` / `mixed` to get the new e2e ceiling |

Non-defects worth recording: recall's `access_count/last_seen` touch-UPDATE is **97.8 % HOT** (64 890 / 66 344) so it does not bloat the HNSW
index; GIN BM25 plans are `BitmapAnd(fact_user_status, fact_tsv)` at 2–5 ms; for a small user in the big table the planner skips HNSW and does an
exact btree+sort kNN (`bench-real`, 591 rows: 17.5 ms, all shared hits) — exact and cheap; the cognify worker (cloud LLM) drained 4 jobs/min and
added no p95 interference to concurrent recall (841 → 753 ms p95 idle vs draining; p99 945 → 1 591 ms).


## 4. Growth projection — 1 M facts / 500 k episodes

Per-row costs measured at 100 571 facts / 50 032 episodes (after `VACUUM ANALYZE`):

| | heap | TOAST (the 768-d vector, 3 080 B, stored out-of-line) | HNSW index | other indexes | **total / row** |
|---|---|---|---|---|---|
| fact | 0.45 KB | 3.95 KB | 3.91 KB (`fact_vec` 393 MB) | 0.33 KB (7 btree/GIN, 33 MB) | **8.6 KB** |
| episode | 0.66 KB | 3.96 KB | 3.90 KB (`episode_vec` 195 MB) | 0.35 KB | **8.9 KB** |

| metric | measured @150 k | projected @1 M facts + 500 k episodes | basis |
|---|---|---|---|
| database size | 1.32 GB | **≈ 13 GB** (facts 8.6 GB + episodes 4.4 GB) | linear, 8.6 / 8.9 KB per row |
| HNSW indexes | 588 MB | **≈ 5.9 GB** (fact_vec 3.9 GB + episode_vec 2.0 GB) | 3.9 KB per row (m=16) |
| TOAST vectors | 595 MB | ≈ 5.9 GB | 3.95 KB per row |
| Postgres cgroup RSS at rest | ~680–720 MiB of 1 GiB (256 MB shared_buffers + page cache of the hot HNSW pages) | **page cache cannot hold the graph: ≥ 5 GB of HNSW vs ≤ 0.75 GB cache** | cgroup v2 charges file pages; they are reclaimable, so the failure mode is *latency*, not OOM |
| in-place HNSW insert | 107 facts/s, 121 episodes/s (Postgres at 100 % of one core; 202 → 107 rows/s from 0 → 100 k) | ~60–80 rows/s by 1 M (log-ish decay); organic growth is fine (1 M facts = 2–3 h of CPU); a *bulk* import should use `seed.py --index-mode rebuild` (drop → COPY → CREATE INDEX) | measured slope |
| HNSW build memory (rebuild) | 128 MB `maintenance_work_mem` holds ~40 k vectors in-memory | 1 M × (3 KB + links) ≈ 3.5 GB needed to build in RAM; otherwise pgvector falls to the slow on-disk phase | pgvector docs |
| recall DB-only, dominant / well-separated user | p50 49 ms, p95 68 ms (warm) | **≈ 60–80 ms warm** (HNSW cost ~log N: ×1.15); **cold: seconds** — each HNSW probe touches ~1 000–2 000 8 KB pages on spinning storage (~150 random IOPS) | EXPLAIN buffers (1 551 hits warm; 11 047 hit + 11 985 read cold = 1.39 s) |
| recall DB-only, minority user (≤ 5 % share) with the iterative-scan fix | p50 204 ms, p95 269 ms warm | bounded by `hnsw.max_scan_tuples` (20 000 tuples ≈ 1–1.5 s warm, more cold); falls back to < 40 candidates beyond that | measured bench-u05 |
| write path | capture 234/270 ms, POST /facts 263/289 ms (single client) — ~180 ms of each is the embed (removed from the request by the asynchronous write path) | unchanged by store size (HNSW insert ≈ 5–8 ms of it) | measured |

**When does the 1 GiB Postgres limit / 256 MB shared_buffers need changing?** Early. At 150 k rows the
cgroup already sits at 680–720 MiB because the page cache is holding the 588 MB of HNSW; the usable cache
(1 GiB − 256 MB shared_buffers − ~100 MiB backends) is ≈ 650 MB, so **beyond ~170 k embedded rows the hot
graph no longer fits and every vector probe starts reading disk**. The knee is not 1 M, it is ~200 k.
Before the store passes ~150 k embedded rows: `mem_limit` 2–3 GiB and `shared_buffers` 768 MB–1 GB, *or*
cut the vector footprint in half with a `halfvec` HNSW index (`USING hnsw ((embedding::halfvec(768))
halfvec_cosine_ops)`, pgvector ≥ 0.7: 1.9 KB/row), *or* stop embedding turns (70 % of episodes are
`kind='turn'`; archive_old_turns already runs — embed only summaries/notes and `episode_vec` shrinks ~70 %).


## 5. Top scaling risks and mitigations

1. **Filtered-HNSW candidate starvation in a multi-user index (correctness, not speed).** pgvector applies
   `WHERE user_id=… AND status=… AND layer=…` *after* pulling `ef_search=64` graph candidates. Measured at
   150 k rows: a user with a 4 % share of the index got **mean 1.9 / 40** vector candidates — semantic recall
   silently collapses to BM25-only for every non-dominant user. *Fixed:* `recall.py` sets
   `hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8): 40/40 candidates, DB-only recall for that user
   204/269 ms p50/p95. For the dominant user the setting is free. Longer term, for several heavy users: a
   **partial HNSW index per heavy user** (`CREATE INDEX … USING hnsw(...) WHERE user_id='<id>'`) or
   **LIST/HASH partitioning by `user_id`**; keep `hnsw.max_scan_tuples` (default 20 000) as the worst-case
   bound.
2. **HNSW working set vs a small Postgres cgroup on spinning disks.** 3.9 KB of HNSW + 3.95 KB of TOAST per
   embedded row; cache ceiling ≈ 650 MB ≈ 170 k rows here. Past that, probes go to disk (cold relaxed-order
   probe measured at 1.39 s with 12 k page reads). Mitigations (pick two): raise `mem_limit`/`shared_buffers`
   (2 GiB / 768 MB), `halfvec` HNSW (halves the index), don't embed raw turns, and keep vacuum honest
   (schema 004 sets `autovacuum_vacuum_scale_factor=0.02` and `fillfactor=90` on `fact`/`episode`; recall's
   touch-update is 97.8 % HOT). **Mass delete / hard-forget / user wipe relies on the chain indexes**
   (`002_chain_indexes.sql`) — keep any future self-referencing FK indexed.
3. **The embedder is the real ceiling, not Postgres.** On a CPU TEI every recall, capture and POST /facts
   spent one embed (170 ms, **~6.5 embeds/s total** on 4 cores). Mitigations now in the code, in order of
   payoff: the **asynchronous write path** (capture/POST /facts write with `embedding NULL`; `embed_backfill`
   fills it — the request no longer competes with recall for the embedder); the **LRU query-embedding
   cache**; **prioritised endpoints** so a GPU embedding seat (a few ms per query) answers first and the CPU
   TEI carries the fallback; and a DB pool (max 8) ≥ the expected recall concurrency.


## 6. Reranker evaluation (cross-encoder stage on vs off)

`scripts/bench/rerank_eval.py` runs 17 query → expected-predicate cases against a real user's store, once
with the cross-encoder stage on and once with it off (2 repetitions, 34 calls per side), in-process inside
the service container so latency excludes HTTP. Reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` on the
NAS CPU (TEI, ONNX backend), top-30 facts + 6 episodes, 240-char texts, weight 0.6.

| metric (mean over cases) | stage off | stage on | note |
|---|---|---|---|
| MRR (first relevant item) | 0.765 | **0.814** | the reranker mostly fixes *which* relevant item is first |
| hit@k | 0.941 | 0.941 | recall set unchanged (it reorders, it does not add) |
| precision@5 | 0.494 | 0.459 | slight loss: a few relevant-but-generic rows drop below k |
| avoid@5 (unwanted predicates in top-5, lower is better) | 0.294 | **0.118** | the motivating case — a query about family no longer ranks an owned-equipment fact above the spouse |
| cold latency p50 / p95 (ms) | 89 / 471 | 585 / 739 | CPU cross-encoder cost; warm (cached pairs) p50 55 → 264 ms |

Verdict: a real but modest ranking gain for a few hundred milliseconds on a CPU reranker; keep it on for
interactive clients whose queries repeat (the logit cache makes repeats free), turn it off per request
(`rerank=false`) for latency-critical paths, and move the stage to a GPU endpoint to raise `top_n`.

**Asynchronous write path**: measured in run 2 (§7.2 "Write path"): `/capture` 6.4 ms p50 / 8.0 ms p95 and
`POST /facts` 8.5 / 11.0 ms at 150 k rows with the default async embed (234 / 270 and 263 / 289 ms in run 1),
122 / 133 and 135 / 171 ms with `sync:true`; the worker embeds new rows within 24–37 s on a quiet store and
falls behind under write bursts (§7.4 defects B/C).

## 7. 2026-08 run 2 (feature-complete build)

Same harness, same shape of load (20 bench users × 5 000 facts + 2 500 episodes by direct COPY, plus the 500 real-embedding
facts of `bench-real` written through `POST /facts`), same NAS, run against the feature-complete build: async write-path embedding,
the cross-encoder rerank stage, the workstation embedding seat, migrations 002–004, graph expansion, `hnsw.iterative_scan=relaxed_order`
in the deployed code, and belief-axis versioning. Raw records: `scripts/bench/results/2026-08-22-run2.jsonl`
(`scripts/bench/report.py` on it reproduces every table below). Labels: `pre-load` = 500 real facts, ~0 other rows;
`150k` = 101 546 facts + 51 488 episodes after the deep load; `rerank=on|off` = the `/recall` request flag;
`fresh-queries` = a nonce per query (service caches cold); `+2000 edges` = 1 989 seeded graph edges on `bench-real`.
The run-1 numbers quoted for comparison are from §2.

### 7.0 Verdict (short)

**The read path got ~2.5× faster per call and the write path ~30× faster, and the ceiling moved from the NAS embedder to the service
process itself.** Single-client `/recall` is **80 ms p50 / 103 ms p95** at 150 k rows with the rerank stage off (run 1: 205 / 225–313 ms)
and **282 / 517 ms** with it on, cold; `/capture` is **6 ms p50 / 8 ms p95** (run 1: 234 / 270) and `POST /facts` 8.5 / 11 ms (run 1: 263 / 289)
because the embed now happens in the worker. With the service's query-embed and rerank caches warm, 8 concurrent recall clients see
**p95 493 ms (rerank off) / 500 ms (rerank on)** — on the line, both sides plateau at **~20 req/s with the single uvicorn worker at
~110 % CPU** (PG at ~100 %). With every query fresh (caches cold) the same 8 clients see p95 **1 121 ms off / 1 790 ms on**: the
workstation embedding seat tops out at ~22 embeds/s and the workstation reranker at ~12 calls/s, so 8 uncached recalls queue behind
them exactly as they queued behind the NAS TEI in run 1 — lower floor (65 ms embed, 116 ms rerank), same shape.

Three things regressed or surfaced and are the work items from this run:

1. **The DB-only recall p95 is now 87–94 ms (threshold 80; run 1: 56–68).** The extra ~20 ms is graph expansion, which runs a seed-subject
   lookup, a recursive edge walk and up to ten "facts about this subject" queries per recall *even when the user has no edges* (0 edges
   on the whole store during the phase). With 2 000 edges on the user it costs **174 ms p50 / 243 ms p95** and DB-only recall becomes
   253 / 329 ms.
2. **The mixed load (8 recall + 4 capture + 2 facts clients) produced 9 HTTP 500s (2 `DeadlockDetected`, 7 `PoolTimeout` after 30 s) and
   18–34 s stalls.** Cause: the worker's `embed_backfill` embeds and UPDATEs up to 200 facts + 200 episodes in **one transaction** whose
   duration under load was ~30 s (ticks landed 60 s apart for a 30 s tick); recall's `access_count/last_seen` touch-UPDATE on freshly
   written rows waits on those row locks, connections pile up behind the waits (pool max 8, getconn timeout 30 s), and the two
   UPDATE orders deadlock. Not a store-size effect: it is write-rate × backfill-transaction-length.
3. **The async-embed "recall gap" is bounded by the backfill rate, not by the 30 s tick.** Quiet store: new rows are embedded after
   24 s (pre-load) / 37 s (150 k). After the 60 s mixed load had written ~1 400 rows the backlog drained at ~4–7 rows/s, so 20 new
   episodes were **still un-embedded 121 s later** (BM25-only for that long).

| contract threshold | run 1 @150 k | run 2 @150 k | result |
|---|---|---|---|
| recall p95 < 500 ms e2e, **8 concurrent clients** | 1 406 ms (NAS-TEI-bound) | **493 ms rerank off · 500 ms rerank on** (warm caches, ~20 req/s, service CPU-bound) · 1 121 / 1 790 ms with fresh queries | **PASS (off, marginal) / FAIL by 0.2 ms (on)**; FAIL cold |
| DB-only recall p95 < 80 ms | 67.7 (55.8 with the iterative-scan fix) | **90.2 ms** (87–94 across phases; graph expansion +20 ms) | **FAIL** (regression, fixable) |
| capture p95 < 400 ms | 270 single / 3 236 mixed | **8.0 ms single · 306–335 ms mixed** | PASS (both) |
| zero errors | 0 | **9** (mixed load, rerank on: 2 deadlocks + 7 pool timeouts) | **FAIL** (defect 7.4-B) |
| no OOM | pass | `OOMKilled=false`, 0 restarts (PG peak 824 MiB/1 GiB in the DB-only hammer) | PASS |
| exactly 1 active under 20 concurrent `/correct` | pass (p95 4 130 ms) | **1 active, history 20, p95 436 ms** | PASS (9.5× faster — embed outside the lock) |

### 7.1 What changed since run 1 (and how each change was measured)

| change | where | measured by |
|---|---|---|
| write-path embedding is **asynchronous**: `/capture` and `POST /facts` return before the embed; the worker's `embed_backfill` fills `embedding` on its next tick (30 s, ≤200 facts + 200 episodes per tick) | `settings.embed_sync=False`; per-request `sync:true` opts back in | `baseline` (async vs `sync:true`), `embed-gap` (time until new rows are embedded), `real-seed` |
| **cross-encoder rerank** over the top-30 fact + 6 episode candidates (TEI MiniLM-L6; workstation endpoint `:8935` first, NAS `:8935` fallback); request flag `rerank:false` bypasses; `(query, hook)` LRU 4 096 | `astoria/core/rerank.py`, `recall._apply_rerank` | `rerank-floor` (per endpoint), every `/recall` phase twice (`--rerank on/off`), `concurrency` both ways, warm and `--unique` |
| embeddings prefer the **workstation nomic seat via SAINT** (`:4000`) over the NAS TEI; query LRU 1 024 | `astoria/core/embed.py` | `embed-floor` (per endpoint), e2e recall |
| schema **002** (chain indexes) + **004** (autovacuum 0.02 / fillfactor 90 on `fact`/`episode`) | `astoria/sql/` | wipe time, `pg_stat_user_tables` HOT ratio |
| **graph expansion** in recall (bounded walk from the top-10 seeds + facts about their subjects; depth 2, fanout 20) | `astoria/retrieval/graph.py`, migration 003 | DB-only step `graph_expand(sql)`; `recall` with 0 and with 2 000 seeded edges |
| `hnsw.iterative_scan=relaxed_order` in the deployed `recall.py` | `recall._hnsw_gucs` | `filter`, `recall` bulk user, `db-concurrency` |
| **belief-axis versioning**: each supersede writes a versioned copy of the closed row (+1 row per `/correct`) | `facts._close_versioned` | `correct` (20 concurrent), `correct-seq` (50 sequential on one key), `chain` |

Environment delta vs §1: `astoria` 768 MiB / 1 worker / pool max 8 (unchanged); `astoria-postgres` unchanged (1 GiB, `shared_buffers=256MB`);
new `astoria-rerank` container on the NAS (1 GiB, MiniLM-L6 CPU) and a second reranker + the nomic seat on the workstation (both
"nightly-off", the NAS copies are the fallback). The client (workstation) carried a load average of ~5 during the run, which is also
the host of the preferred embed and rerank endpoints — treat the workstation-endpoint floors as upper bounds. The store was restarted by a
deploy mid-load (bench-u04 rolled back and re-seeded; the load table below shows the resumed part only: combined **100 012 facts in
916 s = 109/s, 50 000 episodes in 408 s = 122/s**, identical to run 1's 107 / 121).

### 7.2 Results

#### Per-endpoint floors (fixed per-request costs; fresh texts, no cache)

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| embed [workstation seat via SAINT] 1 client | 20 | 64.7 | 96.8 | 413.5 | 492.6 |  | 0 |
| embed [workstation] 4 concurrent | 20 | 152.1 | 249.3 | 249.8 | 249.9 | 22.41 | 0 |
| embed [workstation] 8 concurrent | 40 | 363.4 | 498.3 | 499.0 | 499.4 | 22.16 | 0 |
| embed [NAS TEI] 1 client | 20 | 197.6 | 233.8 | 238.2 | 239.2 |  | 0 |
| embed [NAS TEI] 4 concurrent | 20 | 670.3 | 802.0 | 802.1 | 802.1 | 5.7 | 0 |
| embed [NAS TEI] 8 concurrent | 40 | 1360.9 | 1443.3 | 1716.2 | 1716.2 | 5.64 | 0 |
| rerank 30 hooks [workstation] 1 client | 20 | 116.4 | 132.8 | 133.4 | 133.5 |  | 0 |
| rerank 30 hooks [workstation] 4 concurrent | 20 | 330.2 | 369.6 | 427.9 | 442.5 | 11.31 | 0 |
| rerank 30 hooks [workstation] 8 concurrent | 40 | 633.6 | 717.6 | 755.7 | 779.9 | 12.08 | 0 |
| rerank 30 hooks [NAS] 1 client | 20 | 323.5 | 428.2 | 630.2 | 680.7 |  | 0 |
| rerank 30 hooks [NAS] 4 concurrent | 20 | 1417.5 | 1522.1 | 1552.5 | 1560.1 | 2.79 | 0 |
| rerank 30 hooks [NAS] 8 concurrent | 40 | 2704.6 | 2965.1 | 2970.9 | 2972.3 | 2.89 | 0 |

The workstation seat is ~3× faster per embed and ~3.5× higher throughput than the NAS TEI (22 vs 5.6 embeds/s); the workstation reranker
is ~3× faster than the NAS one (116 vs 324 ms per 30-hook call, 12 vs 2.9 calls/s). **If the workstation is off, a cold recall with rerank
costs ≈ 200 ms embed + 320 ms rerank + ~70 ms store ≈ 0.6 s single-client, and the NAS reranker alone caps recall at < 3 req/s.**

#### Write path — before / after (single client; run 1 in parentheses)

| case | n | p50 ms | p95 ms | p99 ms | max ms | errors |
|---|---|---|---|---|---|---|
| [150k] `/capture` cognify=false, **async embed (default)** | 30 | **6.4** (234.0) | **8.0** (269.7) | 8.5 | 8.7 | 0 |
| [150k] `/capture` `sync:true` (inline embed, workstation seat) | 30 | 122.4 | 133.0 | 395.1 | 501.7 | 0 |
| [150k] `POST /facts` novel set-fact, **async embed (default)** | 30 | **8.5** (262.9) | **11.0** (288.9) | 20.3 | 24.0 | 0 |
| [150k] `POST /facts` `sync:true` | 30 | 135.4 | 170.6 | 429.6 | 527.5 | 0 |
| [pre-load] `POST /facts` real ×500, 4 workers (the real-embedding seed) | 500 | 13.6 | 44.4 | 78.1 | 118.5 | 0 (49.6 req/s) |
| [150k] `POST /correct` × 50 sequential, one key (belief-axis versioned) | 50 | 13.3 | 54.0 | 56.9 | 57.6 | 0 (52.9 req/s) |
| [150k] `POST /correct` × 20 **concurrent**, one key | 20 | **217.2** (2 153) | **435.7** (4 130) | 453.0 | 457.3 | 0; 1 active, history 20 |

Async-embed gap (time from write until the worker has embedded the rows; `embed-gap`, 20 turns + 20 facts):

| store state | embedded at write | first row embedded after | all 40 embedded after |
|---|---|---|---|
| pre-load, worker idle | 0 | 24.1 s | 24.1 s |
| 150 k, **backlog of ~900 episodes from the mixed load still draining** | 0 | facts 37.3 s; episodes > 121 s | facts 37.3 s; **episodes not embedded within the 121 s window** |

Backfill ticks observed in the service log during the drain: `facts=200 episodes=200` at 22:49:04, 22:50:11, 22:51:12, 22:52:00, 22:53:01,
22:53:57 — i.e. ~60 s apart for a 30 s tick = each tick's transaction ran ~30 s; ~1 400 rows written in the 60 s mixed load took ~5.5 min to
embed (≈ 4.3 rows/s sustained). The 500-row real seed (idle store) took 100 s = 3 ticks.

#### `/recall` single client — before / after, rerank on vs off (real-embedding user, 50 queries)

| case | n | p50 ms | p95 ms | p99 ms | max ms | errors |
|---|---|---|---|---|---|---|
| run 1 [150k] e2e (NAS TEI, no rerank) | 50 | 209.6 | 312.8 | 454.8 | 544.1 | 0 |
| [150k] e2e **rerank off** | 50 | **79.8** | **102.5** | 109.1 | 110.5 | 0 |
| [150k] e2e **rerank on** (first pass: rerank cache cold) | 50 | **282.2** | **516.6** | 698.5 | 708.8 | 0 |
| [pre-load] e2e rerank off | 50 | 93.5 | 107.7 | 148.5 | 171.6 | 0 |
| [pre-load] e2e rerank on | 50 | 140.0 | 172.8 | 1708.4 | 3172.6 | 0 |
| run 1 [150k] DB-only (relaxed_order) | 48 | 49.5 | 55.8 | 57.3 | 57.7 | 0 |
| [150k] DB-only, deployed code (relaxed_order; rerank stage off in the probe) | 48 | **70.7** | **90.2** | 108.4 | 110.1 | 0 |
| [150k] same, session GUC scan=off (only the candidate-count query differs — the deployed `recall()` SET LOCALs relaxed_order) | 48 | 72.5 | 94.3 | 98.6 | 101.5 | 0 |
| [150k-bulk-user] e2e rerank off (4 %-share random-vector user) | 20 | 312.4 | 612.8 | 675.7 | 691.5 | 0 |
| [150k-bulk-user] DB-only relaxed_order | 20 | 187.1 (run 1: 203.8) | 219.3 (269.1) | 243.7 | 249.8 | 0 |
| [150k **+2000 edges**] e2e rerank off | 50 | 383.0 | 484.8 | 537.3 | 537.6 | 0 |
| [150k +2000 edges] DB-only relaxed_order | 48 | 252.8 | 329.2 | 365.6 | 387.6 | 0 |

Semantic hit-rate (expected predicate in the items, 20 probes): 1.0 at pre-load and 150 k both with and without rerank; 0.8 for the
bulk user (0.75 in run 1; random vectors); 0.9 with 2 000 random edges (two misses — graph candidates displacing the seed's answer
inside the budget is the mechanism to watch when real edges land). HNSW candidates for the 4 %-share user: **40/40** with the deployed
relaxed_order (2.6/40 with the session GUC forced off — the run-1 starvation, confirmed fixed in the shipped code).

DB-only per-step cost (in-container, ms p50 / p95; run 1 [150k] in the last column):

| step | pre-load | 150k | 150k-bulk-user | 150k +2000 edges | run 1 150k |
|---|---|---|---|---|---|
| fact_vec(hnsw) | 37.5 / 38.9 | 22.2 / 25.2 | 108.8 / 132.6 | 31.9 / 42.6 | 25.0 / 30.4 |
| fact_bm25(gin) | 2.8 / 6.4 | 5.9 / 11.4 | 4.8 / 13.1 | 4.6 / 12.7 | 4.7 / 9.6 |
| episode_vec(hnsw) | 13.5 / 16.8 | 13.4 / 15.9 | 65.1 / 95.3 | 25.8 / 37.9 | 11.6 / 13.2 |
| episode_bm25(gin) | 1.2 / 5.1 | 2.7 / 7.9 | 1.5 / 16.4 | 3.5 / 28.1 | 1.1 / 2.4 |
| score+collapse(py) | 0.9 / 1.8 | 0.7 / 1.9 | 0.7 / 0.7 | 0.8 / 1.1 | 0.7 / 1.7 |
| **graph_expand(sql)** (new) | **12.8 / 24.5** | **20.1 / 29.8** | 15.5 / 39.6 | **173.9 / 243.0** | – |
| stale_hints(sql) | 1.2 / 2.9 | 1.3 / 3.0 | 1.7 / 5.5 | 0.0 / 5.4 | 1.3 / 4.1 |
| snapshot+touch(sql) | 2.0 / 3.0 | 2.1 / 3.0 | 1.8 / 3.1 | 2.0 / 4.0 | 2.1 / 5.6 |

Graph expansion with **zero edges in the store** still costs 13–20 ms p50 / 25–30 ms p95 per recall (a seed-subject lookup, the recursive
CTE over an empty `edge` table, and one `_subject_facts` query per distinct seed subject, up to 10). That is the whole DB-only regression.
The vector legs themselves are flat or better than run 1 (more `bench-real` rows now — 949 facts after the run's own writes — so the
planner still chooses the btree+sort exact kNN for this user, 20 ms at ef_search=64).

#### Concurrency sweep — `/recall` only, 60 s each, 150 k (run 1 in the first block)

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| run 1: recall × 1 (NAS TEI) | 279 | 214.0 | 250.6 | 285.6 | 306.8 | 4.64 | 0 |
| run 1: recall × 4 | 377 | 638.5 | 700.9 | 807.4 | 884.5 | 6.23 | 0 |
| run 1: recall × 8 | 375 | 1290.6 | 1406.0 | 1542.2 | 2178.5 | 6.13 | 0 |
| run 1: recall × 16 | 386 | 2557.1 | 2712.6 | 2951.2 | 3355.0 | 6.21 | 0 |
| **rerank on, warm caches** × 1 | 701 | 85.1 | 113.9 | 139.1 | 178.9 | 11.68 | 0 |
| rerank on, warm × 4 | 1220 | 195.4 | 250.4 | 291.9 | 337.2 | 20.31 | 0 |
| rerank on, warm × 8 | 1222 | 389.1 | **500.2** | 548.9 | 618.4 | 20.3 | 0 |
| rerank on, warm × 16 | 1224 | 786.8 | 928.7 | 991.9 | 1117.9 | 20.22 | 0 |
| **rerank off, warm caches** × 1 | 708 | 84.2 | 109.1 | 138.2 | 165.2 | 11.79 | 0 |
| rerank off, warm × 4 | 1253 | 189.9 | 241.4 | 293.3 | 336.9 | 20.87 | 0 |
| rerank off, warm × 8 | 1258 | 378.6 | **492.7** | 546.2 | 592.7 | 20.9 | 0 |
| rerank off, warm × 16 | 1253 | 769.0 | 909.1 | 962.1 | 1040.5 | 20.69 | 0 |
| **rerank on, fresh queries** × 1 | 178 | 314.7 | 693.0 | 717.1 | 728.4 | 2.95 | 0 |
| rerank on, fresh × 4 | 404 | 563.8 | 953.7 | 1074.2 | 1188.4 | 6.69 | 0 |
| rerank on, fresh × 8 | 445 | 1007.8 | **1790.1** | 1971.4 | 2168.7 | 7.38 | 0 |
| **rerank off, fresh queries** × 1 | 317 | 166.8 | 443.9 | 563.3 | 603.3 | 5.27 | 0 |
| rerank off, fresh × 4 | 780 | 275.7 | 646.8 | 798.7 | 1172.3 | 12.91 | 0 |
| rerank off, fresh × 8 | 713 | 683.5 | **1120.7** | 1161.8 | 1188.1 | 11.85 | 0 |

Peaks (warm sweeps): `astoria` **109–146 % CPU** from 4 clients up (one uvicorn worker + threadpool, GIL-bound), `astoria-postgres` 92–121 %,
`memoryos-tei` idle (1 %). Fresh sweeps: `astoria` 37–129 %, PG 14–78 % — the time goes to the workstation embed/rerank endpoints
(22 embeds/s, 12 reranks/s). No OOM, 0 restarts.

Reading: warm, rerank on and off are indistinguishable (the `(query, hook)` cache makes a repeated prompt's rerank free) and the ceiling is
**~20 recalls/s = the service process**, not the store (DB-only below: 33 recalls/s on 8 connections) and not the embedder. Cold, the
rerank stage adds ~0.3–0.7 s at 8 clients on top of the embed queueing.

#### DB-only concurrency — the store alone (in-container, pre-embedded, rerank stage off), 20 s each

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| run 1 real user × 8 (relaxed_order) | 866 | 183.9 | 238.7 | 274.2 | 306.7 | 43.1 | 0 |
| real user × 1 | 238 | 87.8 | 116.5 | 130.4 | 173.1 | 11.85 | 0 |
| real user × 4 | 556 | 142.1 | 187.6 | 210.7 | 234.6 | 27.67 | 0 |
| real user × 8 | 670 | 237.0 | **296.7** | 327.1 | 531.0 | **33.27** | 0 |
| real user × 16 | 662 | 479.3 | 590.4 | 635.1 | 707.3 | 32.66 | 0 |
| bulk 4 %-share user × 1 (random vectors) | 86 | 229.5 | 292.5 | 319.7 | 323.3 | 4.29 | 0 |
| bulk user × 4 | 236 | 335.6 | 410.4 | 442.1 | 481.4 | 11.66 | 0 |
| bulk user × 8 | 253 | 620.0 | 842.7 | 1030.5 | 1143.3 | 12.42 | 0 |
| bulk user × 16 | 258 | 1264.8 | 1705.4 | 1834.7 | 1900.7 | 12.44 | 0 |

Peaks: PG 246 % (real user) / 592 % (bulk user), 814–824 MiB of 1 GiB; `astoria` (hosting the probe) 111–118 %. The store's own ceiling
dropped from 44.6 to 33.3 recalls/s (8 conns) — again the extra graph-expansion queries per recall, not the vector legs.

#### Mixed load — 8 recall + 4 capture + 2 POST /facts clients, 60 s, 150 k

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| run 1: recall × 8 | 163 | 3096.8 | 3564.0 | 3678.1 | 3711.6 | 2.59 | 0 |
| run 1: capture × 4 | 92 | 2748.1 | 3236.3 | 3350.1 | 3364.5 | 1.47 | 0 |
| run 1: facts × 2 | 44 | 2864.1 | 3217.1 | 3453.2 | 3523.9 | 0.71 | 0 |
| **rerank on**: recall × 8 | 859 | 465.3 | 722.8 | 3463.3 | **33677.0** | 9.96 | **3** |
| rerank on: capture × 4 | 1127 | 183.4 | 306.0 | 746.5 | **30022.0** | 13.3 | **4** |
| rerank on: facts × 2 | 455 | 232.2 | 368.2 | 831.3 | **30008.9** | 5.37 | **2** |
| **rerank off**: recall × 8 | 793 | 402.8 | 594.7 | 2168.7 | 31701.4 | 11.99 | 0 |
| rerank off: capture × 4 | 990 | 175.4 | 334.8 | 416.6 | 18618.9 | 14.97 | 0 |
| rerank off: facts × 2 | 415 | 219.1 | 360.3 | 448.6 | 18622.9 | 6.28 | 0 |

Peaks: `astoria` 115–143 %, PG 108–125 %, 654 MiB. Throughput is 4–10× run 1 and the medians are where the single-client numbers predict,
but the tails are broken: the 9 errors are HTTP 500s — `psycopg.errors.DeadlockDetected` ×2 (`UPDATE fact SET access_count=…,last_seen=now() WHERE id = ANY($1)`
from recall vs `UPDATE fact SET embedding=$1 WHERE id=$2 AND embedding IS NULL` from the backfill, PG log 22:47:59 and 22:48:00) and
`psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec` ×7 (requests logged at exactly 30 001 ms). The 18–34 s maxima in both
runs are the same waits that did not hit the 30 s pool timeout. The `worker` control phase shows the same stall once with only 4 recall clients
(max 25 888 ms) while the backlog from the mixed load was still draining.

#### Belief-axis versioning — 20 concurrent and 50 sequential `/correct`, `/history`, `/as_of`

| case | n | p50 ms | p95 ms | p99 ms | max ms | errors |
|---|---|---|---|---|---|---|
| `POST /correct` × 20 parallel, one key (run 1: 2 153 / 4 130) | 20 | 217.2 | 435.7 | 453.0 | 457.3 | 0 |
| `POST /correct` × 50 sequential, one key | 50 | 13.3 | 54.0 | 56.9 | 57.6 | 0 |
| `GET /history` on that API-built chain (len 50, active 1) | 20 | 12.6 | 18.5 | 18.6 | 18.6 | 0 |
| `POST /as_of` scoped on it | 5 | 4.5 | 4.7 | 4.7 | 4.7 | 0 |
| `POST /as_of` scoped + `as_believed_at` | 5 | 5.8 | 6.6 | 6.7 | 6.8 | 0 |
| `GET /history` on the COPY-seeded 50-chain (`bench-u00`; run 1: 22.2 / 37.8) | 20 | 24.8 | 42.4 | 45.5 | 46.3 | 0 |
| `POST /as_of` scoped (seeded chain; run 1: 5.9 / 6.5) | 5 | 3.9 | 4.8 | 4.9 | 5.0 | 0 |
| `POST /as_of` unscoped, whole user, limit 50 (run 1: 23.8 / 58.8) | 5 | 26.8 | 43.7 | 46.8 | 47.5 | 0 |

Row accounting for the 50-correct key: **99 rows in `fact`** = 1 active + 49 `superseded` belief-closed originals (`meta.belief_closed_by`)
+ 49 `superseded` versioned copies (`meta.version_of`) → **1.98 rows per `/correct`** (was 1.0). `/history` hides the belief-closed
originals, so the chain reads as 50; `/as_of` with and without `as_believed_at` stays 4–7 ms on `fact_key`. After 20 concurrent corrects:
API 1 active, DB 1 active, history 20 — **PASS**, and 9.5× faster than run 1 because the embed now happens before the per-key lock (and
is async).

#### Worker interference — recall × 4 while cognify drains 100 turns (150 k)

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| recall × 4, worker idle (control; run 1: 649.9 / 841.2) | 806 | 189.5 | 263.7 | 334.1 | 25887.6 | 13.41 | 0 |
| capture cognify=true × 100 (enqueue; run 1: 251.7 / 377.2) | 100 | 7.4 | 10.2 | 11.5 | 12.2 |  | 0 |
| recall × 4, worker draining (run 1: 635.4 / 753.4) | 1204 | 192.1 | 273.0 | 334.8 | 940.5 | 20.03 | 0 |

Queue 100 → 96 pending in 60 s (cloud LLM, 4 jobs/min, as in run 1); p95 +9 ms while draining — the cognify worker still does not
interfere. The 25.9 s outlier in the *control* is the backfill-transaction lock wait described above (PG at 210 % draining ~900 rows), not
cognify.

#### EXPLAIN (ANALYZE, BUFFERS) summaries, 150 k

| query | real-embedding user (scan) | exec ms | buffers | 4 %-share bulk user (scan) | exec ms | buffers |
|---|---|---|---|---|---|---|
| fact vector (ef_search=64, iterative_scan=off) | btree+sort (`fact_user_status`) | 27.4 | hit 7 620 | HNSW `fact_vec` | 6.1 (rows=0) | hit 906 read 4 |
| fact vector (ef_search=64, relaxed_order) | btree+sort | 20.4 | hit 7 559 | HNSW | **127.6** | hit 15 334 read 10 395 |
| fact vector (ef_search=40 default) | btree+sort | 9.5 | hit 7 559 | HNSW | 1.4 (rows=0) | hit 716 |
| fact BM25 (GIN) | BitmapAnd(user_status, tsv) | 1.7 | hit 126 | GIN | 1.8 | hit 141 read 10 |
| episode vector (ef_search=64) | bitmap `episode_user_time` + sort | 7.3 | hit 1 979 | HNSW `episode_vec` | 1.1 | hit 569 |
| episode BM25 (GIN) | GIN | 1.3 | hit 164 | GIN | 0.4 | hit 16 read 19 |
| history (fact_key) | fact_key | 0.03 | hit 3 | fact_key | 0.05 | hit 1 read 2 |
| as_of unscoped (limit 50) | fact_key | 0.5 | hit 128 | fact_key | 0.3 | hit 75 |
| stale_hints (1 key) | BitmapAnd(user_time, tsv) | 0.5 | hit 57 | GIN | 0.3 | hit 25 |

Same plan shapes as run 1 (full plans in the JSONL). The relaxed-order probe for the bulk user is the cold-cache case again (10 395 page
reads, 128 ms); warm it is 104 ms p50 / 116 ms p95 for 40/40 candidates (`filter` phase; 125 / 250 in run 1).

#### Deep load, sizes, cleanup

| users | facts | facts/s | episodes | episodes/s | VACUUM ANALYZE s |
|---|---|---|---|---|---|
| 20 (resumed part after the mid-load restart: users 4–19) | 80 006 | 104 | 40 000 | 118 | 2.5 |
| combined with users 0–3 | 100 012 | **109** | 50 000 | **122** | – |

Sizes after the run (db total 1 371 MB; run 1 1 320 MB): `fact` 101 546 rows / 887 MB (heap 54, TOAST 401, `fact_vec` 397 MB) = **8.7 KB/row**;
`episode` 51 488 / 463 MB (`episode_vec` 201 MB) = 9.0 KB/row; the new `fact_supersedes` / `fact_superseded_by` partial indexes are
480 / 424 kB; `snapshot` grew to 20 748 rows / 9 MB from ~22 000 recalls (one row each — prune_snapshots at 90 days keeps it bounded).
HOT update ratio after the run: `fact` **96.0 %** (323 274 / 336 640 — fillfactor 90 holds under 22 000 recalls' touch-updates);
`episode` 21.6 % (the backfill's 3 KB vector UPDATEs go to TOAST and are not HOT by nature; autovacuum ran 27× on `episode`, 72× on
`fact` at the 0.02 scale factor — no bloat: 50–263 dead tuples at the end).

Cleanup (`seed.py --wipe`, chain indexes in place, 003 tables included): **101 348 facts + 51 454 episodes + 1 989 edges + 19 953 snapshots
+ 3 841 audit + 229 tombstones + 100 queue rows deleted in 11.7 s** (run 1: 7.1 + 5.4 s for fact + episode alone); all `bench-*` counts
0 afterwards across `fact, episode, edge, entity, alias, snapshot, audit, cognify_queue, tombstone, profile, profile_history` and the
30 bench predicates; the live user's rows untouched (200 fact rows / 140 active, 35 episodes before and after).

### 7.3 Verdict vs thresholds (report.py, run 2 JSONL)

| threshold | measured | result |
|---|---|---|
| recall p95 < 500 ms e2e, 8 concurrent clients, at scale (service default = rerank on, warm caches) | 500.2 | FAIL (by 0.2 ms) |
|   recall p95 < 500 ms e2e, 8 concurrent clients [rerank off, warm] | 492.7 | PASS |
|   (info) same, rerank on, fresh queries (caches cold) | 1790.1 | short |
|   (info) same, rerank off, fresh queries | 1120.7 | short |
|   (info) recall p95 e2e, 1 client, rerank on (cold rerank cache) / off | 516.6 / 102.5 | short / ok |
| DB-only recall p95 < 80 ms at scale (deployed code, relaxed_order) | 90.2 | FAIL |
| capture p95 < 400 ms (1 client, at scale) | 8.0 | PASS |
|   (info) capture p95 under mixed load | 306.0 (rerank on) / 334.8 (off) | ok |
| zero HTTP errors across all phases | 9 | FAIL |
| no container OOM kill | 0 | PASS |
| exactly 1 active under 20 concurrent /correct | True | PASS |
|   (info) async embed: every new row embedded within 60 s | 24.1 s idle; > 121 s with a write backlog | ok / short |
|   (info) DB-only recall p95, 8 parallel connections, real user / 4 %-share user | 296.7 / 842.7 | ok / short |
|   (info) HNSW candidates of 40 for the 4 %-share user, deployed code | 40.0 | ok |

### 7.4 Defects / regressions found by run 2

| id | defect | evidence | fix (not applied in this pass) |
|---|---|---|---|
| **A** | **Graph expansion runs on every recall even when the user has no edges**, costing 13–20 ms p50 / 25–30 ms p95 (seed-subject lookup + recursive CTE over an empty `edge` + up to 10 `_subject_facts` queries); with 2 000 edges it is 174 / 243 ms and dominates recall | DB-only step table; DB-only p95 56 → 90 ms; 8-conn store ceiling 44.6 → 33.3 recalls/s | skip the stage when `NOT EXISTS (SELECT 1 FROM edge WHERE user_id=%s AND status='active')` (one indexed probe, or a per-user flag cached for the worker tick); fold the per-subject fan-out into one `= ANY(subjects)` query; cap `_subject_facts` to the top-3 seed subjects; consider `graph_max_depth=1` as the recall default and depth 2 for `/graph` |
| **B** | **`embed_backfill` is one transaction per tick** (≤200 facts + 200 episodes, embeds inside the txn): under load the txn lasts ~30 s and holds row locks that recall's touch-UPDATE waits on → 18–34 s stalls, 2 deadlocks, 7 `PoolTimeout` 500s in the mixed load, one 25.9 s stall with 4 clients | PG log deadlocks (recall touch-UPDATE vs backfill UPDATE), service tracebacks, backfill ticks 60 s apart, mixed-load maxima 30 001 ms | commit per batch of 8 (embed outside the txn, then a ≤10 ms UPDATE txn); order both UPDATE sets by `id` (no deadlock); make the touch-update non-blocking (`… WHERE id IN (SELECT id FROM fact WHERE id = ANY(%s) FOR UPDATE SKIP LOCKED)`) or move it off the request path (batch touches in the worker); raise pool max to ≥ 16 and set `getconn` timeout < the client timeout |
| **C** | **Backfill throughput ≈ 4–7 rows/s sustained** (200 + 200 rows per 30 s tick, sequential batches of 8 to the embedder, HNSW insert per row) — any burst above that grows the BM25-only window far past the 30 s design intent (episodes still un-embedded 121 s after a 60 s mixed load) | `embed-gap` 150 k; backfill log; real seed 500 rows = 100 s | embed batches concurrently (the workstation seat does 22/s at 8-way), raise the per-tick limit when `pending` is high, and let the tick loop re-run immediately while `pending > 0` instead of sleeping 30 s; expose `pending_facts/episodes` in `/health` so clients can see the gap |
| **D** | **Single uvicorn worker is the warm-path ceiling** (~20 recalls/s at 110–146 % CPU; 8 clients p95 ≈ 500 ms on the line) | concurrency sweeps, `astoria` CPU peaks; DB-only store does 33/s and the embedder 22/s | 2 uvicorn workers (the worker loop is already leader-elected by advisory lock 43) or `--workers` with the pool per process; send the 768-d query vector once per transaction (it is serialised twice per vector query today — 15 KB text each — `fact_vec`+`episode_vec` = 4 dumps per recall; pass it as a CTE param or binary); skip `_stale_hints`/touch for `facts_only` |
| **E** | Rerank p99/max outliers: 1.7 s / 3.2 s at pre-load, 0.7 s at 150 k single-client (cold `(query, hook)` cache; workstation endpoint shared with a busy host); NAS fallback is 3× slower and caps at 2.9 calls/s | `rerank-floor`, `recall` rerank-on rows | keep `rerank_timeout_s` ≤ 1 s on the read path (base ranking is good: hit-rate 1.0 either way here); rerank only when the candidate pool has > N items or when the top-2 scores are close; pin the reranker to a GPU seat if the workstation is on anyway |
| **F** (harness) | a `BENCH_DSN` export silently moved the DB-only probe onto the ssh tunnel (375 ms "DB-only" recalls that were 70 ms) | first pre-load pass, discarded | `loadgen.py` now always uses the in-container exec path unless `BENCH_DB_EXEC=""` is set explicitly; README updated |

### 7.5 Growth projection update (1 M facts / 500 k episodes)

Per-row costs are unchanged (8.7 KB/fact, 9.0 KB/episode incl. HNSW; 109 / 122 rows/s in-place HNSW insert at 100 k), so the §4 size
and cache-knee projections stand: **≈ 13 GB at 1 M + 500 k, HNSW ≈ 5.9 GB, and the 1 GiB PG cgroup stops holding the hot graph at
~170–200 k embedded rows** — the mitigation list in §4 (PG `mem_limit` 2–3 GiB + `shared_buffers` 768 MB–1 GB, `halfvec` HNSW, stop
embedding raw turns) is still the order of business before the store passes ~150 k. What this run changes in the projection:

| metric | run 1 basis | run 2 | projection note |
|---|---|---|---|
| rows per correction | 1 | **1.98** (belief-axis copy) | a user correcting 50 keys/day adds ~100 rows/day ≈ 0.9 MB/day with vectors — negligible vs episodes, but the versioned copies carry a full 3 KB embedding each; consider `embedding=NULL` on versioned copies (they are never recalled: `status='superseded'`) to halve that |
| write path | 234–289 ms, TEI-bound | 6–11 ms, store-bound | unchanged by store size (HNSW insert ≈ 5–8 ms is now inside the backfill, not the request); the **backfill rate (≈ 4–7 rows/s, defect C) is the real write ceiling** — 1 M facts of organic growth is fine (≤ 0.1 rows/s), a bulk import is not: use `seed.py --index-mode rebuild` or a one-off backfill with concurrency |
| recall DB-only | 49 / 56 ms | 71 / 90 ms (graph +20 ms) | HNSW cost still ~log N (×1.15 to 1 M); graph expansion is O(edges touched), not O(rows) — with real edges it is the dominant term (174 ms at 2 000 edges on one user) and must be gated (defect A) |
| recall e2e ceiling | 6.2 req/s (NAS TEI) | ~20 req/s warm (service CPU) / 7–12 req/s cold (workstation seat + reranker) | independent of store size; the next step is process count (defect D) and keeping the workstation endpoints on |
| `snapshot` | 770 rows | 20 748 rows / 9 MB for 22 k recalls | 1 row per recall: at 10 k recalls/day that is 0.4 MB/day — `prune_snapshots(90 d)` bounds it at ~40 MB |

### 7.6 Risks & mitigations (updated)

1. **Reranker cost: NAS CPU vs workstation.** Workstation: 116 ms / 30 hooks, ~12 calls/s; NAS: 324 ms, ~2.9 calls/s. With the workstation
   off, every cold recall pays +0.3 s and the stage alone caps recall below 3 req/s; with it on, a cold 8-client burst still queues
   (1.8 s p95). The cache makes repeated prompts free (warm: rerank on = rerank off). Mitigations: short read-path timeout (≤ 1 s, degrade to
   base ranking), rerank only when it can change the answer (pool > N, close top scores), keep hooks capped at 240 chars (done), and expect
   the NAS reranker to be a correctness fallback, not a capacity tier.
2. **Async-embed recall gap.** Design intent ≤ 30 s; measured 24 s idle, 37 s at 150 k, **> 121 s after a 60 s burst** because the backfill
   drains at ~4–7 rows/s in 30 s transactions. Until defect B/C land: keep `sync:true` for the few writes that must be recallable
   immediately (explicit `/remember`, `/correct` — the detector path), and read `pending_facts/episodes` from the backfill log (or expose it
   in `/health`) to alarm on a growing backlog. The same transaction shape is what produces the lock waits, deadlocks and pool timeouts —
   fixing B fixes both.
3. **Belief-axis row growth.** 2 rows per supersede (was 1); `/history`/`/as_of` stay on `fact_key` (4–25 ms) because the belief-closed
   originals are filtered by `meta ? 'belief_closed_by'` in SQL. Growth is proportional to corrections, not to recalls; the only cost worth
   pre-empting is the duplicated 3 KB vector on the versioned copy (never searched — drop it) and the two extra HNSW inserts per correction
   in the backfill.
4. **Graph expansion is unbounded by store size but bounded by edge density** — and today it is paid with zero edges. Gate it (defect A)
   before edges start landing from cognify; with real edges budget ~1–3 ms per edge touched at fanout 20 / depth 2.
5. **Service process ceiling (~20 recalls/s).** One uvicorn worker, Python-side vector serialisation, and the recall txn holding a pool
   connection across the embed + rerank HTTP calls. Two workers and a 16-connection pool roughly double the warm ceiling; keeping external
   calls outside the DB transaction shortens connection hold time and removes the 30 s pool-timeout failure mode.
6. Still open from run 1: the HNSW working set vs the 1 GiB PG cgroup on spinning disks (cold probes at 0.13–1.4 s with 10 k page reads are
   still visible in the bulk-user EXPLAIN), and filtered-HNSW cost for minority users (relaxed_order fixes correctness; 4 %-share user DB-only
   187 / 219 ms, 8 conns 843 ms p95 — partial per-user HNSW indexes or partitioning when several heavy users share the store).

### 7.7 Harness changes in this pass

`loadgen.py`: global `--rerank on|off` (adds the request flag to every `/recall`) and `--unique` (nonce per query → cold caches); new
phases `embed-floor`, `rerank-floor`, `embed-gap`, `correct-seq`; `baseline` also measures `sync:true` writes; the in-container probe is the
unconditional default (`BENCH_DB_EXEC` must be set to `""` explicitly to run locally). `db_probe.py`: DB-only recall passes `rerank=False`
(store-only numbers) and the step breakdown includes `graph_expand(sql)`. `seed.py`: `--edges N --edges-user U` seeds random active edges;
`--wipe` also clears `edge`/`entity`/`alias`. `report.py`: renders the new phases, a verdict row per concurrency label (rerank on/off both
hard, fresh-query sweeps informational), and the async-embed window.


## How to re-run

See `scripts/bench/README.md` (environment variables, the exact command sequence, and the thresholds encoded in
`scripts/bench/report.py`). Raw records: run 1 `scripts/bench/results/2026-08-22.jsonl`, run 2
`scripts/bench/results/2026-08-22-run2.jsonl` (`report.py <file>` reproduces the tables of either run); the rerank
evaluation is `scripts/bench/rerank_eval.py --mode exec --json out.json`.

### 7.8 Fixes applied after run 2 (deployed; verified by the acceptance + smoke suites, re-measure on the next run)
- **A — graph expansion gate:** recall checks (60 s per-user cache) whether the user has any active edges before
  running the expansion query; users without a graph no longer pay the +13–20 ms.
- **B/C — embedding backfill:** rewritten as short transactions (≤8 rows each, `FOR UPDATE SKIP LOCKED`,
  committed per batch, 20 s time budget, up to 1000 rows per table per tick) — recall's touch-updates no longer
  wait on a long backfill transaction, and the post-burst un-embedded window shrinks to about one tick.
- **D — service concurrency:** the container now runs two uvicorn workers (the cognify/curator loop still runs in
  exactly one process via the leader advisory lock); raises the warm ceiling above ~20 recalls/s.
