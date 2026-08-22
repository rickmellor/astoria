# Astoria — configuration reference

Everything Astoria reads at start-up comes from the environment, from a `.env` file in the working
directory (pydantic-settings `env_file=".env"` — handy for a development checkout), or from the `.env` the
compose stack passes through `env_file`. Settings are declared once in `astoria/config.py` (`Settings`,
env prefix `ASTORIA_`), cached for the life of the process, and read by the modules named in the tables
below. Every field listed here is read by code; request parameters override the retrieval defaults per
call.

Conventions used here:

- **env** — the variable name. Every `Settings` field is `ASTORIA_<FIELD_IN_UPPER_CASE>` except
  `ANTHROPIC_API_KEY`, which is read without the prefix.
- **default** — the value shipped in `config.py`. Network defaults point at `localhost`; set the endpoint
  variables explicitly for any real deployment.
- **read by** — the module that consults the value (so you know what a change affects).

---

## 1. Storage

| env | default | read by | meaning / when to change |
|---|---|---|---|
| `ASTORIA_DB_DSN` | `postgresql://astoria:astoria@127.0.0.1:55432/astoria` | `store/db.py`, `cognify/worker.py` (leader-lock connection) | Postgres DSN. The compose stack overrides it from `POSTGRES_*` (service → `astoria-postgres:5432`). The shipped default is a local development database. |
| `ASTORIA_DB_POOL_MIN` | `1` | `store/db.py` | psycopg pool floor. |
| `ASTORIA_DB_POOL_MAX` | `8` | `store/db.py` | psycopg pool ceiling. Keep ≥ the expected number of concurrent recalls; the store sustained 8–16 parallel recalls without error in the scale validation (see PERFORMANCE.md). |

Schema migrations (`astoria/sql/NNN_*.sql`) are applied idempotently at start-up; there is no setting to
disable them. The service also creates the `vector` and `pgcrypto` extensions before opening the pool.

## 2. Identity and authentication

| env | default | read by | meaning / when to change |
|---|---|---|---|
| `ASTORIA_USER_DEFAULT` | `default` | `api/service.py` (`_uid`), `/health` | `user_id` applied when a request omits one (REST body/query, MCP tools with `user_id=""`, CLI without `ASTORIA_USER`). Set it to your own user id; every table is keyed by `user_id`, so several users can share one instance. Reported as `user_default` in `/health`. |
| `ASTORIA_CLIENT_TOKENS` | `""` | `config.Settings.client_token_map()`, `api/auth.py` | Comma-separated `name:token` pairs. A request whose `Authorization: Bearer <token>` matches is attributed to `name` (fact `source`, audit `actor`, trust cap). Tokens are opaque strings you generate (e.g. `openssl rand -hex 24`). |
| `ASTORIA_REQUIRE_TOKEN` | `false` | `api/auth.py` (`require_write_token`) | When `true`, every **write** action (anything not in `auth.READ_ACTIONS`) needs a valid bearer token and returns `401` otherwise. Reads stay open. Turn this on before exposing the port beyond a trusted network. |

Read actions (never token-gated): `recall`, `briefing`, `profile`, `facts_list`, `fact_get`, `history`,
`as_of`, `episodes_list`, `episode_get`, `predicates_list`, `audit`, `health`, `retrieve`, `user_profile`,
`graph`, `edges_list`, `aliases_list`, `resolve`, `queue_stats`.

Client trust caps are a code table (`store/facts.py` `CLIENT_TRUST`), keyed by client name:
`cli`/`human` 1.0 · `input`/`claude-code` 0.85 · `api`/`mcp` 0.7 · `megaplan` 0.6 · `curator`/`anonymous` 0.5 ·
`import` 0.4 · any other name 0.6. They rank; they never decide conflicts (see ARCHITECTURE.md §6).

## 3. Embeddings

