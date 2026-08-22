# Astoria benchmark harness (`scripts/bench/`)

Performance / scale validation for the live service. Reusable: every phase is a CLI that prints a
markdown table and appends one JSON record to `$BENCH_OUT`; `report.py` turns the JSONL into the
tables + threshold verdict that live in `docs/PERFORMANCE.md`.

Only user_ids with the prefix **`bench-`** are ever written, and `seed.py --wipe` removes them all
(direct SQL, prints before/after counts). User `rick` is never touched.

| file | what |
|---|---|
| `common.py` | env, percentile helpers, the 30 varied queries, the realistic 500-triple "real user" generator, TEI embed helper, `docker stats` sampler (ssh), OOM check |
| `seed.py` | deep loader: direct COPY of N facts + M episodes per `bench-uNN` user with random unit vectors (supersede chains, retracted rows, 50-long chain on `bench-u00`); `--sizes`, `--vacuum`, `--wipe` (all `bench-*` rows incl. `edge`/`entity`/`alias`), `--edges N --edges-user U` (random active edges for the graph-expansion probe) |
| `db_probe.py` | DB-only probe that runs the service's own `recall()` with pre-embedded vectors; shipped into the `astoria` container (`docker exec -i astoria python -`) so the numbers exclude the ssh tunnel; also EXPLAIN and step breakdowns |
| `loadgen.py` | measurement phases: `tei`, `embed-floor`, `rerank-floor`, `baseline`, `real-seed`, `embed-gap`, `recall`, `concurrency`, `db-concurrency`, `filter`, `mixed`, `correct`, `correct-seq`, `chain`, `explain`, `worker`, `wipe-http`; global flags `--rerank on|off` (request flag on every `/recall`) and `--unique` (nonce per query → service caches cold) |
| `report.py` | JSONL → markdown tables + pass/fail verdict (`--json` for CI) |

## Environment

```
ASTORIA_URL     REST base (default http://192.168.1.134:8933)
TEI_URL         TEI base  (default http://192.168.1.134:8931)
BENCH_DSN       Postgres DSN for seed.py / wipe (the NAS PG is loopback-only → ssh tunnel, below)
BENCH_DB_EXEC   command that runs a python script from stdin INSIDE the service env
                (default: "ssh -o BatchMode=yes root-dxp4800gt docker exec -i astoria python -" — always,
                 even when BENCH_DSN is exported; set to "" explicitly to run db_probe locally against
                 BENCH_DSN, and then never read its numbers as DB-only latency: the tunnel adds 40-100 ms)
BENCH_SSH       ssh host for docker stats (default root-dxp4800gt; "" disables)
BENCH_OUT       JSONL file every phase appends to
BENCH_LABEL     tag stored in each record (e.g. pre-load / 150k) — or pass --label
```

NAS Postgres is reachable only on the NAS loopback:

```sh
ssh -N -L 18934:127.0.0.1:8934 root-dxp4800gt &          # kill it when done
PW=$(ssh root-dxp4800gt "grep POSTGRES_PASSWORD /volume1/docker/astoria/.env" | cut -d= -f2-)
export BENCH_DSN="postgresql://astoria:${PW}@127.0.0.1:18934/astoria"
export BENCH_OUT=/tmp/astoria-bench.jsonl
```

## The full run (what produced docs/PERFORMANCE.md)

