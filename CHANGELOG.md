# Changelog

Astoria follows semantic versioning once it reaches 1.0; until then minor versions may change interfaces
documented in `docs/CONTRACT.md`. Entries are feature-level; see git history for detail.

## Unreleased (0.1.x, feature-complete wave)

### Added
- **Reranker stage**: optional TEI cross-encoder (`POST /rerank`) over the top-N recall candidates with
  prioritised endpoints, served-model verification, cooldowns, logit blending, per-request `rerank=false`,
  `health.rerank` (`on | off | down`) and a `/health.rerank` block.
- **Asynchronous write path**: `capture` and `POST /facts` store rows with `embedding NULL` and return
  without an embedding call (`ASTORIA_EMBED_SYNC=false`, per-request `sync=true`); the worker's
  `embed_backfill` runs every tick; `queue_stats` reports the embedding backlog.
- **Graph layer** (schema 003): `entity`, `alias`, `edge` tables; alias-aware subject canonicalisation in
  every store operation; bounded graph expansion in recall; `GET /graph`, `/edges` (list/add/delete),
  `/aliases` (list/add/delete); MCP `memory(action=graph|edges|edge_add|edge_delete|aliases|alias_add|alias_delete)`;
  CLI `graph`, `edges`, `edge add/rm`, `alias add/list/rm`; the extractor may emit `edges` and `aliases`.
- **Target resolver**: `POST /resolve` (plan only) and `POST /resolve/apply`; MCP
  `memory(action=resolve|resolve_apply)`; CLI `resolve` and natural-language `forget` with preview and
  confirmation, falling back to literal matching when the LLM is unavailable.
- **Curator v2**: `dedup_facts` (near-duplicate set values), `decay` (unused machine-sourced semantic
  facts), `reflect` (LLM insights as low-confidence beliefs), LLM profile narrative with sanity check and
  template fallback, configurable working-memory window (turns/hours), scheduled groups (hourly /
  reflect / daily) driven by settings.
- **Belief-axis versioning**: supersede closes the original row on the belief axis and inserts a closed
  copy carrying the corrected `valid_to`; `as_of(at, as_believed_at)` answers "what did we believe then";
  `history` hides belief-closed originals (`include_expired` shows them).
- **Trust guard**: machine-sourced values never silently supersede a human-stated value on a functional
  key; without a declared contradiction they land in `staging` with `meta.conflict_with` (audit
  `conflict_staged`).
- **BM25 query-synonym expansion** and a cosine floor of 0.45 for recall.
- **Prioritised, verified embedding endpoints** (`ASTORIA_EMBED_URLS`): served-model assertion, canary
  vector-space check, 60 s cooldown, client-side L2 normalisation, LRU cache; `/health.tei` per-endpoint
  status.
- `ASTORIA_REQUIRE_TOKEN`: writes require a bearer token (reads stay open); `queue_stats` action
  (by state, oldest, dead jobs, embed backlog).
- Schema 002 (indexes on the supersede chain; mass delete 17+ min → 7 s at 100 k rows) and schema 004
  (autovacuum scale factors, fillfactor 90 on hot tables).
- `hnsw.iterative_scan = relaxed_order` per recall (filtered-HNSW candidate starvation fix for minority
  users in a shared index); embedding moved before the per-key advisory lock (concurrent corrections on
  one key no longer serialise on the embedder).
- Benchmark harness (`scripts/bench/`), scale validation document, rerank quality evaluation script.
- Tests: belief axis, graph, targets, rerank, curator, concurrency; acceptance suite T1–T12.

### Changed
- Working-memory window default 72 h / 20 turns per session (was 14 days).
- Recall reports `health.rerank` alongside `health.tei` / `degraded`.

## 0.1.0

- Initial release: bitemporal, assertion-ordered, cardinality-aware fact store with tombstones, staging gate
  and audit; idempotent episode capture with gate, regex detector and cognify queue; hybrid recall (cosine ⊕
  BM25 → RRF → recency/importance/trust → collapse → budget → context), briefing, stale hints, snapshots;
  cognify resolver (Graphiti-shaped extraction prompt, validated JSON, repair retry) with an in-process
  leader-elected worker (coalescing, back-off, dead letters); curator (embed backfill, template profile,
  prune/archive); REST + MCP surfaces over one dispatcher, compatibility routes, per-client tokens; typer
  CLI covering every control-plane operation plus export/import; compose deployment with a pg_dump sidecar;
  smoke and seed scripts; acceptance suite.
