# Astoria — performance & scale validation (2026-08-22)

> Rick's requirement: *"Load it deep, hit it hard and make sure that it doesn't become a sluggish
> layer that drags novacore down."* This document is the hard numbers + verdict. The harness that
> produced it lives in `scripts/bench/` (README there) and is re-runnable against the live NAS.

## 0. Verdict (short)

**The store does not collapse under its own weight — the NAS TEI does.** At 150 k embedded rows (100 k facts + 50 k episodes, 20 users)
the Postgres/pgvector side of a recall costs **49 ms p50 / 68 ms p95** (DB-only, real-embedding user), BM25 and HNSW plans stay index-backed,
the store alone sustains **~44 recalls/s with 8 parallel connections (p95 232 ms)** and 16 connections (p95 478 ms) with zero errors, 20
concurrent `/correct` on one key leave exactly one active row, and nothing OOM-killed. Single-client end-to-end `/recall` is **205 ms p50 /
225–313 ms p95** — of which ~170 ms is the TEI query embed — and did not move between 500 rows and 150 k rows.

What fails the contract is the **TEI-inclusive concurrent numbers**: the CPU nomic container tops out at **~6.5 embeds/s**, so 8 concurrent
recall clients see **p95 1 406 ms** (PG at 12–23 % CPU, TEI at 400 %), and the mixed load (14 clients) puts every call at ~3 s. That is a fixed,
store-size-independent ceiling; it is the first thing that will "drag novacore down", and it is not fixable inside Postgres.

| contract threshold | measured @150 k | result |
|---|---|---|
| recall p95 < 500 ms e2e, **8 concurrent clients** (TEI-inclusive) | **1 406 ms** (1 client: 313 ms; DB-only 8 conns: 232 ms) | **FAIL — TEI-bound** |
| DB-only recall p95 < 80 ms | **67.7 ms** (55.8 ms with the iterative-scan fix) | PASS |
| capture p95 < 400 ms | **270 ms** single client (3 236 ms under the 14-client mixed load — TEI-bound) | PASS (single) / FAIL (mixed) |
| zero errors | 0 errors over ~6 000 HTTP requests + ~8 000 DB-only recalls | PASS |
| no OOM | `OOMKilled=false`, 0 restarts on both containers (PG peaked at 1 009 MiB of 1 GiB in the worst-case DB-only hammer) | PASS |
| exactly 1 active under 20 concurrent `/correct` | 1 (API and DB), history 20, 20×200 | PASS |

**One real defect found and fixed in code:** with pgvector's default `hnsw.iterative_scan=off`, a user holding a small share of the shared
HNSW index gets almost no vector candidates back (**mean 1.9 of 40** for a 4 %-share user; semantic recall silently degrades to BM25). `recall.py`
now sets `hnsw.iterative_scan = relaxed_order` alongside `ef_search` (→ 40/40; free for the dominant user; 204/269 ms DB-only for the 4 %-share
user). The deployed container still runs the old code — the e2e numbers in this doc are pre-fix; the DB-only numbers were measured with the
setting applied at session level, which is exactly what the patched code does per transaction. `tests/test_recall.py` passes (10/10) against the
patched module.

**Scale knee:** not 1 M rows — ~200 k. HNSW + TOAST cost 7.9 KB per embedded row and the PG cgroup's usable cache is ~650 MB, so past ~170 k
rows vector probes start hitting the HDD RAID1 that holds `pgdata` (a cold probe measured 1.39 s with 12 k page reads). Raise the PG limits
and/or halve the vector footprint before then (section 4).


## 1. Environment (as measured)

