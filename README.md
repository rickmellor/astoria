# Astoria

**Astoria** is a memory service for AI agents: a network service (REST + MCP + CLI) in front of Postgres +
pgvector that gives every assistant you run one shared, correctable, auditable long-term memory. It stores
what happened (episodes) and what is true (facts) with provenance, confidence and two time axes; it lifts
facts out of conversations with an LLM **at write time**, and it answers recall with an LLM-free hybrid
search that returns a ready-to-inject context block.

Astoria exists because the usual failure modes of agent memory are requirements here: facts you can
**correct and delete**, corrections that **propagate** and **never resurrect**, **provenance and
confidence** on every fact, **"what was true when"**, and a **deterministic control plane** that never
waits on a model.

```
clients ──REST :8933 / MCP /mcp/ / CLI──►  astoria (FastAPI + FastMCP + in-process worker)
                                             │ control plane (no LLM): recall · capture · remember · correct · retract · forget · as_of · graph
                                             │ cognify (LLM at WRITE): extract → resolve → supersede / contradict / edges / aliases
                                             │ curator: embed backfill · profile · reflect · dedup · decay · retention
                                             ▼
                                        Postgres + pgvector (facts · episodes · tombstones · graph · snapshots · queue · audit)
                                        embeddings: nomic-embed-text-v1.5 (768-d) via any OpenAI-compatible endpoint(s)
                                        optional: TEI cross-encoder reranker · OpenAI-compatible LLM gateway → Anthropic fallback
```

## The memory model in a minute

| layer | what | where |
|---|---|---|
| working | the live session's last turns | `episode.kind='turn'` per `session_id` |
| episodic | session summaries, notes, imports, past turns | `episode` |
| semantic · profile · procedural | `(subject, predicate, value)` facts | `fact.layer` |
| graph | typed links between facts and entities; subject aliases | `edge`, `entity`, `alias` |

- **Facts are bitemporal and assertion-ordered.** `valid_from/valid_to` say when something was true in the
  world; `asserted_at` orders statements (the newer statement wins, even if it back-dates its validity);
  `ingested_at/expired_at` say when the system believed it. A correction **supersedes** — the old row is
  closed on the belief axis and a closed copy carries the corrected `valid_to`, so `as_of(at)` answers
  the present belief about the past and `as_of(at, as_believed_at=B)` answers what was believed at `B`.
  Nothing is overwritten; `history` shows the chain.
- **Predicates have cardinality.** `favorite_beer` is *functional* (one current value → supersede);
  `likes` is a *set* (many values → add/retract). Unknown predicates auto-register, safely, as sets.
- **Trust is explicit and bounded.** `confidence` (by how a fact was stated or extracted, saturating with
  independent corroboration) × `source_trust` (capped by client and by kind: explicit > detector >
  extracted > curator > import). It ranks; it never decides conflicts. Low-confidence extractions land in
  **staging**; a machine-extracted value never silently overrides a human-stated one on a functional key
  (**trust guard**) — it is staged as a flagged conflict unless the extractor declared the contradiction.
- **Tombstones** stop a retracted or forgotten fact from being re-learned out of old conversations — the
  resurrection failure mode seen in systems that re-extract from history. An explicit re-assert lifts them.
- **LLM only at write, and never in the way.** `capture` stores the raw episode immediately and returns;
  a queued *cognify* job extracts facts, contradictions, edges and aliases later (with back-off, a dead
  letter queue, and a deterministic apply step). `recall` is pure search: pgvector cosine ⊕ BM25 with query
  synonyms → reciprocal-rank fusion → recency × importance × trust → graph expansion → optional cross-encoder
  rerank → collapse → token budget → a `context` block every client injects verbatim.
- **Natural language is resolved, then executed deterministically.** A regex detector applies obvious
  statements instantly ("actually my favorite beer is IPA"); the LLM **target resolver** turns vaguer
  instructions ("forget the beer stuff") into a plan — which facts, which operation — that you confirm and
  the store applies.

## Features

- Bitemporal facts with belief-axis versioning, assertion-order conflict resolution, cardinality,
  idempotent writes with corroboration, tombstones, staging gate, trust guard, full audit log.
- Hybrid recall: HNSW cosine ⊕ BM25 (+ synonym expansion), RRF, recency/importance/trust weighting,
  bounded graph expansion, optional TEI cross-encoder rerank, collapse + token budget, stale hints,
  working memory, profile narrative, cache-friendly `briefing`, time travel (`as_of`, `as_believed_at`).
- Asynchronous write path: captures and fact writes return without waiting on the embedder; a worker
  backfills embeddings within a tick. Prioritised, verified, fail-over embedding endpoints.
- Cognify: Graphiti-shaped extraction prompt, pydantic-validated output with a repair retry, near-duplicate
  guard on functional values, contradiction-driven supersede, extracted edges and aliases, session
  summaries, leader-elected in-process worker with coalescing, back-off and dead letters.
