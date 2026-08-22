# Astoria — operations runbook

Running, deploying, backing up, restoring and debugging the live service on the NAS. Architecture is
in [ARCHITECTURE.md](ARCHITECTURE.md); the homelab-level view (ports, client wiring, decommission
record) is `~/projects/infrastructure/astoria.md`. Performance numbers: [PERFORMANCE.md](PERFORMANCE.md).

## 1. Where things are

| what | where |
|---|---|
| service | `http://192.168.1.134:8933` (REST) · `/mcp/` (MCP) · `/docs` (OpenAPI) · `/health` |
| NAS deployment dir | `/volume1/docker/astoria/` — `docker-compose.yml` (rendered by deploy.sh), `.env` (**secrets, mode 600, root**), `src/` (repo copy, build context), `pgdata/` (Postgres bind mount), `backups/` (pg_dump rotation), `data/` (service scratch) |
| containers | `astoria` (service, `:8933`), `astoria-postgres` (pgvector pg18, **`127.0.0.1:8934` NAS-local only**), `astoria-backup` (pg_dump sidecar), plus the pre-existing `memoryos-tei` (`:8931`, separate compose at `/volume1/docker/memoryos/`, now "tei only") |
| source of truth | `~/repos/astoria` on the workstation (git, branch `main`); `deploy/nas/` holds compose + deploy.sh + `.env.example` |
| workstation client config | `~/.config/astoria/env` (mode 600): `ASTORIA_URL`, `ASTORIA_TOKEN`, `ASTORIA_TOKEN_INPUT`, `ASTORIA_TOKEN_CLAUDE_CODE`, `ASTORIA_TOKEN_MEGAPLAN` |
| dev DB | `astoria-dev-pg` container on the workstation, `127.0.0.1:55432` (the default `ASTORIA_DB_DSN`); used by the unit/concurrency tests |
| SSH | `ssh root-dxp4800gt` (root alias; UGOS: `rick` is sudo-free and user cron is blocked — hence the in-container worker and the backup sidecar) |

## 2. Deploy / redeploy

```bash
cd ~/repos/astoria
./deploy/nas/deploy.sh              # build + (re)start   — after any code change
./deploy/nas/deploy.sh --no-build   # restart only        — after a .env change
```

What `deploy.sh` does (read it: `deploy/nas/deploy.sh`):
1. `ssh root-dxp4800gt mkdir -p /volume1/docker/astoria/{src,pgdata,backups,data}` and `chown 1000:1000 data backups`.
2. tars the repo (minus `.git`, `.venv`, caches, and `deploy/nas/{.env,pgdata,backups,data}`) over SSH into `…/astoria/src` (UGOS rsync is restricted).
3. renders `deploy/nas/docker-compose.yml` → `/volume1/docker/astoria/docker-compose.yml` (build context rewritten to `./src`), **refuses to continue if `.env` is missing**.
4. `docker compose up -d --build --remove-orphans && docker compose ps`, then curls `/health`.

First-time bootstrap on a new host: copy `deploy/nas/.env.example` to `/volume1/docker/astoria/.env`,
fill the secrets (§3), `chmod 600`, then run `deploy.sh`. The schema is applied automatically at service
start (`db.migrate()`), so a fresh `pgdata/` comes up empty but working.

Env-only changes (tokens, key, log level) need the container recreated: `deploy.sh --no-build` does
`docker compose up -d`, which recreates `astoria` because its `env_file` changed. Override host/dir with
`ASTORIA_NAS_SSH` / `ASTORIA_NAS_DIR`.

After every deploy: `scripts/smoke.sh` (health + MCP handshake + the T1 correction test on a throwaway
user) — takes ~5 s, prints PASS/FAIL per check.

## 3. `.env` keys (NAS: `/volume1/docker/astoria/.env`, never in git)