Astoria embeds every fact hook and episode hook with **nomic-embed-text-v1.5 (768 dimensions)** through any
OpenAI-compatible `/v1/embeddings` endpoint (Hugging Face TEI, vLLM, llama.cpp server, a router in front
of them). The vector space is pinned: the service verifies each endpoint before use and refuses one that
serves a different model or dimension, so two endpoints can never write incompatible vectors into one index.

| env | default | read by | meaning / when to change |
|---|---|---|---|
| `ASTORIA_EMBED_URL` | `http://localhost:8931` | `core/embed.py` | Single embedding endpoint (model name `nomic`), used when `ASTORIA_EMBED_URLS` is empty. |
| `ASTORIA_EMBED_URLS` | `""` | `core/embed.py` | Optional **priority list** `url\|model,url\|model`. Endpoints are tried in order; the first usable one answers. Put the fastest (e.g. GPU) endpoint first and an always-on CPU endpoint last. Takes precedence over `ASTORIA_EMBED_URL` when set. |
| `ASTORIA_EMBED_DIM` | `768` | `core/embed.py` | Expected vector width; must match the `vector(768)` columns. Changing it means a new schema and a full re-embed. |
| `ASTORIA_EMBED_REQUIRE_SUBSTRING` | `nomic-embed` | `core/embed.py` | Served-model assertion. The part before the first `-` (`nomic`) must appear in the endpoint's response `model`, the configured model name, `GET /info` (TEI) or `GET /v1/models`. |
| `ASTORIA_EMBED_TIMEOUT_S` | `20.0` | `core/embed.py` | HTTP timeout per embedding call. |
| `ASTORIA_EMBED_MAX_CHARS` | `6000` | `core/embed.py` | Texts are truncated to this many characters before embedding (the model's own cap is 2048 tokens; TEI auto-truncates). |
| `ASTORIA_EMBED_SYNC` | `false` | `core/capture.py`, `api/service.py` (`fact_add`), `store/facts.py` (`update_fact`) | `false` = **asynchronous write path**: `capture`, `POST /facts` and a `PATCH` that changes a value store the row with `embedding NULL` and return without calling the embedder; the worker's `embed_backfill` fills it on its next tick (≤ 30 s by default). BM25 recall finds the row immediately; vector recall after backfill. `true` = embed inline in the request. A per-request `sync=true` forces inline embedding for that call. |

Endpoint verification and failure handling (`core/embed.py`):

- On first use each endpoint gets a **canary** request. The served model must satisfy the substring
  assertion and return `embed_dim` floats; the canary vector must have cosine ≥ 0.98 with the canary of
  the first verified endpoint (same vector space). A failed check disables the endpoint for 600 s.
- A transport/HTTP failure cools the endpoint down for **60 s**, then it is retried — so an endpoint that
  powers on or comes back is picked up within a minute.
- All vectors are L2-normalised client-side (some servers return raw pooled vectors, TEI returns unit
  vectors), and an LRU cache (1024 entries) short-circuits repeated texts.
- Batches of 8 per request (the smallest `max-client-batch-size` seen on CPU TEI deployments).
- Every endpoint down → `None` vectors: writes proceed without embeddings, recall degrades to BM25 and
  reports `health.tei = "down"`, `degraded = true`.

`GET /health` → `tei` shows `active` (the endpoint currently answering), `endpoints[]` with
`usable / verified / last_ms / error`, and `cache` size.

## 4. Reranker (optional cross-encoder stage)

Recall can send its top candidates through a TEI cross-encoder reranker (`POST /rerank`, raw logits) and
blend the result into the ranking. The stage is **off unless `ASTORIA_RERANK_URLS` is set**, and degrades
to the base ranking when the endpoint is down.

| env | default | read by | meaning / when to change |
|---|---|---|---|
| `ASTORIA_RERANK_URLS` | `""` (stage off) | `core/rerank.py` | Priority list `url\|model,url\|model` of TEI reranker endpoints. The model name is informational; the endpoint is verified through `GET /info` (TEI must report `model_type.reranker` or a model id mentioning `rerank`, `minilm` or `bge`). |
| `ASTORIA_RERANK_ENABLED` | `true` | `core/rerank.py`, `retrieval/recall.py` | Kill switch for a configured stage. A per-request `rerank=false` also bypasses it. |
| `ASTORIA_RERANK_TOP_N` | `30` | `retrieval/recall.py` | How many fact candidates (by base score) are reranked; plus the top 6 episode candidates (`recall.RERANK_EPISODES`). CPU-bound: ~0.3 ms per token on a small NAS CPU; 30 facts + 6 episodes ≈ 300–350 ms cold. Raise only with a GPU reranker. |
| `ASTORIA_RERANK_WEIGHT` | `0.6` | `retrieval/recall.py` | `final = (1-w)·norm(base) + w·norm(sigmoid(logit))`, both min-max normalised over the reranked set and mapped back into the base-score range. |
| `ASTORIA_RERANK_TIMEOUT_S` | `3.0` | `core/rerank.py` | Read-path timeout: fail fast and keep the base order. |

Texts sent to the reranker are capped at 240 characters (`rerank.MAX_TEXT_CHARS`); (query, text) logits are
cached (4096 entries) so repeated ambient-memory prompts rerank for free. When every logit in the reranked
set sits within 1.0 of each other the reranker is treated as having no opinion and the base order is kept.
Failure cooldown 60 s; "not a reranker" cooldown 600 s. `GET /health` → `rerank.status` is `on` / `off` /
`down`.

The reference compose stack ships an optional `astoria-rerank` service running
`cross-encoder/ms-marco-MiniLM-L-6-v2` (22 M parameters, CPU) on port 8935; see §9.

## 5. LLM (write path only)

The LLM is called only by cognify (extraction), the curator (reflection, LLM profile narrative) and the
on-demand target resolver (`/resolve`). Recall never calls an LLM.

| env | default | read by | meaning / when to change |
|---|---|---|---|
| `ASTORIA_LLM_URL` | `http://localhost:4000/v1` | `core/llm.py` | Primary **OpenAI-compatible** chat-completions base URL (a local router, vLLM, or any `/v1/chat/completions` server). The code and `/health` refer to this route as `saint` (the router it was first built against). |
| `ASTORIA_LLM_MODEL` | `auto` | `core/llm.py`, `cognify/*` | Model name sent to the primary. Set it to whatever your gateway serves (`auto` suits routers that pick a model themselves). |
| `ASTORIA_LLM_TIMEOUT_S` | `120.0` | `core/llm.py` | Timeout for both routes. |
| `ASTORIA_LLM_FALLBACK_MODEL` | `claude-sonnet-4-6` | `core/llm.py` | Anthropic model used directly (official SDK) when the primary is unreachable **and** `ANTHROPIC_API_KEY` is set. |
| `ANTHROPIC_API_KEY` | `""` | `core/llm.py` | Enables the direct-Anthropic fallback. Without it, LLM jobs back off until the primary returns. This is the only variable read without the `ASTORIA_` prefix. |
| `ASTORIA_PROFILE_LLM` | `true` | `cognify/worker.py` → `curator.rederive_profile` | Render the profile narrative with the LLM (`profile.source='llm'`, prompt `cognify/prompts/profile.md`, sanity-checked: must mention ≥ 80 % of fact values, ≤ 2500 chars). Falls back to the deterministic template when the LLM is unavailable or the check fails. `false` = template only. |

Fallback order in `llm.chat`: primary → (connection error, timeout, HTTP error or any exception) →
Anthropic → `LLMUnavailable`. `chat_json` strips code fences and extracts the first JSON object; unparsable
output returns `None` and callers retry/back off rather than writing anything.

## 6. Retrieval

Request parameters override these per call.

| env | default | read by | meaning |
|---|---|---|---|
| `ASTORIA_RECALL_LIMIT` | `12` | `api/service.py` (`_recall`) | Default `limit` (max items after collapse) for `/recall` and the MCP `recall` tool. |
| `ASTORIA_RECALL_TOKEN_BUDGET` | `1000` | `api/service.py` (`_recall`) | Default `max_tokens` for `/recall` (≈ chars/4 of the hooks shown). Briefing's default budget is 1200 (request default). |
| `ASTORIA_RECALL_MIN_COSINE` | `0.45` | `retrieval/recall.py` | Cosine floor for vector candidates. nomic places short personal queries vs long hooks around 0.46–0.50; BM25 plus query synonyms carry the rest. A per-request `min_cosine` overrides. |
| `ASTORIA_GRAPH_MAX_DEPTH` | `2` | `retrieval/graph.py`, `store/graph.py`, `api/service.py` | Hops for graph expansion in recall and the default for `/graph` and `/edges?depth=`. `0` disables expansion. Hard cap 6. |
| `ASTORIA_GRAPH_MAX_FANOUT` | `20` | same | Strongest edges followed per node per hop (cap 200). |
| `ASTORIA_RECENCY_HALF_LIFE_DAYS` | `180.0` | `retrieval/recall.py` (`HALF_LIFE_DAYS`, refreshed on every recall) | Half-life of the recency term for **semantic** facts in recall and briefing scoring (`2^(−age/half_life)`, age from `last_seen`/`asserted_at`). Profile and procedural facts never decay in rank. |
| `ASTORIA_BELIEF_HALF_LIFE_DAYS` | `60.0` | same | Recency half-life for `is_belief` facts in recall. |
| `ASTORIA_EPISODIC_HALF_LIFE_DAYS` | `30.0` | same | Recency half-life for episodes in recall (age from `occurred_at`). |

Candidate counts are constants: 40 vector + 40 BM25 fact candidates, 20 + 20 episode candidates
(`recall.VEC_TOPN_*`, `BM25_TOPN_*`), RRF k = 60, at most 3 episodes shown; the score shape is
`rrf × (0.25 + 0.25·recency + 0.25·importance + 0.25·trust)`.

## 7. Trust and confidence

| env | default | read by | meaning |
|---|---|---|---|
| `ASTORIA_CONFIDENCE_FLOOR` | `0.05` | `store/facts.py`, `store/graph.py` | Lower clamp on any stored confidence. |
| `ASTORIA_CONFIDENCE_CAP` | `0.98` | same | Upper clamp; corroboration saturates towards it. |
| `ASTORIA_CONFIDENCE_STAGING_THRESHOLD` | `0.35` | `store/facts.py` | Extracted / imported / curator facts below this confidence land in `status='staging'` instead of `active`. |

Trust caps are code tables (`CLIENT_TRUST`, `KIND_TRUST`) and the per-kind default confidences `KIND_CONF`
in `store/facts.py` (see ARCHITECTURE.md §6).

## 8. Worker, curator and retention

| env | default | read by | meaning |
|---|---|---|---|
| `ASTORIA_WORKER_ENABLED` | `true` | `api/app.py` | Start the in-process worker loop (cognify drain, embed backfill, curator). Set `false` for API-only replicas or tests. |
| `ASTORIA_COGNIFY_POLL_S` | `30.0` | `cognify/worker.py` | Worker tick. Each tick: `embed_backfill` (200 facts + 200 episodes max), then a cognify drain. |
| `ASTORIA_COGNIFY_BATCH` | `4` | `cognify/worker.py` | Jobs claimed per tick (before coalescing by session). |
| `ASTORIA_CURATOR_INTERVAL_MIN` | `60` | `cognify/worker.py` | "Hourly" curator group: profile re-derive for users with changed profile facts + working-window turn archive. Floor 60 s. |
| `ASTORIA_REFLECT_INTERVAL_H` | `6.0` | `cognify/worker.py` | Reflection pass cadence (LLM). Floor 300 s. |
| `ASTORIA_CURATOR_DAILY_H` | `24.0` | `cognify/worker.py` | "Daily" group: `dedup_facts`, `decay`, `prune_snapshots`. Floor 3600 s. |
| `ASTORIA_WORKING_WINDOW_TURNS` | `20` | `curator.archive_old_turns` | Keep at most this many active `turn` episodes per session; older ones → `archived`. `0` disables the per-session cap. |
| `ASTORIA_WORKING_WINDOW_HOURS` | `72` | `curator.archive_old_turns` | Turns older than this leave working memory (→ `archived`). |
| `ASTORIA_DECAY_ARCHIVE_THRESHOLD` | `0.08` | `curator.decay` | Machine-sourced, never-recalled semantic facts with `decay_score` below this are archived. |
| `ASTORIA_DECAY_MIN_AGE_DAYS` | `90` | `curator.decay` | Only facts ingested longer ago than this are decay candidates. |
| `ASTORIA_DECAY_HALF_LIFE_DAYS` | `90.0` | `curator.decay_score` | Recency half-life inside the **decay** (forgetting) score for non-belief facts — deliberately shorter than the ranking half-life: an unrecalled machine fact ages out of the active set faster than it drops in rank. |
| `ASTORIA_DECAY_BELIEF_HALF_LIFE_DAYS` | `45.0` | `curator.decay_score` | Decay half-life for `is_belief` facts. |
| `ASTORIA_DEDUP_COSINE` | `0.93` | `curator.dedup_facts` | Cosine between stored value embeddings above which two active set-values of one key are merged (or normalised containment). |

Backups are not a service setting: the compose **sidecar** (`astoria-backup`) is driven by
`BACKUP_INTERVAL_S` and `BACKUP_KEEP` (compose environment, §10).

Fixed worker constants (`cognify/worker.py`): back-off 1, 5, 15, 60, 240 minutes; `max_attempts` 5 per
queue row; groups of ≤ 8 episodes / ≤ 6000 chars per LLM call; `running` rows reclaimed after 30 min;
leader advisory lock id 43. Snapshot retention 90 days (`curator.prune_snapshots`). Reflection looks back
7 days, ≤ 40 episodes / 12 000 chars, ≤ 5 insights, confidence ≤ 0.6.

## 9. Service process and logging

| env | default | read by | meaning |
|---|---|---|---|
| `ASTORIA_LOG_LEVEL` | `INFO` | `api/app.py` | Python logging level for the `astoria.*` loggers. Request log lines (JSON, stdout) are always emitted. |
| `ASTORIA_VERSION` | `0.1.0` | `api/app.py`, `/health`, `/` | Reported version string. |

The bind address is not a setting: it comes from the `uvicorn` command line (`Dockerfile` CMD: `--host
0.0.0.0 --port 8933 --workers 1`). Run exactly one uvicorn worker per database unless you want several API
processes sharing one worker leader (the advisory lock makes that safe, but only one drains the queue).

## 10. The compose stack (`deploy/nas/docker-compose.yml`)

| service | image | role | ports / volumes | env |
|---|---|---|---|---|
| `astoria-postgres` | `pgvector/pgvector:pg18` | the store | `127.0.0.1:8934 → 5432` (host-local only); `./pgdata` bind mount | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (from `.env`); `shared_buffers=256MB`, `maintenance_work_mem=128MB`, `shm_size 256m`, `mem_limit 1g` |
| `astoria` | built from the repo root `Dockerfile` (`python:3.12-slim`, non-root uid 1000) | REST + MCP + in-process worker | `8933:8933`; `./data:/data` | everything in `.env` plus `ASTORIA_DB_DSN` composed from `POSTGRES_*`; `mem_limit 768m`; waits for the database health-check |
| `astoria-backup` | `pgvector/pgvector:pg18` (so `pg_dump` matches the server major) | `pg_dump -Fc` sidecar | `./backups:/backups` | `BACKUP_INTERVAL_S` (default `21600` = 6 h), `BACKUP_KEEP` (default `14`), `PGPASSWORD` |
| `astoria-rerank` (optional) | digest-pinned `ghcr.io/huggingface/text-embeddings-inference` CPU image | TEI cross-encoder reranker | `8935:80`; `./rerank-model:/models/rerank:ro` (a Hugging Face snapshot of `cross-encoder/ms-marco-MiniLM-L-6-v2`; include `onnx/model.onnx` for the faster ORT backend) | `--max-batch-tokens 4096 --max-client-batch-size 32 --max-concurrent-requests 512 --auto-truncate`; `mem_limit 1g` |

The embedding endpoint is **not** part of this stack: point `ASTORIA_EMBED_URLS` at an existing
nomic-embed TEI (or any compatible server). The template `.env` is `deploy/nas/.env.example`; copy it
next to the compose file, fill in `POSTGRES_PASSWORD`, the endpoint URLs, `ASTORIA_USER_DEFAULT`,
optional `ASTORIA_CLIENT_TOKENS` / `ANTHROPIC_API_KEY`, and keep it out of version control.

## 11. Client-side variables (CLI, tests, benchmark)

| env | used by | meaning |
|---|---|---|
| `ASTORIA_URL` | `astoria` CLI, tests, `scripts/smoke.sh`, `scripts/bench/*`, `deploy.sh` health check | Service base URL (CLI default `http://localhost:8933`). |
| `ASTORIA_NAS_SSH`, `ASTORIA_NAS_DIR` | `deploy/nas/deploy.sh` (from the gitignored `deploy/nas/deploy.env`) | ssh host alias (default `nas`) and deploy directory (default `/opt/astoria`) for the reference tar-over-ssh deploy script. |
| `ASTORIA_TOKEN` | CLI | Bearer token sent as `Authorization: Bearer …`; maps to a client name server-side. |
| `ASTORIA_USER` | CLI | Default `user_id` for every CLI call; empty (the default) = the server applies `ASTORIA_USER_DEFAULT`. |
| `ASTORIA_DB_DSN` (or `ASTORIA_DSN`) | tests (also read from a repo-local `.env`) | Store-level tests and the "reach under the API" acceptance tests. |
| `ASTORIA_DIRECT_DB` | tests | Force (`1`) or forbid (`0`) direct-DB variants of acceptance tests. |
| `ASTORIA_RUN_SLOW` | tests | Run the `slow` scale test (T7). |
| `TEI_URL`, `BENCH_DSN`, `BENCH_DB_EXEC`, `BENCH_SSH`, `BENCH_OUT`, `BENCH_LABEL` | `scripts/bench/*` | Benchmark harness (see `scripts/bench/README.md`). |

## 12. A minimal production `.env`

```dotenv
POSTGRES_USER=astoria
POSTGRES_PASSWORD=<random>
POSTGRES_DB=astoria

ASTORIA_USER_DEFAULT=alice
ASTORIA_CLIENT_TOKENS=cli:<random>,assistant:<random>
ASTORIA_REQUIRE_TOKEN=false

# embeddings: one endpoint, or a priority list (fastest first, always-on last — same model on both!)
ASTORIA_EMBED_URL=http://embedder.local:8931
# ASTORIA_EMBED_URLS=http://gpu-box.local:4000|nomic-embed,http://embedder.local:8931|nomic
ASTORIA_EMBED_SYNC=false

# optional reranker (unset/empty = stage off)
# ASTORIA_RERANK_URLS=http://nas.local:8935|cross-encoder/ms-marco-MiniLM-L-6-v2

# LLM for cognify / curator / resolve
ASTORIA_LLM_URL=http://gateway.local:4000/v1
ASTORIA_LLM_MODEL=auto
# ANTHROPIC_API_KEY=...            # enables the direct fallback

ASTORIA_LOG_LEVEL=INFO
BACKUP_INTERVAL_S=21600
BACKUP_KEEP=14
```
