# Astoria — operations runbook

Deploying, upgrading, backing up, restoring, watching and debugging an Astoria service. Architecture is
in [ARCHITECTURE.md](ARCHITECTURE.md), every knob in [CONFIGURATION.md](CONFIGURATION.md), measured
capacity in [PERFORMANCE.md](PERFORMANCE.md). Commands assume the compose stack in `deploy/nas/`
(service `astoria`, database `astoria-postgres`, sidecar `astoria-backup`, optional `astoria-rerank`) and
use `http://nas.local:8933` as the service URL.

## 1. Components at a glance

| component | what | where it listens |
|---|---|---|
| `astoria` | FastAPI + FastMCP + in-process worker (one uvicorn worker) | `:8933` REST, `/mcp/` MCP, `/docs` OpenAPI, `/health` |
| `astoria-postgres` | Postgres 18 + pgvector, bind-mounted `./pgdata` | `127.0.0.1:8934` on the host only |
| `astoria-backup` | `pg_dump -Fc` every `BACKUP_INTERVAL_S`, keep `BACKUP_KEEP` | writes `./backups/astoria-YYYY-MM-DD-HHMM.dump` |
| `astoria-rerank` (optional) | TEI cross-encoder reranker | `:8935` |
| embedder (external) | any nomic-embed-text-v1.5 endpoint(s) named in `ASTORIA_EMBED_URLS` | — |
| LLM (external) | OpenAI-compatible gateway named in `ASTORIA_LLM_URL`; optional Anthropic fallback | — |

## 2. Deploy and upgrade

First deployment:

```bash
# on the host that runs the stack
mkdir -p astoria && cd astoria
cp <repo>/deploy/nas/.env.example .env            # fill POSTGRES_PASSWORD, endpoints, ASTORIA_USER_DEFAULT, tokens; chmod 600
cp <repo>/deploy/nas/docker-compose.yml .         # point `build.context` at the repo checkout (or use a prebuilt image)
mkdir -p pgdata backups data
docker compose up -d --build astoria-postgres astoria astoria-backup
# optional reranker: put a Hugging Face snapshot of cross-encoder/ms-marco-MiniLM-L-6-v2 (incl. onnx/model.onnx)
# in ./rerank-model, then `docker compose up -d astoria-rerank` and set ASTORIA_RERANK_URLS; leave the
# variable empty to run without the stage
curl -s http://nas.local:8933/health | jq .status  # "ok"
```

`deploy/nas/deploy.sh` is a reference script for the "build on a workstation, ship the source tree over
ssh, compose up" pattern. It reads site values from the gitignored `deploy/nas/deploy.env`
(`ASTORIA_NAS_SSH` — ssh host alias, default `nas`; `ASTORIA_NAS_DIR` — deploy directory, default
`/opt/astoria`; `ASTORIA_URL` for the post-deploy health check), never copies `.env`, rewrites the compose
build context to the shipped source directory, and refuses to start when `$DEST/.env` is missing. Adapt it
or replace it with your registry/CI flow.

Upgrade = rebuild the image and restart the service; **migrations apply themselves** at start-up
(`astoria/sql/NNN_*.sql` in lexical order, each recorded in `schema_migrations`):

```bash
docker compose up -d --build astoria             # code change
docker compose up -d astoria                     # .env change only (recreates the container)
docker logs astoria 2>&1 | grep -m1 'migrations applied'   # lists newly applied versions, if any
```

A new schema file is additive and idempotent (`CREATE … IF NOT EXISTS`, `ALTER TABLE … SET`). A rollback
of the code does not roll back the schema; keep schema changes backward-compatible with the previous
release or restore from a dump.

After any deploy:

```bash
ASTORIA_URL=http://nas.local:8933 scripts/smoke.sh
```

`smoke.sh` checks `/health` (db, tei, version, queue), the MCP handshake (`initialize` → `tools/list`
must contain `recall capture remember forget memory retrieve_memory add_memory get_user_profile`), a full
correct-and-recall cycle on a throwaway user (`POST /facts` → `POST /correct` → `GET /facts` → `POST
/recall` → detector path via `/capture`), then `DELETE /users/{id}`. Exit 0 iff every check passed.