| key | used by | meaning |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | postgres, astoria (DSN is composed in compose), backup | DB credentials (`astoria`/`astoria` + a generated password) |
| `BACKUP_INTERVAL_S` (21600) / `BACKUP_KEEP` (14) | astoria-backup | dump cadence / rotation depth |
| `ASTORIA_USER_DEFAULT` | astoria | default `user_id` (`rick`) |
| `ASTORIA_EMBED_URL` / `ASTORIA_EMBED_DIM` | astoria | TEI endpoint (`http://192.168.1.134:8931`, 768); served model must contain `nomic-embed` |
| `ASTORIA_LLM_URL` / `ASTORIA_LLM_MODEL` | astoria (cognify) | SAINT `http://192.168.1.221:4000/v1`, `saint-cloud-medium` |
| `ASTORIA_LLM_FALLBACK_MODEL` | astoria (cognify) | `claude-sonnet-4-6` via direct Anthropic |
| `ANTHROPIC_API_KEY` | astoria (cognify fallback) | **set → the overnight direct-cloud path is ENABLED** (SAINT is off nightly) |
| `ASTORIA_CLIENT_TOKENS` | astoria (auth) | `input:…,claude-code:…,cli:…,megaplan:…` — Bearer token → client name |
| `ASTORIA_LOG_LEVEL` | astoria | `INFO` |
| `ASTORIA_WORKER_ENABLED` | astoria | `true`; `false` = API-only (no cognify/curator) |

Other knobs (all `ASTORIA_*`, see `astoria/config.py`; the wired ones): `DB_POOL_MIN/MAX` (1/8),
`EMBED_TIMEOUT_S` (20), `EMBED_MAX_CHARS` (6000), `LLM_TIMEOUT_S` (120), `RECALL_MIN_COSINE` (0.48),
`CONFIDENCE_FLOOR/CAP/STAGING_THRESHOLD` (.05/.98/.35), `COGNIFY_BATCH` (4), `HOST/PORT`.
`ASTORIA_DB_DSN` is set by compose (`postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@astoria-postgres:5432/$POSTGRES_DB`).

## 4. Health, status, logs

```bash
curl -s http://192.168.1.134:8933/health | jq .          # 200 iff DB ok; tei.ok / llm.saint / queue.{pending,dead}
astoria status                                           # same, as a table; exit 0 iff ok
ssh root-dxp4800gt 'docker ps --format "{{.Names}} {{.Status}} {{.Ports}}" | grep -E "astoria|tei"'
ssh root-dxp4800gt 'cd /volume1/docker/astoria && docker compose ps'
ssh root-dxp4800gt 'docker logs --since 1h astoria'        # service log
ssh root-dxp4800gt 'docker logs --tail 20 astoria-backup'  # "backup ok /backups/astoria-…dump" lines
ssh root-dxp4800gt 'docker logs --tail 50 astoria-postgres'
```

Log streams in `docker logs astoria` (json-file driver, 10 MB × 3):
- JSON request lines `{"ts","method","path","status","ms","client"}` — one per HTTP request (no bodies);
- uvicorn access lines (`GET /episodes?user_id=rick&limit=5 HTTP/1.1" 200 OK` — query strings included);
- Python logging: `astoria.cognify.worker` (`acquired leader lock 43`, `drain: {...}`, `cognify done user=…
  facts=N`), `astoria.llm` (`SAINT unavailable … trying direct Anthropic`), `astoria.embed`
  (`embedding failed (degrading)`, `embedding model mismatch`), `astoria.curator`, httpx request lines.

Container health: the `astoria` image has a `HEALTHCHECK` on `/health` (60 s); compose waits for
`astoria-postgres` `pg_isready` before starting the service and the backup sidecar.

## 5. Cognify queue and dead letters

`/health.queue` → `{pending (pending+failed+running), dead, by_state}`; `astoria queue` shows the same.
The worker drains every 30 s (immediately after start), 4 jobs per tick, one LLM call per coalesced
session chunk; failures back off 1/5/15/60/240 min and go `dead` after 5 attempts.