- Curator: embed backfill, working-memory window, profile re-derive (template or sanity-checked LLM
  narrative), reflection into low-confidence beliefs, set-value dedup, decay of unused machine facts,
  snapshot retention.
- Target resolver: natural-language forget / retract / correct / remember → plan → confirm → apply.
- Surfaces: REST (typed routes + `/op` dispatcher), MCP tools (`recall`, `capture`, `remember`, `forget`,
  `memory(action=…)`), a full CLI (`astoria`), compatibility routes for an earlier memory-service API,
  per-client tokens (optional write gating), per-user wipe, export/import.
- Degrades, never fails: embedder down → BM25-only; reranker down → base ranking; LLM down → fallback,
  then back-off; everything reports through `/health`.

## Quick start

```bash
# 1. service (docker compose)
cp deploy/nas/.env.example deploy/nas/.env   # set POSTGRES_PASSWORD, ASTORIA_EMBED_URLS, ASTORIA_LLM_URL/MODEL, ASTORIA_USER_DEFAULT
docker compose -f deploy/nas/docker-compose.yml up -d --build astoria-postgres astoria astoria-backup
#   (add `astoria-rerank` once deploy/nas/rerank-model holds a cross-encoder snapshot — optional)
curl -s http://localhost:8933/health | jq .status        # "ok"; OpenAPI at /docs; MCP at /mcp/

# 2. CLI (anywhere that can reach the service)
pip install .                                            # console script `astoria`
export ASTORIA_URL=http://nas.local:8933 ASTORIA_USER=alice   # + ASTORIA_TOKEN for attributed writes
astoria remember alice favorite_beer IPA
astoria correct  alice favorite_beer Pilsner             # supersedes, keeps history
astoria history  alice favorite_beer
astoria as-of 2026-07-01 --predicate favorite_beer
astoria recall "what beer do I like"
astoria forget "the thing about Pilsner"                 # LLM resolves the targets, you confirm

# 3. agents: point an MCP client at http://nas.local:8933/mcp/ and call recall / capture / remember / forget
```

You need a **nomic-embed-text-v1.5** endpoint (Hugging Face TEI, vLLM, llama.cpp server — anything
OpenAI-compatible) and, for extraction, an OpenAI-compatible chat endpoint and/or an Anthropic key. Without
an LLM, Astoria still captures, detects explicit statements, stores facts and recalls; extraction waits.

## Layout

```
astoria/
  config.py              every setting (ASTORIA_* env)
  sql/                   001 schema · 002 chain indexes · 003 graph + aliases · 004 pg tuning (auto-applied)
  store/    db.py · facts.py (the supersede transaction) · episodes.py · graph.py
  core/     embed.py (endpoints, verification) · rerank.py · llm.py · capture.py (gate + detector + enqueue)
  retrieval/ recall.py (hybrid recall, briefing, context) · graph.py (expansion, /graph)
  cognify/  prompts/ (extract, resolve, profile, reflect) · resolver.py · targets.py · worker.py
  curator/  maintenance.py
  api/      app.py · rest.py · mcp_tools.py · service.py (one dispatcher) · auth.py
  cli/      main.py (typer) · client.py · render.py
deploy/nas/   docker-compose.yml · deploy.sh · .env.example
scripts/      smoke.sh · bench/ (scale + rerank harness)
tests/        unit · concurrency · acceptance (T1–T12) · belief axis · graph · targets · rerank
docs/         see below
```

## Documentation

| doc | what |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data model (tables, the three time axes, belief-axis versioning, cardinality, tombstones, graph), write path (capture → detector → cognify), read path, trust model, curator, worker, degrade matrix, limitations |
| [docs/API.md](docs/API.md) | every REST route with request/response JSON and curl, MCP tools and the full `memory(action=…)` list, auth, compat routes, error shapes |
| [docs/CLI.md](docs/CLI.md) | setup, workflows (resolve/forget, history/as-of/belief axis, graph/alias, staging/approve, export/import), full `--help` of every command |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | every environment variable with default and meaning, the compose services, embedding/reranker endpoint lists, LLM primary/fallback, tokens |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | deploy/upgrade, backups and restore, health and queue inspection, logs, capacity knobs, troubleshooting, tests |
| [docs/SECURITY.md](docs/SECURITY.md) | network posture, tokens and trust, secrets, data handling, outbound calls, no telemetry |
| [docs/CONTRACT.md](docs/CONTRACT.md) | the developer-facing interface summary: module signatures, routes, tools, algorithm defaults |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | measured latency/throughput at 150 k rows, reranker evaluation, growth projection, scaling risks (`scripts/bench/`) |
| [CHANGELOG.md](CHANGELOG.md) | what changed, by version |

## License

MIT — see `LICENSE`. The extraction prompt's shape is adapted from Graphiti (Apache-2.0), attributed in
the prompt file.