| component | detail |
|---|---|
| service | `astoria` container, 768 MiB limit, 1 uvicorn worker (sync FastAPI routes → anyio threadpool), psycopg pool max 8, in-process cognify worker |
| store | `astoria-postgres` — Postgres 18.6 + pgvector 0.8.6, **1 GiB cgroup limit**, `shared_buffers=256MB`, `work_mem=4MB`, `maintenance_work_mem=128MB`, `effective_cache_size=4GB` (over-stated vs the 1 GiB cgroup), no CPU cap |
| HNSW | `fact_vec` / `episode_vec` = `hnsw(embedding vector_cosine_ops)` with defaults **m=16, ef_construction=64**; recall sets `SET LOCAL hnsw.ef_search=64`; `hnsw.iterative_scan=off` (server default), `hnsw.max_scan_tuples=20000` |
| embeddings | TEI nomic-embed-text-v1.5 on the NAS CPU (`memoryos-tei`, 3 GiB limit, float32, max_batch_requests 4, max_client_batch_size 8) |
| host | UGREEN DXP4800GT, 8 cores, 7.2 GiB RAM (~3.5 GiB available, 1.7 GiB swap in use before the run), Home Assistant / MegaPlan / jellyfin also resident |
| client | specul8-o-matic over LAN; `POST` via httpx; DB-only probes executed *inside* the `astoria` container (see README: the ssh tunnel adds 40–100 ms stalls to vector-parameter queries, so it is never on the DB-only timing path) |
| data | 20 bench users × 5 000 facts + 2 500 episodes via direct COPY (random unit vectors, 60 predicates mixed functional/set, ~200 entity subjects, 3-year `asserted_at` spread, ~8 % superseded chains + 2 % retracted, 10 % beliefs; episodes 70 % turns in sessions of 8 / 20 % summaries / 10 % notes) **+ one real-embedding user `bench-real` with 500 realistic triples written through `POST /facts`** (true TEI vectors) for semantic-quality probes; live user `rick` (14 facts / 2 episodes) untouched |

Method: every number is wall-clock from the client unless marked DB-only; DB-only = the service's own
`recall()` code run in the container with the query vector pre-embedded (so TEI is excluded). p50/p95/p99
over the stated n. Concurrency phases run N threads each with its own HTTP client for 60 s.

## 2. Results — every table (generated by `scripts/bench/report.py scripts/bench/results/2026-08-22.jsonl`)

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

- 1 clients peak: astoria: cpu 25% mem 115 MiB/768MiB; astoria-postgres: cpu 12% mem 697 MiB/1GiB; memoryos-tei: cpu 387% mem 480 MiB/3GiB
- 4 clients peak: astoria: cpu 56% mem 116 MiB/768MiB; astoria-postgres: cpu 23% mem 698 MiB/1GiB; memoryos-tei: cpu 414% mem 482 MiB/3GiB
- 8 clients peak: astoria: cpu 49% mem 123 MiB/768MiB; astoria-postgres: cpu 118% mem 705 MiB/1GiB; memoryos-tei: cpu 412% mem 487 MiB/3GiB
- 16 clients peak: astoria: cpu 79% mem 142 MiB/768MiB; astoria-postgres: cpu 21% mem 690 MiB/1GiB; memoryos-tei: cpu 412% mem 486 MiB/3GiB
- OOM check: `/astoria OOMKilled=false restarts=0
/astoria-postgres OOMKilled=false restarts=0`

#### DB-only concurrency — the store alone (in-container, pre-embedded queries, TEI excluded), 20 s each [150k real-user scan=off] — user `bench-real`, iterative_scan=off, random_vectors=False

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| DB-only recall × 1 connections | 397 | 54.3 | 72.2 | 78.3 | 103.6 | 19.79 | 0 |
| DB-only recall × 4 connections | 776 | 103.5 | 130.4 | 146.1 | 169.4 | 38.62 | 0 |
| DB-only recall × 8 connections | 895 | 178.0 | 232.2 | 253.7 | 290.2 | 44.57 | 0 |
| DB-only recall × 16 connections | 873 | 363.5 | 478.3 | 527.1 | 586.4 | 43.24 | 0 |

- peak: astoria: cpu 144% mem 220 MiB/768MiB; astoria-postgres: cpu 171% mem 876 MiB/1GiB; memoryos-tei: cpu 1% mem 480 MiB/3GiB