## 3. Health and status

```bash
curl -s http://nas.local:8933/health | jq .        # status/version, db counts, queue, tei endpoints, llm, rerank
astoria status                                     # the same as a table
astoria queue                                      # POST /op queue_stats
docker compose ps                                  # container state, restarts
```

What to look at:

| field | healthy | meaning when not |
|---|---|---|
| `status` | `ok` (HTTP 200) | `error` / 503 — database unreachable |
| `tei.ok`, `tei.active` | `true`, the endpoint you expect first | no usable embedding endpoint → recall BM25-only, new rows unembedded until one returns |
| `tei.endpoints[].verified / error` | verified, no error | `not nomic` / dim mismatch → wrong model behind that URL (disabled for 10 min); connect errors → down (retried after 60 s) |
| `llm.saint` | `reachable` | primary gateway unreachable; cognify uses the Anthropic fallback if `llm.fallback` is true, else jobs back off |
| `rerank.status` | `on` (or `off` if you disabled it) | `down` — reranker endpoints unreachable; recall keeps the base order |
| `queue.pending` | small, not growing | growing → LLM path broken or the worker is not the leader (§4) |
| `queue.dead` | 0 | jobs that exhausted 5 attempts (§4) |
| `user_default` | your user id | the `user_id` applied to requests that omit one (`ASTORIA_USER_DEFAULT`) |

## 4. Cognify queue, worker, dead letters

The worker ticks every `ASTORIA_COGNIFY_POLL_S` (30 s; immediately after start): embed backfill, then up to
`ASTORIA_COGNIFY_BATCH` (4) jobs coalesced per session → one LLM call each; failures back off 1 / 5 / 15 /
60 / 240 min and go `dead` after 5 attempts. Only the process holding advisory lock 43 drains.

```bash
# what is in the queue (by state, oldest, last 20 dead jobs, embedding backlog)
curl -s -X POST http://nas.local:8933/op -H 'Content-Type: application/json' -d '{"action":"queue_stats"}' | jq .

# inspect failed/dead rows in SQL (local socket inside the container needs no password)
docker exec astoria-postgres psql -U astoria -d astoria -c \
  "SELECT id, user_id, session_id, state, attempts, next_attempt_at, left(last_error,120) err
     FROM cognify_queue WHERE state IN ('failed','dead','running') ORDER BY id"

# replay dead jobs after fixing the cause (they are picked up on the next tick)
docker exec astoria-postgres psql -U astoria -d astoria -c \
  "UPDATE cognify_queue SET state='pending', attempts=0, next_attempt_at=now(), last_error=NULL, finished_at=NULL WHERE state='dead'"

# or give up on them
docker exec astoria-postgres psql -U astoria -d astoria -c \
  "UPDATE cognify_queue SET state='skipped', finished_at=now() WHERE state='dead'"

# force an immediate drain
docker compose restart astoria

# who holds the leader lock? (a second service instance or a developer process pointed at this DB idles)
docker exec astoria-postgres psql -U astoria -d astoria -c \
  "SELECT pid, application_name, client_addr, state FROM pg_stat_activity
    WHERE pid IN (SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=43)"
```

Rows stuck in `running` (a crash mid-job) are reclaimed automatically after 30 minutes. Jobs whose episode
was deleted are marked `skipped`. `payload.result` on `done` rows records what was extracted.

Embedding backlog (`embed_backlog` in `queue_stats`): with the asynchronous write path every new fact and
episode waits for the next tick to be embedded; a backlog that does not drain means every embedding
endpoint is down (`/health.tei`).

## 5. Logs

- Service: `docker logs -f astoria`. Two kinds of lines on stdout: the JSON request log
  `{"ts","method","path","status","ms","client"}` (no bodies; uvicorn's own access log adds query
  strings) and Python logging for `astoria.*` (`ASTORIA_LOG_LEVEL`). Worker lines of interest:
  `worker: acquired leader lock 43`, `drain: {...}`, `cognify done user=… facts=… retracted=…`,
  `embed_backfill: …`, `rederive_profile …`, `reflect …`, `dedup_facts …`, `decay …`,
  `prune_snapshots: …`, `embedding endpoint … failed … cooling down`, `rerank endpoint … failed`,
  `SAINT unavailable … trying direct Anthropic` (primary LLM down).