```bash
# inspect (psql inside the container; local socket = no password)
ssh root-dxp4800gt 'docker exec astoria-postgres psql -U astoria -d astoria -c \
  "SELECT id, user_id, session_id, state, attempts, next_attempt_at, left(last_error,120) err FROM cognify_queue WHERE state IN ('"'"'failed'"'"','"'"'dead'"'"','"'"'running'"'"') ORDER BY id"'

# replay dead jobs (e.g. after the LLM path is fixed)
ssh root-dxp4800gt 'docker exec astoria-postgres psql -U astoria -d astoria -c \
  "UPDATE cognify_queue SET state='"'"'pending'"'"', attempts=0, next_attempt_at=now(), last_error=NULL, finished_at=NULL WHERE state='"'"'dead'"'"'"'

# give up on them instead
…  "UPDATE cognify_queue SET state='skipped', finished_at=now() WHERE state='dead'"

# force an immediate drain: restart the service (drains on the first tick)
ssh root-dxp4800gt 'cd /volume1/docker/astoria && docker compose restart astoria'
```

Rows stuck in `running` (a crash mid-job) are reclaimed automatically after 30 min. Jobs whose
episode was deleted are marked `skipped`. `payload.result` on `done` rows records what was extracted
(`{facts, retracted, summary_episode, nothing_durable}`). If `pending` grows while `astoria` is up, check
§8 (LLM) and §9 (advisory lock).

## 6. Backups

- **What**: `astoria-backup` sidecar (same `pgvector/pgvector:pg18` image, so `pg_dump` matches the
  server major) runs `pg_dump -Fc` over the compose network every `BACKUP_INTERVAL_S` (6 h, first one
  60 s after start), writes `astoria-YYYY-MM-DD-HHMM.dump` atomically (`.tmp` → `mv`), and prunes to the
  newest `BACKUP_KEEP` (14) files → `/volume1/docker/astoria/backups/` (≈ 3.5 days of 6-hourly dumps).
  Log line `backup ok /backups/…` / `backup FAILED`.
- **Verified**: the first dump (`astoria-2026-08-22-1709.dump`, 52 KB) lists cleanly with
  `pg_restore --list` (68 TOC entries, server 18.6).
- **Check**:
  ```bash
  ssh root-dxp4800gt 'ls -lt /volume1/docker/astoria/backups | head; docker logs --tail 3 astoria-backup'
  ssh root-dxp4800gt 'docker run --rm -v /volume1/docker/astoria/backups:/b:ro pgvector/pgvector:pg18 \
      pg_restore --list /b/$(ls -t /volume1/docker/astoria/backups | head -1) | head -15'
  ```
- **Logical copy** (portable, human-readable, per user): `astoria export -o rick-$(date +%F).json`;
  re-import anywhere with `astoria import`.
- **Gap**: the dumps live on the same NAS RAID1 pool as `pgdata/` — drive failure is covered,
  NAS loss is not. An off-box copy (e.g. a workstation timer pulling the newest dump) is the next step.

## 7. Restore (step by step, into the running container)

Use when the data is corrupted/wiped and you want to roll back to a dump. Takes a couple of minutes.

```bash
ssh root-dxp4800gt
cd /volume1/docker/astoria
ls -lt backups | head                                   # 1. pick the dump
DUMP=backups/astoria-2026-08-22-1709.dump
docker run --rm -v $PWD/backups:/b:ro pgvector/pgvector:pg18 pg_restore --list /b/$(basename $DUMP) | head   # 2. sanity: TOC lists
docker compose stop astoria astoria-backup              # 3. stop writers (postgres stays up)
docker cp $DUMP astoria-postgres:/tmp/restore.dump      # 4. put the dump inside the DB container
# 5a. in-place restore (drops+recreates objects the dump contains; keeps the DB and extensions)
docker exec astoria-postgres pg_restore -U astoria -d astoria --clean --if-exists --no-owner --exit-on-error /tmp/restore.dump
#   — or — 5b. fresh database (cleanest; requires no connections, which step 3 ensured)
# docker exec astoria-postgres psql -U astoria -d postgres -c 'DROP DATABASE astoria' -c 'CREATE DATABASE astoria'
# docker exec astoria-postgres pg_restore -U astoria -d astoria --no-owner --exit-on-error /tmp/restore.dump
docker exec astoria-postgres rm /tmp/restore.dump
docker exec astoria-postgres psql -U astoria -d astoria -c "SELECT count(*) facts FROM fact" -c "SELECT version FROM schema_migrations"   # 6. check
docker compose start astoria astoria-backup             # 7. restart; migrate() is a no-op for already-applied versions
curl -s http://192.168.1.134:8933/health | jq .db        # 8. facts_active back to the expected count
```