#### DB-only concurrency — the store alone (in-container, pre-embedded queries, TEI excluded), 20 s each [150k real-user scan=relaxed_order (fix)] — user `bench-real`, iterative_scan=relaxed_order, random_vectors=False

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| DB-only recall × 1 connections | 377 | 57.6 | 73.0 | 81.2 | 179.8 | 18.82 | 0 |
| DB-only recall × 4 connections | 757 | 105.0 | 134.4 | 144.9 | 175.5 | 37.81 | 0 |
| DB-only recall × 8 connections | 866 | 183.9 | 238.7 | 274.2 | 306.7 | 43.1 | 0 |
| DB-only recall × 16 connections | 867 | 363.6 | 491.6 | 544.4 | 583.2 | 43.0 | 0 |

- peak: astoria: cpu 115% mem 221 MiB/768MiB; astoria-postgres: cpu 162% mem 804 MiB/1GiB; memoryos-tei: cpu 1% mem 480 MiB/3GiB

#### DB-only concurrency — the store alone (in-container, pre-embedded queries, TEI excluded), 20 s each [150k bulk-user 4% share scan=relaxed_order (fix, worst case)] — user `bench-u05`, iterative_scan=relaxed_order, random_vectors=True

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| DB-only recall × 1 connections | 90 | 201.0 | 326.5 | 515.1 | 1028.8 | 4.5 | 0 |
| DB-only recall × 4 connections | 261 | 304.2 | 366.6 | 432.2 | 456.0 | 12.84 | 0 |
| DB-only recall × 8 connections | 330 | 482.8 | 584.4 | 637.7 | 723.4 | 16.25 | 0 |
| DB-only recall × 16 connections | 307 | 1058.0 | 1373.6 | 1470.9 | 1559.3 | 14.76 | 0 |

- peak: astoria: cpu 78% mem 221 MiB/768MiB; astoria-postgres: cpu 638% mem 1009 MiB/1GiB; memoryos-tei: cpu 8% mem 480 MiB/3GiB

#### Mixed load — 8 recall + 4 capture + 2 POST /facts clients, 60 s [150k]

| case | n | p50 ms | p95 ms | p99 ms | max ms | req/s | errors |
|---|---|---|---|---|---|---|---|
| facts x2 | 44 | 2864.1 | 3217.1 | 3453.2 | 3523.9 | 0.71 | 0 |
| capture x4 | 92 | 2748.1 | 3236.3 | 3350.1 | 3364.5 | 1.47 | 0 |
| recall x8 | 163 | 3096.8 | 3564.0 | 3678.1 | 3711.6 | 2.59 | 0 |

- peak: astoria: cpu 37% mem 146 MiB/768MiB; astoria-postgres: cpu 19% mem 695 MiB/1GiB; memoryos-tei: cpu 411% mem 492 MiB/3GiB
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
- peak idle: astoria: cpu 60% mem 140 MiB/768MiB; astoria-postgres: cpu 30% mem 678 MiB/1GiB; memoryos-tei: cpu 415% mem 482 MiB/3GiB
- peak draining: astoria: cpu 87% mem 141 MiB/768MiB; astoria-postgres: cpu 43% mem 684 MiB/1GiB; memoryos-tei: cpu 415% mem 484 MiB/3GiB

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

#### Cleanup — bench-* rows before → after wipe [150k (run by the orchestrator after adding migration 002)]

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