- Database: `docker logs astoria-postgres`.
- Backups: `docker logs astoria-backup` (`backup ok /backups/…` or `backup FAILED`).
- Compose keeps ≤ 30 MB of log per container (`json-file`, 3 × 10 MB).

## 6. Backups

The `astoria-backup` sidecar runs `pg_dump -Fc` over the compose network every `BACKUP_INTERVAL_S`
(default 6 h, first one 60 s after start), writes `astoria-YYYY-MM-DD-HHMM.dump` atomically (`.tmp` →
`mv`), and prunes to the newest `BACKUP_KEEP` (14) files in `./backups/`. The dump is the whole database
(schema + all users + queue + audit), compressed, **not encrypted**.

```bash
ls -lt backups | head                                                  # newest first
docker logs --tail 3 astoria-backup                                    # last outcomes
docker run --rm -v "$PWD/backups:/b:ro" pgvector/pgvector:pg18 \
  pg_restore --list /b/$(ls -t backups | head -1) | head -15         # the dump lists cleanly
```

Off-site copy — dumps on the same disks as `pgdata/` protect against data corruption and operator error,
not against losing the host. An example pull from another machine (cron/systemd timer):

```bash
# on the off-site machine
rsync -az --delete nas.local:/path/to/astoria/backups/ /srv/backups/astoria/
# or, if only the newest dump is wanted:
scp "nas.local:/path/to/astoria/backups/$(ssh nas.local 'ls -t /path/to/astoria/backups | head -1')" /srv/backups/astoria/
```

Portable, human-readable per-user copies: `astoria export -o alice-$(date +%F).json` → `astoria import`
anywhere (logical, idempotent; see CLI.md).

## 7. Restore

Into the running stack, from a dump (a few minutes):

```bash
cd /path/to/astoria
ls -lt backups | head                                      # 1. pick the dump
DUMP=backups/astoria-2026-08-22-1709.dump
docker run --rm -v "$PWD/backups:/b:ro" pgvector/pgvector:pg18 pg_restore --list /b/$(basename $DUMP) | head   # 2. sanity
docker compose stop astoria astoria-backup                 # 3. stop writers (postgres stays up)
docker cp $DUMP astoria-postgres:/tmp/restore.dump         # 4. dump into the DB container
# 5a. in-place (drops and recreates the objects the dump contains; keeps DB + extensions)
docker exec astoria-postgres pg_restore -U astoria -d astoria --clean --if-exists --no-owner --exit-on-error /tmp/restore.dump
#  — or — 5b. fresh database (cleanest; step 3 ensured no connections)
# docker exec astoria-postgres psql -U astoria -d postgres -c 'DROP DATABASE astoria' -c 'CREATE DATABASE astoria'
# docker exec astoria-postgres psql -U astoria -d astoria -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto'
# docker exec astoria-postgres pg_restore -U astoria -d astoria --no-owner --exit-on-error /tmp/restore.dump
docker exec astoria-postgres rm /tmp/restore.dump
docker exec astoria-postgres psql -U astoria -d astoria -c "SELECT count(*) facts FROM fact" -c "SELECT version FROM schema_migrations"   # 6. check
docker compose start astoria astoria-backup                # 7. restart; migrate() is a no-op for applied versions
curl -s http://nas.local:8933/health | jq .db              # 8. counts back to the expected values
```

Notes: the postgres image trusts local-socket connections, so `docker exec … -U astoria` needs no
password. `--clean --if-exists` emits harmless notices for objects that do not exist yet. To restore into
a **different host**, bring the compose stack up there with an empty `pgdata/`, let the service create
the schema once, then follow the same steps. To restore a **single user** without touching others: run a
scratch stack from the dump, `astoria export` that user, `astoria import` into production.

## 8. Capacity knobs (measured basis in PERFORMANCE.md)