```sh
cd ~/repos/astoria && PY=.venv/bin/python
$PY scripts/bench/loadgen.py tei                        --label pre-load   # TEI floor
$PY scripts/bench/loadgen.py real-seed bench-real --n 500 --workers 4 --label pre-load  # 500 real-embedding facts via POST /facts
$PY scripts/bench/loadgen.py baseline  bench-real       --label pre-load   # recall/capture/facts at ~0 rows
$PY scripts/bench/loadgen.py recall    bench-real --n 50 --label pre-load
$PY scripts/bench/seed.py --users 20 --facts 5000 --episodes 2500 --vacuum --sizes   # 100k facts + 50k episodes
$PY scripts/bench/loadgen.py recall      bench-real --n 50   --label 150k
$PY scripts/bench/loadgen.py concurrency bench-real --clients 1,4,8,16 --seconds 60 --label 150k
$PY scripts/bench/loadgen.py mixed       bench-real --seconds 60 --label 150k
$PY scripts/bench/loadgen.py correct     bench-real          --label 150k
$PY scripts/bench/loadgen.py chain       bench-u00           --label 150k
$PY scripts/bench/loadgen.py explain     bench-real --chain-user bench-u00 --label 150k
$PY scripts/bench/loadgen.py worker      bench-real --turns 100 --seconds 60 --label 150k
$PY scripts/bench/loadgen.py baseline    bench-real          --label 150k     # single-client recall/capture/facts at scale
$PY scripts/bench/loadgen.py explain     bench-u05 --chain-user bench-u00 --label 150k-bulk-user   # plan shape for a 5k-row user
$PY scripts/bench/loadgen.py filter      bench-u05 --n 20    --label 150k     # filtered-HNSW candidate starvation check
$PY scripts/bench/loadgen.py recall      bench-u05 --n 20 --random-vectors --label 150k-bulk-user
$PY scripts/bench/loadgen.py db-concurrency bench-real --clients 1,4,8,16 --seconds 20 --label "150k real-user scan=off"
$PY scripts/bench/loadgen.py db-concurrency bench-real --clients 1,4,8,16 --seconds 20 --iterative --label "150k real-user scan=relaxed_order (fix)"
$PY scripts/bench/loadgen.py db-concurrency bench-u05  --clients 1,4,8,16 --seconds 20 --iterative --random-vectors --label "150k bulk-user 4% share scan=relaxed_order (fix, worst case)"
$PY scripts/bench/report.py $BENCH_OUT > /tmp/bench-tables.md
$PY scripts/bench/seed.py --wipe                               # removes every bench-* row, prints before/after (record goes to $BENCH_OUT too)
kill %1                                                        # the tunnel
```

## Run 2 (2026-08-22, feature-complete build) — the sequence that produced docs/PERFORMANCE.md §7

```sh
export BENCH_OUT=scripts/bench/results/2026-08-22-run2.jsonl     # BENCH_DSN as above; BENCH_DB_EXEC stays at its default (in-container)
$PY scripts/bench/loadgen.py embed-floor  --label pre-load        # workstation seat vs NAS TEI, 1/4/8-way
$PY scripts/bench/loadgen.py rerank-floor --label pre-load        # workstation vs NAS reranker, 30 hooks/call
$PY scripts/bench/loadgen.py tei          --label pre-load
$PY scripts/bench/loadgen.py real-seed bench-real --n 500 --workers 4 --label pre-load   # async now: wait for embed_backfill (~3 ticks)
$PY scripts/bench/loadgen.py embed-gap bench-real --n 20 --seconds 120 --label pre-load
$PY scripts/bench/loadgen.py baseline  bench-real --label pre-load                       # capture/facts async + sync:true
$PY scripts/bench/loadgen.py recall    bench-real --n 50 --rerank on  --label pre-load
$PY scripts/bench/loadgen.py recall    bench-real --n 50 --rerank off --label "pre-load rerank=off"
$PY scripts/bench/seed.py --users 20 --facts 5000 --episodes 2500 --vacuum --sizes
$PY scripts/bench/loadgen.py recall      bench-real --n 50 --rerank on  --label 150k
$PY scripts/bench/loadgen.py recall      bench-real --n 50 --rerank off --label "150k rerank=off"
$PY scripts/bench/loadgen.py baseline    bench-real --label 150k
$PY scripts/bench/loadgen.py concurrency bench-real --clients 1,4,8,16 --seconds 60 --rerank on  --label "150k rerank=on"
$PY scripts/bench/loadgen.py concurrency bench-real --clients 1,4,8,16 --seconds 60 --rerank off --label "150k rerank=off"
$PY scripts/bench/loadgen.py concurrency bench-real --clients 1,4,8 --seconds 60 --rerank on  --unique --label "150k rerank=on fresh-queries"
$PY scripts/bench/loadgen.py concurrency bench-real --clients 1,4,8 --seconds 60 --rerank off --unique --label "150k rerank=off fresh-queries"
$PY scripts/bench/loadgen.py mixed       bench-real --seconds 60 --rerank on  --label "150k rerank=on"
$PY scripts/bench/loadgen.py mixed       bench-real --seconds 60 --rerank off --label "150k rerank=off"
$PY scripts/bench/loadgen.py correct     bench-real --label 150k
$PY scripts/bench/loadgen.py correct-seq bench-real --n 50 --label 150k                # belief-axis rows per /correct
$PY scripts/bench/loadgen.py chain       bench-u00  --label 150k
$PY scripts/bench/loadgen.py explain     bench-real --chain-user bench-u00 --label 150k
$PY scripts/bench/loadgen.py worker      bench-real --turns 100 --seconds 60 --rerank on --label 150k
$PY scripts/bench/loadgen.py embed-gap   bench-real --n 20 --seconds 120 --label 150k
$PY scripts/bench/loadgen.py explain     bench-u05 --chain-user bench-u00 --label 150k-bulk-user
$PY scripts/bench/loadgen.py filter      bench-u05 --n 20 --label 150k
$PY scripts/bench/loadgen.py recall      bench-u05 --n 20 --random-vectors --rerank off --label 150k-bulk-user
$PY scripts/bench/loadgen.py db-concurrency bench-real --clients 1,4,8,16 --seconds 20 --iterative --label "150k real-user scan=relaxed_order"
$PY scripts/bench/loadgen.py db-concurrency bench-u05  --clients 1,4,8,16 --seconds 20 --iterative --random-vectors --label "150k bulk-user 4% share scan=relaxed_order (worst case)"
$PY scripts/bench/seed.py --edges 2000 --edges-user bench-real                          # graph-expansion cost with edges
$PY scripts/bench/loadgen.py recall      bench-real --n 50 --rerank off --label "150k rerank=off +2000 edges"
$PY scripts/bench/seed.py --sizes
$PY scripts/bench/report.py $BENCH_OUT > /tmp/bench-tables.md
$PY scripts/bench/seed.py --wipe
```