Notes: the postgres image trusts local-socket connections, so `docker exec … -U astoria` needs no
password (the sidecar uses `PGPASSWORD` over TCP). `--clean --if-exists` emits harmless notices for
objects that do not exist yet. If `pg_restore` complains about `CREATE EXTENSION vector` on a fresh DB,
run `psql -U astoria -d astoria -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS
pgcrypto'` first and retry. A restore into a *different* host: bring up the compose stack there with an
empty `pgdata/`, let the service create the schema once, then follow the same steps.

To restore a **single user** without touching others: `astoria export` from a scratch stack restored
from the dump, then `astoria import` into production (logical, idempotent).

## 8. Schema upgrades

Migrations are plain SQL files in `astoria/sql/NNN_name.sql` (packaged with the wheel), applied in
lexical order by `db.migrate()` at every service start; each version is recorded in
`schema_migrations` and never re-run. To add one:

1. `astoria/sql/002_<what>.sql` — write statements idempotently anyway (`IF NOT EXISTS`, `ADD COLUMN IF
   NOT EXISTS`, `ON CONFLICT DO NOTHING`); end with `INSERT INTO schema_migrations(version) VALUES
   ('002_<what>') ON CONFLICT DO NOTHING;` (migrate() also records it).
2. run the tests against the dev DB (`pytest`), then `./deploy/nas/deploy.sh` — the container rebuilds
   (SQL is package data) and applies the file at startup (`migrations applied: ['002_…']` in the log).
3. **Back up first** for anything destructive — the sidecar dump is at most 6 h old, or force one:
   `ssh root-dxp4800gt 'docker exec astoria-backup sh -c "pg_dump -Fc -h astoria-postgres -U astoria astoria > /backups/astoria-manual-$(date +%F-%H%M).dump"'`.
4. If a migration fails, the service keeps booting (`db.migrate failed at startup; continuing`) and
   `/health` still works; fix the file and redeploy — nothing partial is recorded because each file runs
   in one transaction before its `schema_migrations` insert.

Postgres major upgrades: `pgdata/` is a bind mount of the pg18 image layout (`/var/lib/postgresql`);
moving majors = dump with the old image, start the new image on an empty `pgdata/`, restore (§7).

## 9. Troubleshooting

