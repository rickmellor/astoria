# Astoria — security posture

Astoria is designed as a **trusted-network service**: plain HTTP, identity by token or hint, no per-user
authorisation. This page states what protects it, what does not, where secrets live, what is stored, and
what leaves the host. Read it before exposing the port beyond the network you trust.

## 1. Network posture

| endpoint | binds | auth | notes |
|---|---|---|---|
| REST `:8933` + MCP `/mcp/` | `0.0.0.0` in the container, published on the host | none by default; writes token-gated with `ASTORIA_REQUIRE_TOKEN=true` | plain HTTP, no TLS |
| Postgres | `127.0.0.1:8934` on the host only (compose) | password (`POSTGRES_PASSWORD`) | reach it with `docker exec` on the host |
| reranker `:8935` (optional) | published on the host | none | read-only scorer |
| embedder, LLM gateway | external, configured by URL | as those services provide | outbound only |

Consequences on an open network: anyone who can reach `:8933` can read, write, correct, forget and wipe
memory for any `user_id` (`DELETE /users/{id}` has no confirmation); there is no rate limiting and no
TLS. With `ASTORIA_REQUIRE_TOKEN=true` writes need a token from `ASTORIA_CLIENT_TOKENS`, but reads stay
open and a token grants writes to every user. Do **not** port-forward or reverse-proxy the service to
the internet without authentication in front.

Hardening, in order of effort: turn on `ASTORIA_REQUIRE_TOKEN`; bind the published port to a LAN
address only (`127.0.0.1:8933:8933` + a reverse proxy); put TLS + authentication (basic auth, OAuth
proxy, mTLS) on that proxy; firewall the host.

## 2. Identity and trust model

- `Authorization: Bearer <token>` → looked up in `ASTORIA_CLIENT_TOKENS` (`name:token,…`) → a **proven**
  client name.
- `X-Astoria-Client: <name>` → an unauthenticated *hint*, trusted as-is (≤ 64 chars).
- Neither → `anonymous` (MCP calls without HTTP headers → `mcp`).

The client name is **provenance, not authorisation**: it becomes `fact.source`, the audit `actor` and the
`snapshot.client`, and selects a trust cap used for ranking only (`cli`/`human` 1.0 · `input`/`claude-code`
.85 · `api`/`mcp` .7 · `megaplan` .6 · `anonymous`/`curator` .5 · `import` .4 · other .6). Conflicts are
decided by assertion order, cardinality, tombstones, the staging gate and the trust guard — not by these
caps — so a spoofed hint can at most make a fact rank a little higher. With `ASTORIA_REQUIRE_TOKEN=true`
the hint no longer suffices for writes.

Tokens are opaque random strings you generate (`openssl rand -hex 24`). Rotate by editing
`ASTORIA_CLIENT_TOKENS`, recreating the service container, then updating the clients.

Every control-plane mutation is audited (`audit`: actor, op, target, detail, time) — `GET /audit`,
`astoria audit`. Reads are not audited (recall writes a `snapshot` of ids shown).

## 3. Secrets — where they live

| secret | location | notes |
|---|---|---|
| `POSTGRES_PASSWORD`, `ASTORIA_CLIENT_TOKENS`, `ANTHROPIC_API_KEY` | the stack's `.env` next to `docker-compose.yml` | read by compose `env_file`; **never commit it** (`.gitignore` excludes `.env`; `.env.example` is the template); `chmod 600` |
| client tokens on workstations | the client's own config (`ASTORIA_TOKEN` for the CLI) | mode 600 |
| backups | `./backups/*.dump` | **plaintext memory** (pg_dump custom format is compressed, not encrypted) — protect the directory and any off-site copy; encrypt at rest if the destination is shared |
| `pgdata/` | the database volume | owned by the image's postgres uid |

The service container runs as a non-root user (uid 1000); its only writable mounts are `/data` and the
database connection. Tokens are never logged (the request log records the resolved client *name*).

## 4. Data handling

- **What is stored**: facts (triples with provenance, evidence snippets, lineage), episodes (raw captured
  text — turns, summaries, notes, imports), profile narratives (and their history), recall snapshots (ids
  only), the cognify queue (job payloads + result summaries), the audit log, graph entities/edges/aliases.
- **Secrets inside captured text**: the extraction and reflection prompts forbid storing credentials; the
  target-resolver prompt treats "remember this password" as `none`. There is **no automatic scrubber on
  `capture`** — a key pasted into a captured turn is in `episode.body` until you `DELETE /episodes/{id}`
  and `forget --hard` any fact derived from it. Clients that capture should filter what they send.
- **Logs**: the JSON request log records method, path, status, latency and client — no bodies; uvicorn's
  access log includes query strings (`/facts?q=…`). Docker log rotation caps each container at ~30 MB.
- **Deletion semantics**: `forget --hard` / `DELETE /facts/{id}?mode=hard` removes the row; `DELETE
  /users/{id}` removes everything for one user; `DELETE /episodes/{id}` removes an episode (queue rows
  cascade, facts keep a nulled lineage). Soft forgets, retracts and supersedes keep rows for history and
  audit. Backups keep deleted data until they rotate out (`BACKUP_KEEP × BACKUP_INTERVAL_S`, default ≈
  3.5 days); off-site copies keep it as long as you keep them.
- **Multi-tenancy**: every table is keyed by `user_id`, and every action is scoped to the `user_id` it is
  given — but any caller may name any `user_id`. Treat one Astoria instance as one trust domain.

## 5. What leaves the host (no telemetry)

Astoria makes exactly these outbound calls:

1. **Embedding endpoints** (`ASTORIA_EMBED_URLS`): fact/episode hooks and recall queries (with the
   `search_document:` / `search_query:` prefixes), plus a fixed canary sentence for verification.
2. **Reranker endpoints** (`ASTORIA_RERANK_URLS`): the recall query and up to ~36 candidate hooks
   (truncated to 240 chars).
3. **Primary LLM gateway** (`ASTORIA_LLM_URL`): extraction prompts (the coalesced episode text, ≤ 6000
   chars, plus up to 30 candidate facts and the predicate registry), reflection prompts (recent summaries
   and notes, ≤ 12 000 chars, plus up to 60 known facts), profile-narrative prompts (profile facts), and
   target-resolver prompts (the instruction plus up to 30 candidate facts).
4. **`api.anthropic.com`** — only as the LLM fallback when the primary is unreachable and
   `ANTHROPIC_API_KEY` is set; same payloads as 3.

Recall, briefing, history, as-of and all CRUD are local SQL plus (1) and (2). There are no usage
metrics, crash reports, update checks or third-party SDK telemetry. If your embedding/rerank/LLM endpoints
are on your own network, nothing leaves it unless you configure the Anthropic fallback.

## 6. Supply chain

- Images: `pgvector/pgvector:pg18` (tag — pin a digest if you need reproducibility); the reranker uses a
  digest-pinned TEI image; the service image is built from `python:3.12-slim` with the dependencies in
  `pyproject.toml` (FastAPI, uvicorn, FastMCP `>=2.3,<3`, psycopg 3, pgvector, httpx, pydantic, typer,
  rich, python-dateutil, anthropic SDK).
- The extraction prompt's *shape* is adapted from Graphiti (Apache-2.0; attributed in the prompt file);
  no third-party memory code runs at runtime.
- Astoria itself is MIT licensed (`LICENSE`).