| knob | default | where |
|---|---|---|
| service memory | 768 MiB | compose `mem_limit` (`astoria`) |
| Postgres memory | 1 GiB limit, `shared_buffers=256MB`, `maintenance_work_mem=128MB`, `shm_size 256m` | compose `astoria-postgres` — raise to 2–3 GiB / 768 MB–1 GB shared_buffers before ~150–200 k embedded rows (HNSW + TOAST ≈ 8.6 KB/row; the hot graph must fit the page cache) |
| DB pool | `ASTORIA_DB_POOL_MIN/MAX` 1 / 8 | keep `MAX` ≥ expected concurrent recalls |
| uvicorn workers | 1 (by design) | `Dockerfile` CMD |
| write-path embedding | asynchronous (`ASTORIA_EMBED_SYNC=false`); backfill 200 facts + 200 episodes per 30 s tick | `.env`; `worker.EMBED_BACKFILL_LIMIT` |
| embedding endpoints | priority list, fastest first | `ASTORIA_EMBED_URLS` — the embedder, not Postgres, was the throughput ceiling in the scale run (CPU TEI ≈ 6.5 embeds/s) |
| reranker | top-30 facts + 6 episodes, 240-char texts, weight 0.6, 3 s timeout | `ASTORIA_RERANK_*`; a GPU reranker allows a larger `TOP_N` |
| recall | 40 vector ⊕ 40 BM25 facts, 20 ⊕ 20 episodes, `hnsw.ef_search=64`, `iterative_scan=relaxed_order`; cosine ≥ 0.45, default `limit` 12 / `max_tokens` 1000, half-lives 30/180/60 d | `retrieval/recall.py` constants; `ASTORIA_RECALL_MIN_COSINE`, `ASTORIA_RECALL_LIMIT`, `ASTORIA_RECALL_TOKEN_BUDGET`, `ASTORIA_*_HALF_LIFE_DAYS` |
| cognify | 4 jobs / 30 s tick, ≤ 8 episodes or 6000 chars per LLM call, 120 s LLM timeout | `ASTORIA_COGNIFY_*`, `ASTORIA_LLM_TIMEOUT_S` |
| retention | snapshots 90 d; working turns archived after 72 h or beyond 20 per session; machine facts decayed after 90 d unused (decay half-lives 90 d / 45 d for beliefs); backups 14 × 6 h | curator settings, `BACKUP_INTERVAL_S`/`BACKUP_KEEP` |
| Postgres autovacuum | scale factor 0.02 + fillfactor 90 on `fact`/`episode` (schema 004) | keeps recall's `last_seen` touch-updates HOT and bloat low |
| bulk import | direct-SQL loads should drop the HNSW indexes, `COPY`, then `CREATE INDEX CONCURRENTLY` (`scripts/bench/seed.py --index-mode rebuild`) | in-place HNSW insert is ~100–200 rows/s on one core |

## 9. Troubleshooting