| symptom | check | fix |
|---|---|---|
| `/health` 503 / `status: error` | `docker ps` shows `astoria-postgres` unhealthy; `docker logs astoria-postgres` | `docker compose up -d`; disk full on `/volume1`?; `pgdata/` permissions (owned by the image's postgres uid) |
| `tei.ok: false`, `recall.health.degraded: true` | `curl -s http://192.168.1.134:8931/info` (model id must contain `nomic-embed`); `docker ps | grep memoryos-tei` | `docker compose -f /volume1/docker/memoryos/docker-compose.yml up -d tei`. New rows written meanwhile have `embedding=NULL`; the curator backfills within 15 min (or restart `astoria` to run it now). A **model mismatch** is refused on purpose (`embedding model mismatch` in the log) — never point Astoria at a different embedder without re-embedding everything |
| `llm.saint: unreachable (...)` | expected **every night** (workstation off) and whenever SAINT is down | cognify falls to direct Anthropic if `ANTHROPIC_API_KEY` is set (`llm.fallback: true`); otherwise jobs back off until SAINT returns |
| queue `failed`/`dead` with `last_error: llm unavailable: saint: …; anthropic: …` | both paths failed — key missing/invalid, outage, or `LLM_TIMEOUT_S` too low | fix the key in `.env` → `deploy.sh --no-build`; then replay dead rows (§5) |
| `last_error: extraction returned no valid JSON` | model returned prose twice | usually transient; if persistent, check `ASTORIA_LLM_MODEL` still resolves on SAINT (`curl http://192.168.1.221:4000/v1/models`) |
| queue `pending` grows, log never says `acquired leader lock 43` | someone else holds the advisory lock: `docker exec astoria-postgres psql -U astoria -d astoria -c "SELECT pid, application_name, client_addr, state FROM pg_stat_activity WHERE pid IN (SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=43)"` | stop the other holder (a second `astoria` instance, or a dev process pointed at the NAS DB) or `SELECT pg_terminate_backend(<pid>)`; the lock is session-scoped and frees when its connection closes |
| worker loop crashed (`worker loop crashed; API keeps serving`) | `docker logs astoria` traceback | `docker compose restart astoria` |
| MCP client can't connect | URL must end with `/mcp/`; `curl -s http://192.168.1.134:8933/` shows `"mcp": "/mcp/"`; `MCP endpoint disabled` in the log means FastMCP failed to import | rebuild (`deploy.sh`), check `fastmcp` pin (`>=2.3,<3`) |
| a wrong fact keeps coming back | it is being re-extracted | `retract`/`forget` it (writes a tombstone); check `astoria audit` for `blocked_tombstone` entries to confirm the guard is firing |
| `action: "historical"` when you expected a supersede | the new statement's `asserted_at` is older than the active row's (only possible from the store/resolver, or a back-dated episode `occurred_at`) | assert it again now (explicit `remember`) |
| `detector.action: "error"` in a capture response | detector hit a DB error (e.g. bad cardinality) — the episode was still stored | read `detector.error`; the fact can be asserted explicitly |
| backups stopped (`backup FAILED`) | `docker logs astoria-backup`; `df -h /volume1` | disk; password changed in `.env` without recreating the sidecar (`deploy.sh --no-build`) |

## 10. Capacity knobs

| knob | default | where |
|---|---|---|
| service memory | 768 MB | compose `mem_limit` (`astoria`) |
| Postgres memory | 1 GB limit, `shared_buffers=256MB`, `maintenance_work_mem=128MB`, `shm_size` 256 MB | compose `astoria-postgres` |
| DB pool | `ASTORIA_DB_POOL_MIN/MAX` 1/8 | `.env` |
| uvicorn workers | 1 (by design: the in-process worker + advisory lock) | Dockerfile CMD |
| cognify | `ASTORIA_COGNIFY_BATCH` 4 jobs/tick, tick 30 s, ≤ 8 episodes / 6 000 chars per LLM call, `LLM_TIMEOUT_S` 120 | `.env` / `worker.py` |
| embeddings | TEI batch 8, `EMBED_MAX_CHARS` 6000, `EMBED_TIMEOUT_S` 20; backfill 200 facts + 200 episodes / 15 min | `.env` / `embed.py` / `maintenance.py` |
| recall | top-40 ⊕ 40 facts, 20 ⊕ 20 episodes, `hnsw.ef_search=64`, `RECALL_MIN_COSINE` 0.48, `max_tokens` ≤ 20000, `limit` ≤ 200 | `recall.py` / request |
| retention | snapshots 90 d, working turns archived after 14 d, backups 14 × 6 h | `maintenance.py` / `.env` |
| TEI (`memoryos-tei`) | `--max-batch-tokens 2048 --max-client-batch-size 8 --max-concurrent-requests 64`, `mem_limit 3g` | `/volume1/docker/memoryos/docker-compose.yml` (the 7 GiB NAS OOM-loops on the default warmup) |

Measured throughput/latency and the 10k-fact benchmark: [PERFORMANCE.md](PERFORMANCE.md)
(`scripts/bench/`, acceptance T7).

## 11. Tests

```bash
cd ~/repos/astoria && . .venv/bin/activate
docker start astoria-dev-pg 2>/dev/null || true          # dev DB on 127.0.0.1:55432 (default ASTORIA_DB_DSN)
uvicorn astoria.api.app:app --port 8977 &                 # API tests default to ASTORIA_URL=http://127.0.0.1:8977
pytest                                                    # unit + concurrency (store-level) + API tests (skip when the API/DB is down)
ASTORIA_URL=http://192.168.1.134:8933 pytest tests/test_acceptance.py -m "not slow"   # T1–T12 against the NAS (throwaway users)
ASTORIA_URL=http://192.168.1.134:8933 pytest tests/test_acceptance.py -m slow -k t7   # 10k-fact scale test
pytest -m llm                                             # tests that make a real LLM call
scripts/smoke.sh                                          # 5-second post-deploy check
```
Acceptance tests skip when `/health` is unreachable and fall back to API-only variants when they cannot
reach the service's DB directly.