Thresholds (in `report.py`): recall p95 < 500 ms e2e with 8 concurrent clients at scale (TEI-inclusive; reported per concurrency label — the
rerank-on (service default) and rerank-off warm sweeps are both hard rows, `fresh-queries` sweeps are informational);
DB-only recall p95 < 80 ms; capture p95 < 400 ms; zero errors; no OOM; exactly one active row under
20 concurrent `/correct`.

The raw JSONL of the two 2026-08-22 runs is kept at `scripts/bench/results/2026-08-22.jsonl` (run 1) and
`scripts/bench/results/2026-08-22-run2.jsonl` (run 2, feature-complete build); `report.py` on either reproduces its tables in
`docs/PERFORMANCE.md`. `report.py` exits 0 only when every hard threshold passes.

## Notes / gotchas

* The DB-only probe runs the deployed `recall()` with `rerank=False` (store-only numbers); the deployed code SET LOCALs
  `hnsw.iterative_scan=relaxed_order` inside each vector query, so the probe's "scan=off" mode now only changes the
  candidate-count query, not the recall timing.
* The service caches query embeddings (LRU 1 024) and `(query, hook)` rerank logits (LRU 4 096): the 30 fixed QUERIES are warm after
  one pass. Use `--unique` for the cold-path number; report both.
* `real-seed` / `baseline` / `embed-gap` writes are async-embedded: wait for `embed_backfill` (≤200 facts + 200 episodes per 30 s tick)
  before semantic-quality probes on freshly written rows.
* **Don't measure DB-only latency through the ssh tunnel.** The 768-d vector parameter (~15 KB of text,
  sent twice per query) trips Nagle/delayed-ACK stalls in the tunnel (+40–100 ms per query). That is
  why `db_probe.py` runs inside the container.
* `seed.py --wipe` deletes by `user_id LIKE 'bench-%'`; it needs the chain indexes from
  `astoria/sql/002_chain_indexes.sql` (without them a 100k-row wipe ran 17+ min — the self-FK
  `supersedes`/`superseded_by` forced a table scan per deleted row; with them: 7 s).
* `seed.py` default is `--index-mode inplace` (real per-row HNSW insert cost, ~130-200 rows/s on the
  NAS, CPU-bound on one PG core). `--index-mode rebuild` drops `fact_vec`/`episode_vec`, COPYs, then
  `CREATE INDEX CONCURRENTLY` — much faster for multi-million loads and it reports the build time.
* Supersede chains are emitted atomically (a `GROUP_END` marker keeps a chain inside one COPY batch,
  because `superseded_by` is a DEFERRED FK).
* `real-seed` writes through `POST /facts`, so those 500 rows carry true TEI vectors — the semantic
  hit-rate probes (`REAL_QUERIES`) are only meaningful on that user.
* TEI (CPU nomic on the NAS) saturates at ~6.5 embeds/s; every concurrent e2e number above 4 clients
  is TEI-bound. Compare with the DB-only columns (`recall`, `db-concurrency`) to see the store's own share.
* `baseline`/`capture` texts are made unique per run: re-posting identical turns hits the idem_key
  **dedupe** path (~6 ms, no embed) and would under-report capture cost.
* The DB-only probe imports `astoria` from `/app` **inside the deployed container** — a code change in the
  repo (e.g. the iterative-scan fix) is not on that path until redeploy; the probe's `modes`/`--iterative`
  flags apply the same GUC at session level to validate such a change before deploying.
