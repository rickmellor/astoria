# Astoria — architecture

Astoria is a memory service for AI agents and the humans who work with them: a network service (REST +
MCP) in front of Postgres + pgvector that stores **episodes** (raw, non-lossy captures) and **facts**
(`subject, predicate, value` triples with two time axes, assertion order, cardinality, provenance and
trust), lifts facts out of episodes with an LLM **at write time**, and answers **recall** with an LLM-free
hybrid search that returns a pre-rendered context block. This document describes the components, the
data model, the write and read paths, the trust model, the background workers, the degrade behaviour and
the honest limits of the current code. Configuration knobs are in [CONFIGURATION.md](CONFIGURATION.md);
the API in [API.md](API.md); the developer-facing interface summary in [CONTRACT.md](CONTRACT.md).

## 1. Shape in one picture

```
clients ── REST :8933 ─┐                      ┌─ embeddings: nomic-embed-text-v1.5 (768-d), any OpenAI-compatible
           MCP  /mcp/ ─┤                      │   endpoint, prioritised list, verified vector space
           CLI         │                      ├─ reranker (optional): TEI cross-encoder, POST /rerank
                       ▼                      ├─ LLM (write path only): OpenAI-compatible gateway → Anthropic fallback
        ┌──────────────────────────────┐      │
        │ astoria (one uvicorn process)│◄─────┘
        │  api/     rest · mcp_tools   │
        │           service (dispatch) │      ┌──────────────────────────────────────────────────────┐
        │  core/    capture · embed    │      │ Postgres + pgvector                                   │
        │           llm · rerank       │◄────►│  fact · episode · predicate · tombstone · profile(_history)
        │  store/   facts · episodes   │      │  snapshot · cognify_queue · audit                      │
        │           graph · db         │      │  entity · alias · edge                (graph layer)    │
        │  retrieval/ recall · graph   │      └──────────────────────────────────────────────────────┘
        │  cognify/ resolver · targets │
        │           worker (leader)    │
        │  curator/ maintenance        │
        └──────────────────────────────┘
```

One process hosts everything: FastAPI routes, the FastMCP streamable-HTTP app mounted at `/mcp/`, and an
asyncio task running the worker loop. Every REST route and every MCP tool call the same dispatcher
(`api/service.do_action`), so the two surfaces cannot drift.

## 2. Layers (what is stored where)

| layer | storage | content |
|---|---|---|
| working | `episode.kind='turn'` per `session_id` | the live session's last turns; prepended to recall as `working`, never searched |
| episodic | `episode.kind in (summary, note, import)` (+ `turn` of *other* sessions) | "what happened": summaries, notes, imports, past turns |
| semantic | `fact.layer='semantic'` | "what is true": facts about the user's world, projects, decisions |
| profile | `fact.layer='profile'` (subject == user) + `profile.narrative` | durable facts about the user themself; narrative is a display-only dual |
| procedural | `fact.layer='procedural'` | how-tos; may carry a `ref` (`{kind, ref}`) to a skill, plan or document |
| graph | `entity`, `alias`, `edge` | typed links between facts and entities; subject aliases |

## 3. Data model

Schema files live in `astoria/sql/` and are applied in lexical order at start-up, each recording itself
in `schema_migrations` (re-running is a no-op):

| file | adds |
|---|---|
| `001_schema.sql` | `predicate`, `episode`, `fact`, `tombstone`, `profile`, `profile_history`, `snapshot`, `cognify_queue`, `audit`, seed predicate vocabulary |
| `002_chain_indexes.sql` | partial b-tree indexes on `fact.supersedes` / `fact.superseded_by` (without them every hard delete scanned the table; a 100 k-row wipe went from 17+ min to 7 s) |
| `003_graph_aliases.sql` | `entity`, `alias`, `edge` |
| `004_pg_tuning.sql` | `autovacuum_*_scale_factor = 0.02` and `fillfactor = 90` on `fact` / `episode`; `0.05` on `cognify_queue` / `snapshot` |

### 3.1 `fact`