- wipe took fact 7.06 s + episode 5.4 s (was >17 min and cancelled before the chain indexes) s; db total after: 1330 MB before VACUUM reclaim; fact 871 MB / episode 445 MB still allocated until vacuum/autovacuum returns the space

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
| **Unindexed self-FK on the supersede chain** — `fact.supersedes` / `fact.superseded_by` (`ON DELETE SET NULL`) had no index → a table scan per deleted fact | the first bench wipe (`DELETE FROM fact WHERE user_id LIKE 'bench-%'`, 100 655 rows) ran **17+ min and had to be cancelled**; the same cost hits `DELETE /users/{id}` and `forget mode=hard` (2 seq scans of `fact` per row ≈ 100+ ms each at 150 k) | `astoria/sql/002_chain_indexes.sql` (orchestrator): partial btree indexes on both columns, applied on the NAS | **fact: 100 655 rows deleted in 7.06 s; episode: 50 255 in 5.4 s**; also speeds `history` chain walks |
| **TEI embed inside the per-key advisory lock** (`facts.upsert_fact`) serialised 20 concurrent `/correct` on one key behind 20 sequential embeds | `/correct` × 20: p50 2 153 ms / p95 4 130 ms, all 200, exactly 1 active (correct, just slow) | orchestrator: embedding now happens **before** the lock is taken | lock hold time drops from ~200 ms to the few-ms supersede txn; expect ~TEI-bound ≈ 0.3–3 s total at 20-way, no serialisation |
| **Embedding path** — every recall/capture/facts call paid the NAS CPU TEI (170 ms single, **~6.5 embeds/s ceiling** at 400 % CPU) | concurrency sweep flat at 6.2 req/s from 4 clients; 8 clients p95 1 406 ms; mixed load ~3 s for everything | orchestrator: embeddings now prefer the **workstation nomic seat via SAINT (~64 ms)** over NAS TEI (~280 ms) with automatic fallback | the ~6 req/s wall in this document is the NAS-TEI path; the store itself sustained **44.6 recalls/s** (8 parallel DB-only connections, p95 232 ms) — re-run `concurrency`/`mixed` after deploy to get the new e2e ceiling |

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
| PG cgroup RSS at rest | ~680–720 MiB of 1 GiB (256 MB shared_buffers + page cache of the hot HNSW pages) | **page cache cannot hold the graph: ≥ 5 GB of HNSW vs ≤ 0.75 GB cache** | cgroup v2 charges file pages; they are reclaimable, so the failure mode is *latency*, not OOM |
| in-place HNSW insert | 107 facts/s, 121 episodes/s (PG at 100 % of one core; 202 → 107 rows/s from 0 → 100 k) | ~60–80 rows/s by 1 M (log-ish decay); organic growth is fine (1 M facts = 2–3 h of CPU); a *bulk* import should use `seed.py --index-mode rebuild` (drop → COPY → CREATE INDEX) | measured slope |
| HNSW build memory (rebuild) | 128 MB `maintenance_work_mem` holds ~40 k vectors in-memory | 1 M × (3 KB + links) ≈ 3.5 GB needed to build in RAM; otherwise pgvector falls to the slow on-disk phase | pgvector docs; this box cannot give PG 3.5 GB |
| recall DB-only, dominant / well-separated user | p50 49 ms, p95 68 ms (warm) | **≈ 60–80 ms warm** (HNSW cost ~log N: ×1.15); **cold: seconds** — each HNSW probe touches ~1 000–2 000 8 KB pages; `pgdata` is on the HDD RAID1 (`sda/sdb`, rotational), ~150 random IOPS | EXPLAIN buffers (1 551 hits warm; 11 047 hit + 11 985 read cold = 1.39 s) |
| recall DB-only, minority user (≤ 5 % share) with the iterative-scan fix | p50 204 ms, p95 269 ms warm | bounded by `hnsw.max_scan_tuples` (20 000 tuples ≈ 1–1.5 s warm, more cold); falls back to < 40 candidates beyond that | measured bench-u05 |
| write path | capture 234/270 ms, POST /facts 263/289 ms (single client) — ~180 ms of each is the TEI embed | unchanged by store size (HNSW insert ≈ 5–8 ms of it) | measured |

**When does the 1 GiB PG limit / 256 MB shared_buffers need changing?** Now-ish. At 150 k rows the cgroup already sits at 680–720 MiB
because the page cache is holding the 588 MB of HNSW; the usable cache (1 GiB − 256 MB shared_buffers − ~100 MiB backends) is ≈ 650 MB,
so **beyond ~170 k embedded rows the hot graph no longer fits and every vector probe starts reading HDD pages**. The knee is not 1 M, it is
~200 k. Recommendation before the store passes ~150 k embedded rows: `mem_limit` 2–3 GiB and `shared_buffers` 768 MB–1 GB (the NAS has
~3.4 GiB available, but it is already swapping 1.7 GB — so pair this with the footprint reductions below), *or* cut the vector
footprint in half with a `halfvec` HNSW index (`USING hnsw ((embedding::halfvec(768)) halfvec_cosine_ops)`, pgvector ≥ 0.7: 1.9 KB/row), *or* stop
embedding turns (70 % of episodes are `kind='turn'`; archive_old_turns + embed only summaries/notes cuts `episode_vec` by ~70 %).