| symptom | check | fix |
|---|---|---|
| `/health` 503, `status: error` | `docker compose ps` (database unhealthy), `docker logs astoria-postgres`, disk full, `pgdata/` permissions | `docker compose up -d`; free disk; fix ownership (the image's postgres uid) |
| `tei.ok: false`, `recall.health.degraded: true` | `/health.tei.endpoints[].error`; `curl <endpoint>/info` or `/v1/models` — the served model must mention `nomic-embed` and return 768-d vectors | start/repair the embedder; rows written meanwhile carry `embedding NULL` and are backfilled on the next tick. A **model mismatch is refused on purpose** — never point Astoria at a different embedder without re-embedding everything |
| one endpoint `usable:false` with `canary cosine … DIFFERENT vector space` | that URL serves a model whose vectors do not match the first verified endpoint | fix the model behind it (same model + same normalisation); it is retried after 10 min |
| `llm.saint: unreachable (...)` | primary gateway down | expected when the gateway host is off; cognify uses Anthropic if `ANTHROPIC_API_KEY` is set (`llm.fallback: true`), else jobs back off until it returns |
| queue rows `failed`/`dead` with `last_error: llm unavailable: saint: …; anthropic: …` | both LLM routes failed — key missing/invalid, outage, `ASTORIA_LLM_TIMEOUT_S` too low | fix `.env` → recreate the service → replay dead rows (§4) |
| `last_error: extraction returned no valid JSON` | the model answered prose twice | usually transient; if persistent check that `ASTORIA_LLM_MODEL` still resolves on the gateway (`GET <llm_url>/models`) |
| `queue.pending` grows; log never says `acquired leader lock 43` | another process holds the advisory lock (§4 query) | stop the other holder or `SELECT pg_terminate_backend(<pid>)`; the lock frees when its connection closes |
| `worker loop crashed; API keeps serving` | traceback in `docker logs astoria` | `docker compose restart astoria` |
| MCP client cannot connect | URL must end with `/mcp/`; `GET /` shows `"mcp": "/mcp/"`; `MCP endpoint disabled` in the log = FastMCP failed to import | rebuild; check the `fastmcp>=2.3,<3` pin |
| `401 … requires a client token` | `ASTORIA_REQUIRE_TOKEN=true` and the client sent no valid bearer token | give the client a token from `ASTORIA_CLIENT_TOKENS` (or set `REQUIRE_TOKEN=false` on a trusted network) |
| a wrong fact keeps coming back | it is being re-extracted from old episodes | `retract`/`forget` it (writes a tombstone); `astoria audit` shows `blocked_tombstone` when the guard fires |
| an extracted value did not replace a human-stated one | trust guard: it is in `staging` with `meta.conflict_with` (`conflict_staged` in the audit) | `astoria staging` → `astoria approve <id>` if it is right |
| `action: "historical"` when you expected a supersede | the statement's `asserted_at` is older than the active row's (back-dated `asserted_at`, or an old episode `occurred_at` through cognify/import) | assert it again now (explicit `remember` / `POST /facts` without `asserted_at`) |
| `detector.action: "error"` in a capture response | the detector hit a store error; the episode was stored | read `detector.error`; assert the fact explicitly |
| recall returns few vector candidates for a user who is a small share of a big index | `hnsw.iterative_scan` not in effect (pgvector < 0.8) | use pgvector ≥ 0.8 (the `pgvector/pgvector:pg18` image does) |
| `rerank.status: down` | reranker container / endpoint unreachable; `GET <rerank_url>/info` | restart it or set `ASTORIA_RERANK_URLS=` to turn the stage off explicitly |
| backups stopped (`backup FAILED`) | `docker logs astoria-backup`, disk space, password changed in `.env` without recreating the sidecar | free disk; `docker compose up -d astoria-backup` |
| hard delete / user wipe is slow | schema 002 indexes missing (very old database) | restart the service (migrations apply) |

## 10. Tests

```bash
. .venv/bin/activate
# store-level + concurrency tests need a Postgres with pgvector (default ASTORIA_DB_DSN is a local dev DB on 55432)
pytest                                                             # unit + concurrency + API (API tests skip when no service)
uvicorn astoria.api.app:app --port 8977 &                           # API tests default to ASTORIA_URL=http://127.0.0.1:8977
ASTORIA_URL=http://nas.local:8933 pytest tests/test_acceptance.py -m "not slow"   # T1–T12 against a live service (throwaway users)
ASTORIA_URL=http://nas.local:8933 ASTORIA_RUN_SLOW=1 pytest tests/test_acceptance.py -k t7   # 10k-fact recall p95
pytest -m llm                                                      # tests that make a real LLM call
ASTORIA_URL=http://nas.local:8933 scripts/smoke.sh                 # post-deploy check
```

The acceptance suite (`tests/test_acceptance.py`) is the behavioural contract: T1 correction propagates
(and the detector path), T2 edit/delete + tombstone, T3 valid/belief time travel and back-dated
corrections, T4 cross-client provenance, T5 provenance fields, T6 capture durable without an LLM + gate,
T7 scale, T8 set predicates, T9 idempotency, T10 staging + approve, T11 out-of-order assertions and
belief axis at the store, T12 compatibility routes, plus health, wipe, briefing/profile, audit,
forget-by-query and predicate flips. `tests/test_belief_axis.py` pins the versioned supersede (original
belief-closed, copy carries `valid_to`, `history` hides the original, `as_believed_at` answers the past).
Every test uses a throwaway `user_id` and wipes it.