| group | columns | notes |
|---|---|---|
| identity | `id uuid`, `user_id`, `subject`, `predicate → predicate(name)`, `cardinality` (denormalised at write), `value`, `value_norm` (generated: lower-cased, whitespace-collapsed), `hook` (`"subject predicate words: value"` — what is embedded and searched), `detail`, `layer` | subject is canonical (lower-case, ≤ 64 chars; first-person forms and the user's own id map to `user_id`; aliases resolve to their canonical name) |
| search | `embedding vector(768)`, `tsv` (generated tsvector over subject + predicate words + value) | HNSW cosine index + GIN |
| valid time | `valid_from`, `valid_to` | when the fact was/is true in the world |
| assertion time | `asserted_at` | when the statement was made — **the ordering axis** (newer statement wins) |
| belief time | `ingested_at`, `expired_at` | when *we* believed it (transaction time) |
| state | `status ∈ {active, superseded, retracted, archived, staging, deleted}`, `supersedes`, `superseded_by` (self-FKs, deferrable) | the supersede chain |
| trust | `confidence` (0–1), `source` (client name), `source_kind ∈ {explicit, detector, extracted, imported, curator}`, `source_trust`, `is_belief`, `corroborations` | see §6 |
| usage | `importance`, `last_seen`, `access_count` | ranking and decay inputs |
| lineage | `origin_episode → episode(id)`, `evidence` (verbatim snippet), `ref` (procedural link), `tags`, `meta` | `meta` carries `cognify`, `conflict_with`, `version_of`, `belief_closed_by`, `merged_from`, `archived_reason`, `resolved`, … |

Two partial unique indexes are the invariant the whole store is built around: **exactly one `active` row
per functional key** `(user_id, subject, predicate)` and **one `active` row per set value**
`(user_id, subject, predicate, value_norm)`.

### 3.2 The three time axes and belief-axis versioning

- **Valid time** (`valid_from`/`valid_to`) shapes the validity window and nothing else. A fact asserted
  with a `valid_to` in the past is recorded as history (`status='superseded'`, action `historical`) and
  never disturbs the current value.
- **Assertion time** (`asserted_at`) decides conflicts on a functional key: the newer *statement* wins,
  even when it back-dates its validity ("since June it has been X"). An older statement arriving later
  (a delayed queue item, a replayed export) cannot clobber the current value — it is stored as history
  closed at the active row's start.
- **Belief time** (`ingested_at`/`expired_at`) records what the system believed when. Retraction closes
  the belief axis (`expired_at=now`) and leaves `valid_to` alone.

Supersession is **bitemporal** (this follows the supersede-don't-delete, bitemporal edge model of
Graphiti): when a new value supersedes an active functional row, the store

1. inserts a **closed copy** of the old row (same identity, `valid_from`, `asserted_at`, embedding;
   `valid_to = max(old.valid_from, new.valid_from)`, `status='superseded'`, `superseded_by=new`,
   `meta.version_of=old.id`) — this copy is the *current belief* about the past, and it is what the new
   row's `supersedes` points at;
2. closes the **original** on the belief axis only (`expired_at=now`, `status='superseded'`,
   `superseded_by=new`, `meta.belief_closed_by=copy.id`) — its `valid_to` stays as it was believed.

Consequently `as_of(at)` (current belief) returns the copy for past instants and the new row for now,
while `as_of(at, as_believed_at=B)` with `B` before the correction still returns the original — "what did
we believe at B". `history` hides belief-closed originals (rows with `meta.belief_closed_by`) so a chain
reads as one entry per statement; `include_expired` shows them.

### 3.3 `predicate` — cardinality registry

`name` (PK), `cardinality ∈ {functional, set}`, `layer_hint ∈ {semantic, profile, procedural}`, `auto`
(registered by an extractor — review it), `description`. Unknown predicates auto-register:
**functional** only for the prefixes `favorite_ default_ primary_ preferred_ current_` or the suffixes
`_is _name`; everything else is a **set** (guessing "set" can never clobber a value). The seed vocabulary
includes `name, location, timezone, employer, role, favorite_*, preferred_*, primary_*, default_*,
current_focus` (functional) and `likes, dislikes, interested_in, has_skill, knows_person, uses_tool,
owns_hardware, runs_service, works_on_project, goal, decided, fact, learned_howto, related_to` (set); edit
it through `PATCH /predicates/{name}`.

### 3.4 `tombstone` — the resurrection guard

`(user_id, subject, predicate, value_norm)` → `reason`, `by_source`, `blocks ∈ {non-explicit, none}`. A
human-initiated `retract`, `forget` or `delete` writes a tombstone with `blocks='non-explicit'`; any later
**non-explicit** write of that triple (extractor, import, curator) is refused (`action: blocked`, audit
`blocked_tombstone`). An **explicit** re-assert lifts it. Retractions made by the extractor itself or the
curator write `blocks='none'` — they are recorded but do not block. Tombstones address a failure mode
common to systems that re-extract facts from conversation history: a fact the user retracted comes back
because an old transcript still mentions it.

### 3.5 `episode`

`kind ∈ {turn, summary, note, import}`, `hook` (≤ 400 chars, what is embedded), `body`, `embedding`, `tsv`
(over hook + body), `occurred_at`, `ingested_at`, `source`, `session_id`, `importance`, `access_count`,
`last_seen`, `status ∈ {active, archived, deleted}`, `processed_at` (cognify done), `idem_key`
(`sha256(user_id|session_id|kind|body)` — a replayed capture returns the existing row with `deduped=true`),
`tags`, `meta` (turns keep `user_input` / `agent_response` here).

### 3.6 Graph layer

- `entity (user_id, name)` — canonical lower-case name (same spelling as `fact.subject`), free-form
  `kind`, `summary`. Auto-registered when an edge or alias names it.
- `alias (user_id, alias) → canonical` — flat (no chains): adding `a → b` when `b` is itself an alias of
  `c` stores `a → c`, and anything that pointed at `a` is re-pointed to `c`. Every subject-taking store
  operation canonicalises through the alias table, so a write or read on an alias lands on the canonical
  subject. The user id itself cannot be aliased.
- `edge` — `src_kind/src_id`, `dst_kind/dst_id` (kind ∈ `{fact, entity}`; fact ids are uuids, entity ids
  are names), `relation` (snake_case), `weight`, the same valid/assertion axes, `status ∈ {active,
  superseded, retracted, archived}`, `source`, `source_kind`, `confidence`, `origin_episode`, `evidence`,
  `meta`. One **active** edge per `(src, dst, relation)`; a re-assert bumps `asserted_at`, keeps the max
  weight/confidence, corroborates when it comes from a different episode *and* client. No FK to `fact` on
  purpose: hard-deleted facts leave dangling edges that readers filter.

### 3.7 Supporting tables

`profile` (one narrative per user, `version`, `rederived_at`, `source ∈ {template, llm}`) +
`profile_history`; `snapshot` (which fact/episode ids a recall returned, per session/client/query — ids
only, pruned after 90 days); `cognify_queue` (see §5); `audit` (append-only log of every control-plane
mutation: `actor`, `op`, `target`, `detail`).

## 4. Write path

### 4.1 `capture` (no LLM; `core/capture.py`)

```
text / turn ──► detect() ──► gate() ──► episode (idempotent) ──► detector apply ──► enqueue cognify
```

1. **Detector first, gate second.** `detect()` recognises a small set of explicit memory statements
   (`/remember S P V`, `/correct S P V`, `/forget S P [V]`, "my favorite X is Y", "I live in …", "my name
   is …", "I don't use/like … (anymore)", "I like …"; an initial "actually" marks a correction). A match is
   a memory operation even when it looks like a slash command, so it is never gated away.
2. **Gate.** Anything else that is empty, a slash command, a one-word acknowledgement (`ok`, `done`, `y`,
   …) or shorter than 8 characters is dropped (`dropped: <reason>`, no episode, no queue row).
3. **Episode.** Written first and durably (`store/episodes.add_episode`). With the asynchronous write path
   (`ASTORIA_EMBED_SYNC=false`, the default) the row carries `embedding NULL` and the request makes no
   embedding call; `sync=true` embeds inline.
4. **Detector apply.** Inside a savepoint (a detector failure never poisons the episode write): `remember`
   / `correct` → `facts.upsert_fact(source_kind='detector', confidence .80)`; `retract` →
   `facts.retract(source_kind='detector')` trying the alternate predicate (`likes` ↔ `uses_tool`) when the
   first finds nothing.
5. **Enqueue.** Unless `cognify=false` or the episode was a replay: one `cognify_queue` row, `priority 1`
   when `priority="high"` or the text carries a correction hint ("actually", "correction", "instead",
   "not … anymore", …), else `5`.

### 4.2 The supersede transaction (`store/facts.upsert_fact`)

Every fact write — explicit, detector, extractor, import, curator, resolver — goes through this one
function, inside a transaction:

1. canonicalise subject (user aliases, alias table), predicate (snake_case), value (whitespace); derive
   `hook`, `value_norm`, confidence (`KIND_CONF` default per `source_kind` unless given, clamped to
   `[confidence_floor, confidence_cap]`), trust (§6), layer (`profile` when subject == user and the
   predicate's `layer_hint` is `profile`, else the hint, else `semantic`);
2. `ensure_predicate` (register or, when a cardinality is passed, update the registry);
3. **embed before the lock** (a same-value re-assert is pre-checked without the lock and skips the
   embedding); then `pg_advisory_xact_lock(hash(user_id|subject|predicate))` serialises writers on the key;
4. **tombstone guard** — blocked unless `source_kind='explicit'` (which lifts the tombstone);
5. **idempotency** — same `value_norm` already active → `noop`: bump `last_seen`/`access_count`, raise
   `asserted_at` to the newer statement, upgrade `source_trust`/`source_kind` on an explicit re-assert,
   and **corroborate** (`corroborations+1`, `confidence ← 1-(1-c)·0.6`, saturating at the cap) when the
   re-assert comes from a different `origin_episode` *and* a different client;
6. **staging gate** — `extracted` / `imported` / `curator` with confidence < `0.35` → `status='staging'`;
7. **trust guard** (§6) — may downgrade to `staging` with `meta.conflict_with`;
8. **history paths** — `historical=true` or a past `valid_to` → closed row, `action: historical`; a
   functional statement **older** (by `asserted_at`) than the active one → closed row ending at the
   active row's start, `action: historical`;
9. **supersede** — for an active functional row, and for every row named in `contradicts` (any
   cardinality, same user), the bitemporal close of §3.2; then insert the new row → `action:
   superseded` (with the ids closed) or `inserted` / `staging`;
10. one `audit` row per outcome (`inserted`, `superseded`, `noop`, `historical`, `staging`,
    `conflict_staged`, `blocked_tombstone`).

`retract` closes belief (`status='retracted'`, `expired_at=now`) for a fact id or a `(subject, predicate[,
value])` key and tombstones each closed triple. `forget` archives (`soft`) or deletes (`hard`) one row and
tombstones it. `update_fact` edits `value, confidence, importance, tags, layer, valid_from, valid_to,
asserted_at, is_belief, ref, status (active|archived|staging), evidence, detail` by id; a changed value
re-renders the hook and, like every other write, embeds inline only when `ASTORIA_EMBED_SYNC=true`
(otherwise the embedding is nulled and backfilled). `approve_staging` promotes a staging row through the normal supersede path as an
explicit assertion (confidence ≥ 0.8) and archives the staging row.

### 4.3 Cognify (LLM at write; `cognify/`)

```
queue ──claim (priority, occurred_at; SKIP LOCKED)──► coalesce per (user, session) ≤ 8 episodes / 6000 chars
      ──► gather_context: ≤ 30 candidate active facts (top-20 cosine to the job text ∪ facts whose
          non-user subject appears literally in it) + predicate registry (≤ 60)
      ──► extract: ONE LLM call, strict JSON, pydantic-validated, one repair retry fed the error
      ──► apply (deterministic, same transaction): aliases → facts → edges → summary episode → mark done
```

The extractor prompt (`cognify/prompts/extract.md`; its shape — reuse known names, cite contradicted
candidate ids, dates only when stated — is adapted from Graphiti's extraction prompts) returns
`{summary, nothing_durable, facts[], edges[], aliases[]}`. Each fact is `{subject, predicate, value, layer,
is_belief, confidence (clamped to [0.3, 0.85]), valid_from, valid_to, action: assert|retract, contradicts:
[candidate ids], evidence}`.

`apply` writes in this order, all through the store functions:

- **aliases** first (`graph.add_alias`, `source_kind='extracted'`), so a rename stated in this text
  canonicalises what follows;
- **facts**: `retract` → `facts.retract(source_kind='extracted', reason='extracted-retract')` (never a
  blanket retract of a whole set without a value); `assert` → for functional keys a **near-duplicate
  guard** first (if the active value is the same thing spelled differently — normalised-equal or cosine ≥
  0.93 between the *values* — reuse the active spelling so the upsert is a corroborating `noop` instead of
  a flip-flop), then `facts.upsert_fact(source_kind='extracted', asserted_at=occurred_at, contradicts=…,
  meta.cognify={episodes, session_id})`;
- **edges**: endpoints are subject names (→ entity nodes) or `fact:N` (the N-th fact of this reply);
  `graph.add_edge(source_kind='extracted')`;
- **summary**: when the group contained turns and the model produced one, a `summary` episode
  (idempotent on its text) embedded inline, importance 0.6; the summarised turns drop to importance 0.3;
- every episode in the group gets `processed_at`; the queue rows are marked `done` with a result payload
  **in the same transaction** — a failure anywhere rolls the whole group back and backs the rows off.

### 4.4 Target resolver (LLM on demand; `cognify/targets.py`)

The sibling of the regex detector for natural-language instructions the detector cannot parse ("forget
the thing about Guinness", "actually I moved to Portland", "I don't use Emacs anymore"). `resolve` gathers
≤ 30 candidate facts (hybrid search top-20 ∪ hook ILIKE any salient token ∪ the user's own functional
facts), asks the LLM (`prompts/resolve.md`) for `{intent ∈ forget|retract|correct|remember|none, targets
[ids ⊂ candidates], new_fact, confidence, explanation}`, validates it (target ids must be candidates;
`correct`/`remember` need `new_fact`) with one repair retry, and returns the **plan without applying it**;
`requires_confirmation` is false only for `remember`, `none`, or one target with confidence ≥ 0.85.
`apply` executes a plan deterministically: `forget` → `facts.forget(soft)`, `retract` → `facts.retract`,
`correct` → `upsert_fact(source_kind='explicit', contradicts=targets)`, `remember` → `upsert_fact`. It
is reachable as `POST /resolve`, `POST /resolve/apply`, MCP `memory(action="resolve"|"resolve_apply")`
and the CLI (`astoria resolve`, `astoria forget "<text>"`). It is never on the capture hot path.

### 4.5 Curator (`curator/maintenance.py`, scheduled by the worker)

| pass | cadence | LLM | what |
|---|---|---|---|
| `embed_backfill` | every tick (30 s) before the drain | no | embeds up to 200 facts + 200 episodes with `embedding NULL` (batches of 8) — the other half of the asynchronous write path |
| `rederive_profile` | hourly group, for users whose profile-layer facts changed since the last derive | optional (`ASTORIA_PROFILE_LLM`) | rebuilds `profile.narrative` from active profile facts; LLM narrative (sanity-checked: ≥ 80 % of values mentioned, ≤ 2500 chars) or deterministic template; version + history only when the text changed |
| `archive_old_turns` | hourly group | no | working-memory window: active `turn` episodes older than `working_window_hours` or beyond the newest `working_window_turns` per session → `archived` |
| `reflect` | every `reflect_interval_h` | yes (`prompts/reflect.md`) | ≤ 5 higher-order insights over the last 7 days of unreflected summary/note episodes, written as beliefs (`source_kind='curator'`, `is_belief=true`, confidence ≤ 0.6 — the staging gate applies), `embedding NULL` (backfilled); episodes marked `meta.reflected` |
| `dedup_facts` | daily group | no | merges near-duplicate **active set values** of one key (cosine ≥ `dedup_cosine` on stored embeddings, or normalised containment): keeps the human-stated / richer / newer row, folds usage counters into it, retracts the other with reason `curator-dedup` (a non-blocking tombstone) |
| `decay` | daily group | no | archives **machine-sourced** (`extracted`, `curator`, `imported`), never-recalled (`access_count=0`) **semantic** facts older than `decay_min_age_days` whose `decay_score = importance × (1+ln(1+access_count)) × source_trust × 2^(−age/half_life)` (half-life `decay_half_life_days`, or `decay_belief_half_life_days` for beliefs) is below `decay_archive_threshold`; never explicit/detector rows, never profile/procedural |
| `prune_snapshots` | daily group | no | deletes recall snapshots older than 90 days |

Every pass is idempotent, re-verifies its targets inside the transaction right before writing, takes
`dry_run`, and returns a small report the worker logs as one line.

## 5. Worker and queue (`cognify/worker.py`)

- **Single leader.** The loop drains only while it holds `pg_try_advisory_lock(43)` on a dedicated
  connection; a second service instance (or a developer process) pointed at the same database simply idles
  on the lock. The lock is re-checked every tick and re-acquired if the connection dropped.
- **Tick** (`ASTORIA_COGNIFY_POLL_S`, 30 s; immediate on start): `embed_backfill` → `drain_once`
  (claim ≤ `cognify_batch` ready rows ordered by `priority, occurred_at` with `FOR UPDATE SKIP LOCKED`,
  reclaiming rows stuck `running` > 30 min; coalesce per `(user_id, session_id)`; process each group in
  its own transaction) → curator groups that are due (`hourly`, `reflect`, `daily`).
- **Failure.** LLM unavailable or unusable output → rows `failed`, `next_attempt_at` backed off 1 / 5 /
  15 / 60 / 240 min by attempt, `dead` after `max_attempts` (5); nothing written. Jobs whose episode was
  deleted → `skipped`. `queue_stats` (REST `/op`, `astoria queue`) shows counts by state, oldest per
  state, the last 20 dead jobs and the embedding backlog.
- Queue `kind` is normally `extract`; `rederive_profile` and `embed_backfill` kinds are routed to the
  curator when present.

## 6. Trust model

Trust is **explicit, bounded, and used for ranking and gating — never as the conflict resolver**.
Conflicts are decided by assertion order, cardinality, tombstones and the guards below.

| quantity | definition |
|---|---|
| default confidence by `source_kind` (`KIND_CONF`) | explicit .90 · detector .80 · extracted = the LLM's value clamped to [.30, .85] · imported .45 · curator .50 |
| clamps | `[confidence_floor 0.05, confidence_cap 0.98]` |
| corroboration | independent re-assert (different origin episode **and** different client): `confidence ← 1 − (1 − confidence) × 0.6` |
| `source_trust` | `min(CLIENT_TRUST[source], KIND_TRUST[source_kind], confidence)` — client caps: `cli`/`human` 1.0, `input`/`claude-code` .85, `api`/`mcp` .7, `megaplan` .6, `curator`/`anonymous` .5, `import` .4, unknown .6; kind caps: explicit 1.0, detector .9, extracted .75, curator .5, imported .45 |
| staging gate | extracted / imported / curator with confidence < `confidence_staging_threshold` (0.35) → `staging` (not recalled) until approved |
| trust guard | a machine-sourced value (`extracted`, `imported`, `curator`) **never silently supersedes** a human-stated active value (`explicit`, `detector`) on a **functional** key. It supersedes only when the writer explicitly declared `contradicts` against that row (the extractor saw the candidate and judged a real contradiction). An incidental same-key assertion lands in `staging` with `meta.conflict_with=<active id>` and an audit row `conflict_staged`. The guard yields to assertion order: an older statement takes the history path instead. |
| recall weight | `0.25 + 0.25·recency + 0.25·importance + 0.25·(confidence × source_trust)` |

Approving a staging row (`POST /approve`) re-asserts it as `explicit` with confidence ≥ 0.8 through the
normal supersede path, so functional keys stay unique.

## 7. Read path (`retrieval/recall.py`, no LLM)

```
query ─► embed (query prefix) ─► candidates ─► RRF ─► weight ─► graph expansion ─► rerank ─► collapse ─► budget ─► context
```

1. **Query embedding** with the `search_query:` prefix (documents use `search_document:`). Embedder down
   → BM25 only, `health.tei="down"`, `degraded=true`.
2. **Candidates.** Facts (layers ∩ `{profile, semantic, procedural}`, `status='active'`, valid now):
   top-40 by cosine (HNSW, `SET LOCAL hnsw.ef_search=64`, `hnsw.iterative_scan=relaxed_order` so a user
   holding a small share of a multi-user index still gets full candidates after the `user_id/status/layer`
   filter; cosine ≥ `recall_min_cosine`) ⊕ top-40 BM25 (`ts_rank_cd` over an **OR tsquery** of the query
   words expanded with a small synonym map that bridges everyday words to predicate vocabulary — `family` →
   spouse/kids/parents/pet…, `job` → role/employer…). Episodes (unless `facts_only` or `episodic` not in
   `layers`): top-20 ⊕ top-20 over `summary/note/import/turn`, excluding this session's own turns.
   With `as_of`, fact candidates come from `facts.as_of` (valid axis, optional belief axis) ranked by BM25
   only.
3. **RRF** `Σ 1/(60 + rank)` over the lists an item appears in; then `score = rrf × (0.25 + 0.25·recency +
   0.25·importance + 0.25·trust)` with recency `2^(−age/half_life)` — half-lives from settings
   (`episodic_half_life_days` 30, `recency_half_life_days` 180 for semantic facts, `belief_half_life_days`
   60), profile/procedural none; episode trust fixed at 0.6.
4. **Graph expansion** (current-time recalls only, `graph_max_depth > 0`): the top-10 fact candidates and
   the entities they are about seed a bounded walk over active edges (`retrieval/graph.expand_candidates`):
   facts reached over edges, and facts *about* reached entities (the user hub is skipped) join the pool
   with `score = min(pool score) / (1 + hops)` — always below the seeds, never a failure.
5. **Rerank** (optional, §4 of CONFIGURATION): the top-`rerank_top_n` facts + top-6 episodes by score are
   sent as `(query, hook)` pairs to the cross-encoder; `score ← blend(score, sigmoid(logit), w)`. Reranked
   items carry `rerank_score` (the raw logit). Endpoint down → base order, `health.rerank="down"`;
   `rerank=false` or stage disabled → `"off"`.
6. **Collapse**: one row per functional `(subject, predicate)` (the best), all rows for set keys.
   Episodes capped at 3.
7. **Budget**: facts first, then episodes; each item costs `len(hook)/4` tokens against `max_tokens`
   (default 1000); at most `limit` items (default 12).
8. **Stale hint** (current-time only): a selected functional fact gets `stale_hint=true` when a newer
   active episode mentions its key (FTS on the predicate words, and the subject unless it is the user)
   but not its current value.
9. **Side effects**: one `snapshot` row; `access_count`/`last_seen` bumped on what was shown (not for
   `as_of` queries).
10. **Output**: `items` (facts and episodes in one ranked list), `working` (last 4 turns of `session_id`,
    oldest first), `profile` (narrative + profile facts when `include_profile`), `context` — the
    pre-rendered block clients inject verbatim:

```
Relevant memory (current facts are authoritative; past conversation may be outdated):
- alice favorite beer: IPA  [profile · 0.90]
- alice uses tool: Neovim  [semantic · 0.84 · stale?]
- from a past session (2026-05-02): …  [episodic]
```

`briefing` is the stable, cache-friendly prefix: narrative + all active profile facts + top-10 semantic
facts by `importance × confidence × recency`, rendered as `Known about <user> (authoritative, as of
DATE):`. `search_facts_simple` (facts-only hybrid search without budget/snapshot) backs `forget` by query
and the target resolver's candidate search.

## 8. API surfaces

- **REST** (`api/rest.py`): typed routes over the dispatcher plus `POST /op {action, …}` as the raw
  mirror for scripts. `GET /docs` serves the OpenAPI UI.
- **MCP** (`api/mcp_tools.py`, FastMCP streamable HTTP at `/mcp/`): `recall`, `capture`, `remember`,
  `forget`, `memory(action=…)` and three compatibility tools (`retrieve_memory`, `add_memory`,
  `get_user_profile`). Tool docstrings are the descriptions agents read.
- **Compatibility routes** (`POST /retrieve`, `POST /memories`, `GET /users/{id}/profile`): the request
  and response shapes of an earlier memory service, so existing integrations keep working while they
  migrate to `recall`/`capture`.
- **CLI** (`cli/`): a typer client over REST; never touches the database.
- **Identity**: `Authorization: Bearer <token>` → client name via `ASTORIA_CLIENT_TOKENS`; else the
  `X-Astoria-Client` hint; else `anonymous` (MCP calls without HTTP headers → `mcp`). With
  `ASTORIA_REQUIRE_TOKEN=true`, writes without a valid token get `401`.

## 9. Process and degrade behaviour

One `uvicorn` worker process (`Dockerfile` CMD). Lifespan: enter FastMCP's session manager → `db.migrate()`
→ start the worker task → serve → stop worker (10 s grace) → close the pool. If FastMCP fails to import,
REST still comes up with MCP disabled (logged). Request log = JSON lines on stdout
`{ts, method, path, status, ms, client}`.

| failure | effect | recovery |
|---|---|---|
| embedder down / wrong model on every endpoint | new rows stored with `embedding NULL`; recall BM25-only, `health.tei="down"`, `degraded=true`; `/health.tei.ok=false` (HTTP still 200) | failed endpoints retried after 60 s (wrong model: 600 s); `embed_backfill` fills the NULLs on the next tick |
| one of several embedding endpoints down | transparent fail-over to the next in priority order | automatic within a minute of the endpoint returning |
| reranker down | base ranking, `health.rerank="down"` | automatic (60 s cooldown) |
| primary LLM down | `llm.chat` falls back to Anthropic when `ANTHROPIC_API_KEY` is set | automatic; `/health.llm.saint` reports the primary as unreachable |
| primary and fallback LLM both down | queue rows `failed` with back-off, `dead` after 5; `resolve` returns `503` with `error_kind: llm_unavailable`; profile narrative falls back to the template; reflect writes nothing | natural-language corrections are eventually consistent; structured ops and the detector are immediate |
| LLM returns unusable output | one repair retry, then `failed` (back-off) — nothing written | as above |
| Postgres down | `/health` 503 `status=error`; every action errors | compose `restart: unless-stopped` |
| worker crash | supervised task logs and exits; API keeps serving; `queue.pending` grows | restart the service |
| second service instance | it serves reads/writes but does not drain (advisory lock) | — |

## 10. Limitations and roadmap (honest)

- **Heuristic, not learned.** The regex detector covers a handful of English patterns; the BM25 synonym
  map is a small hand-written table; trust caps and decay weights are constants. They are deliberately
  simple and auditable, and they are where tuning will happen next.
- **Some retrieval shape is constant, not configurable**: candidate counts (40 ⊕ 40 facts, 20 ⊕ 20
  episodes), RRF k, the additive score shape and its 0.25 weights, the episode cap of 3. Half-lives,
  default `limit`/`max_tokens`, cosine floor, graph bounds and reranker parameters are settings
  (CONFIGURATION.md).
- **Extraction and reflection are only as good as the LLM and the prompts**; the guards (staging gate,
  trust guard, tombstones, near-duplicate guard, candidate-id validation) bound the damage, they do not
  remove the dependency.
- **Graph layer v1**: edges and aliases are written by the extractor and by hand; expansion is a bounded
  undirected walk with a hop penalty. There is no edge-level temporal reasoning in recall yet, and no
  entity summaries are generated automatically.
- **Reranker is CPU-sized by default** (22 M-parameter MiniLM, 240-char texts, top-30). The measured
  ranking gain is real but modest and costs a few hundred milliseconds cold; a GPU reranker or a larger
  model is a drop-in change of `ASTORIA_RERANK_URLS`.
- **Multi-user-ready, single-tenant-minded**: every table is keyed by `user_id` and `DELETE /users/{id}`
  wipes one, but there is no per-user authorisation — any caller may name any `user_id`.
- **Vector footprint**: ~8.6 KB per embedded row (HNSW + TOAST); the practical knee on a small host with
  a 1 GiB Postgres limit is ~200 k embedded rows (see PERFORMANCE.md §4) — raise memory, switch to
  `halfvec`, or stop embedding raw turns before then.