## 5. Top scaling risks & mitigations

1. **Filtered-HNSW candidate starvation in a multi-user index (correctness, not speed).** pgvector applies `WHERE user_id=… AND status=… AND layer=…`
   *after* pulling `ef_search=64` graph candidates. Measured at 150 k rows: a user with a 4 % share of the index got **mean 1.9 / 40** vector
   candidates (20 of 20 queries short, EXPLAIN `rows=0`) — semantic recall silently collapses to BM25-only for every non-dominant user.
   *Fixed in this pass:* `recall.py` now sets `hnsw.iterative_scan = relaxed_order` next to `ef_search` (pgvector ≥ 0.8): 40/40 candidates,
   DB-only recall for that user 204/269 ms p50/p95 (was 91/144 ms but wrong). For the dominant user (every row passes the filter) the setting is
   free. Longer term, for several heavy users: **one partial HNSW index per heavy user** (`CREATE INDEX fact_vec_rick ON fact USING hnsw(...) WHERE user_id='rick'`
   — the planner picks it for that user's recalls; small, cacheable, exact per-user graph) or **LIST/HASH partitioning by `user_id`** so each
   partition carries its own graph; also keep `hnsw.max_scan_tuples` (default 20 000) as the worst-case bound.
2. **HNSW working set vs the 1 GiB PG cgroup on spinning disks.** 3.9 KB of HNSW + 3.95 KB of TOAST per embedded row; cache ceiling ≈ 650 MB ≈
   170 k rows. Past that, probes go to HDD (cold relaxed-order probe measured at 1.39 s with 12 k page reads). Mitigations (pick two):
   raise PG `mem_limit`/`shared_buffers` (2 GiB / 768 MB), `halfvec` HNSW (halves index), don't embed raw turns (archive_old_turns is already a
   curator job — make it also drop `embedding` on archived turns), and keep `VACUUM (ANALYZE)` honest: autovacuum scale factor 0.2 means
   200 k dead tuples before a vacuum at 1 M rows — set `autovacuum_vacuum_scale_factor=0.02` on `fact`/`episode` (recall's touch-update is
   97.8 % HOT today, so bloat is low, but the margin shrinks as pages fill; `fillfactor=90` on `fact` keeps HOT working).
   **Mass delete / hard-forget / user wipe relies on the chain indexes** (`002_chain_indexes.sql`): without `fact_supersedes` /
   `fact_superseded_by` every deleted fact is two table scans — keep them, and keep any future self-referencing FK indexed.
3. **TEI is the real ceiling, not Postgres.** Every recall, capture and POST /facts spends one TEI embed (170 ms, CPU nomic, **~6.5 embeds/s
   total** on 4 cores). The concurrency sweep flat-lines at 6.2 req/s from 4 clients up with TEI at 400 % CPU and PG at 12–23 %; the 8-client
   e2e p95 (1 406 ms) and the mixed-load capture p95 (3 236 ms) are TEI queueing. Mitigations, in order of payoff: **embed asynchronously on
   the write path** (capture/POST /facts write the row with `embedding NULL`, the existing `embed_backfill` curator fills it — capture drops from
   ~240 ms to ~10 ms and stops competing with recall for TEI); an **LRU query-embedding cache** in `embed.py` (repeated prompts/briefings are free);
   **move embeddings to a GPU seat** (nomic on an R9700 via johnny/SAINT is ~5 ms) or at least a quantized/ONNX TEI with all 8 cores; and keep
   the DB pool (max 8) ≥ the expected recall concurrency — the store itself handled 8–16 parallel DB-only recalls without error (table above).


## How to re-run

See `scripts/bench/README.md` (tunnel, env, the exact command sequence, and the thresholds encoded in
`scripts/bench/report.py`). Raw records for this run: `scripts/bench/results/2026-08-22.jsonl`.
