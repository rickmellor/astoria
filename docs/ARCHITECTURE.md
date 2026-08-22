# Astoria — architecture

How the memory service is built and why it behaves the way it does. The interface contract is
[CONTRACT.md](CONTRACT.md); operating it is [OPERATIONS.md](OPERATIONS.md); the HTTP/MCP surface is
[API.md](API.md); numbers are in [PERFORMANCE.md](PERFORMANCE.md). Design rationale and the expert
reviews that shaped it live in `~/projects/infrastructure/astoria/DESIGN.md` (§1, §4, §16, §17).

Astoria is **live** on the NAS since 2026-08-22 (`http://192.168.1.134:8933`, MCP at `/mcp/`) and
replaces MemoryOS (and, on the workstation, Turnstone and Hermes as memory surfaces).

---

## 1. Shape in one picture

```
                 input · Claude Code hooks/MCP · MegaPlan · astoria CLI · Nova (future)
                                  │  REST :8933   │  MCP streamable-HTTP /mcp/
                                  ▼               ▼
 ┌──────────────────────────── astoria (one uvicorn process, container `astoria`) ───────────────────────────┐
 │  api/rest.py  ──┐                                                                                         │
 │  api/mcp_tools  ├──► api/service.do_action()  ── the ONE dispatcher (auth → txn → store → JSON)           │
 │  api/app.py   ──┘          │                                                                              │
 │                            ├─ control plane (NO LLM): facts.upsert/retract/forget/update/approve/as_of    │
 │                            ├─ capture (NO LLM): gate → episode → regex detector → cognify queue           │
 │                            └─ recall (NO LLM): pgvector cosine ⊕ BM25 → RRF → weights → budget → context  │
 │  cognify/worker.py  (asyncio task, single leader via pg_try_advisory_lock(43))                            │
 │        every 30 s: drain queue → resolver.extract (LLM) → resolver.apply (facts.* again, deterministic)   │
 │        +15 min embed backfill · +6 h profile re-derive · +24 h snapshot prune / turn archive             │
 └───────────────┬──────────────────────────────┬──────────────────────────────┬───────────────────────────┘
                 │ psycopg pool                 │ HTTP                         │ HTTP (write path only)
                 ▼                              ▼                              ▼
   astoria-postgres (pgvector/pgvector:pg18)   memoryos-tei :8931          SAINT :4000 (workstation, off
   127.0.0.1:8934 on the NAS only              nomic-embed-text-v1.5       nightly) → fallback: direct
   fact · episode · tombstone · predicate ·    768-d, pinned              Anthropic (claude-sonnet-4-6)
   profile(+history) · snapshot · cognify_queue · audit · schema_migrations
                 ▲
   astoria-backup sidecar: pg_dump -Fc every 6 h, keep 14 → /volume1/docker/astoria/backups
```

Three invariants fall out of this shape:

1. **LLM only at write, never at read.** `recall`, `briefing`, every CRUD op and every compat route are
   pure SQL + one embedding call. The LLM is consulted only by the background cognify worker, and even
   then it only *proposes* triples — `facts.upsert_fact` / `facts.retract` decide.
2. **Episodes first, durably.** `capture` writes the raw episode and returns; extraction is a queued job.
   Nothing the user said is lost when SAINT, the cloud or TEI is down.
3. **One dispatcher.** REST routes, `/op`, and every MCP tool call `service.do_action(action, params,
   client)`. The two surfaces cannot drift; the caller identity resolved once becomes the fact `source`
   and the audit `actor`.

## 2. Layers (what is stored where)

| layer | storage | what it holds | recalled how |
|---|---|---|---|
| working | `episode.kind='turn'` keyed by `session_id` | the live session's raw turns (`User:`/`Assistant:` body, `meta.user_input/agent_response`) | last 4 turns of the given `session_id`, **prepended** to recall as `working`, never searched |
| episodic | `episode.kind ∈ {summary, note, import}` (+ other sessions' `turn`s) | session summaries, free notes, imports | hybrid search, ≤ 3 per recall, rendered as "from a past session (DATE)" |
| semantic | `fact.layer='semantic'` | `(subject, predicate, value)` about the world: tools, hardware, projects, decisions | hybrid search |
| profile | `fact.layer='profile'` (subject == user_id and predicate `layer_hint=profile`) | identity, preferences, traits | hybrid search; all of them in `/briefing` and `/profile`; recency weight = 1 (never decays) |
| procedural | `fact.layer='procedural'` | how-tos (`learned_howto`), may carry `ref {kind: skill|megaplan|infra-doc|url, ref}` | hybrid search; recency weight = 1 |

The **profile narrative** (`profile.narrative`) is a *display-only* dual of the profile facts: a
deterministic template rendered by the curator (`curator.render_profile_narrative`), versioned in
`profile_history`, never embedded or searched — so a bad rewrite can never poison recall.

## 3. Data model

Schema: [`astoria/sql/001_schema.sql`](../astoria/sql/001_schema.sql) (applied idempotently at boot,
recorded in `schema_migrations`). Postgres 18 + pgvector 0.8.x + pgcrypto.

### 3.1 `fact` — the heart

```
(id, user_id, subject, predicate, cardinality, value, value_norm*, hook, detail, embedding vector(768), tsv*, layer,
 valid_from, valid_to,            -- VALID time: when it was true in the world
 asserted_at,                     -- ASSERTION time: orders statements (newer statement wins)
 ingested_at, expired_at,         -- BELIEF/transaction time: when WE believed it
 status ∈ {active, superseded, retracted, archived, staging, deleted},
 supersedes → fact, superseded_by → fact   (DEFERRABLE INITIALLY DEFERRED),
 confidence, source, source_kind ∈ {explicit, detector, extracted, imported, curator}, source_trust, is_belief,
 importance, last_seen, access_count, corroborations, tags[], origin_episode → episode, evidence, ref jsonb, meta jsonb)
 * generated columns: value_norm = lower(btrim(collapse-ws(value))); tsv = to_tsvector(subject + predicate words + value)
```

Key facts about facts:

- **`hook`** = `"subject predicate words: value"` — the string that is embedded, BM25-indexed and
  rendered. `detail` is free text that is not searched.
- **Subject canonicalization** (`facts.canon_subject`): `I / me / my / myself / user / the user / owner /
  self / you / rick` (and the literal user_id) → the `user_id`; anything else lower-cased, ≤ 64 chars.
  **Predicates** (`canon_predicate`) → `snake_case`, `[a-z0-9_]`, ≤ 64, default `fact`.
- **Three time axes** (the bitemporal model, plus assertion order):
  - *valid* (`valid_from`, `valid_to`): the real-world window. `valid_from` defaults to `asserted_at`.
  - *asserted* (`asserted_at`): when the statement was made. **This is the ordering axis** — a newer
    statement supersedes an older one *even if it back-dates its validity* ("I've preferred IPA since
    June" stated in August beats a July "Guinness"). An older-asserted statement arriving late (queue
    reorder, replay) is stored as history and cannot clobber the active row. The REST API cannot set
    `asserted_at` on insert (it is `now()`); only the store/resolver back-dates it to the episode's
    `occurred_at`.
  - *belief* (`ingested_at`, `expired_at`): when the system started / stopped believing the row.
    Retract/forget/supersede stamp `expired_at`.
- **Cardinality** is denormalized onto the row from the `predicate` registry at write time and enforced
  by two partial unique indexes: at most **one active row per `(user, subject, predicate)`** for
  `functional` keys; at most **one active row per `(user, subject, predicate, value_norm)`** for `set`
  keys. `functional` = one current value (`favorite_beer`, `location`, `default_model`) → a new value
  *supersedes*; `set` = many values coexist (`likes`, `uses_tool`, `owns_hardware`) → add members,
  `retract` removes one. Unknown predicates auto-register (`predicate.auto=true`): functional iff prefix
  `favorite_|default_|primary_|preferred_|current_` or suffix `_is|_name`, else `set` (the safe guess —
  never clobbers by accident). `PATCH /predicates/{name}` fixes a wrong guess and clears `auto`.
- **Status lifecycle**
  - `active` — current belief, recalled.
  - `staging` — low-confidence non-explicit extraction (confidence < 0.35); listed, not recalled;
    `approve` promotes it through the normal supersede path.
  - `superseded` — closed by a newer value (`superseded_by` set, `valid_to` closed, `expired_at` set) or
    stored as explicit history (`historical=true` / a past `valid_to`).
  - `retracted` — "we no longer believe this"; `expired_at` set, `valid_to` untouched.
  - `archived` — soft-forgotten (hidden from recall, still auditable) or an aged-out turn.
  - `deleted` — reserved; hard forget actually `DELETE`s the row.
- **Trust numbers** (bounded heuristics for *ranking*, never the conflict resolver — assertion order is):

  | | |
  |---|---|
  | `confidence` default by `source_kind` | explicit **.90** · detector **.80** · extracted = LLM value clamped **[.30, .85]** · imported **.45** · curator **.50** (clamped to `[0.05, 0.98]`) |
  | corroboration | an *independent* re-assert (different `origin_episode` **and** different client) bumps `corroborations` and `conf = 1 − (1 − conf)·0.6` (saturating); same-source repeats only bump `last_seen`/`access_count` |
  | client trust cap (`CLIENT_TRUST`) | cli 1.0 · human 1.0 · input .85 · claude-code .85 · api .7 · mcp .7 · megaplan .6 · curator .5 · anonymous .5 · import .4 · unknown names .6 |
  | kind trust cap (`KIND_TRUST`) | explicit 1.0 · detector .9 · extracted .75 · curator .5 · imported .45 |
  | `source_trust` | `min(CLIENT_TRUST[source], KIND_TRUST[source_kind], confidence)` |
  | staging gate | `source_kind ∈ {extracted, imported, curator}` and confidence < **0.35** → `status='staging'` |
  | recall weight | `trust = confidence × source_trust` (one of four equal-weight terms, see §5) |

### 3.2 `tombstone` — the resurrection guard

`(user_id, subject, predicate, value_norm) → reason, by_source, blocks ∈ {non-explicit, none}`.
A human **retract** (explicit or detector), **forget** (soft or hard) or delete writes a tombstone with
`blocks='non-explicit'`. `upsert_fact` checks it first: a non-explicit write (extracted / imported /
curator) of the same triple is **blocked** (`action: "blocked"`, audited as `blocked_tombstone`) — this is
what stops an old conversation from re-extracting "Guinness" after you corrected it. An **explicit**
re-assert lifts the tombstone. Retracts proposed by the LLM (`source_kind=extracted`) write
`blocks='none'` — they never block a human.

### 3.3 `episode` — non-lossy raw captures

`(id, user_id, kind ∈ {turn, summary, note, import}, hook (≤ 400 chars — what is embedded), body, embedding,
tsv*, occurred_at, ingested_at, source, session_id, importance, access_count, last_seen, status ∈ {active,
archived, deleted}, processed_at, idem_key UNIQUE, tags[], meta)`.
`body` is the text, or `"User: …\nAssistant: …"` for a turn (also kept in `meta.user_input/agent_response`).
`idem_key = sha256(user_id|session_id|kind|body)` makes re-sending the same content a no-op
(`deduped: true`, no second cognify job). `processed_at` is stamped when cognify finishes.

### 3.4 Supporting tables

- `predicate` — the registry (name, cardinality, layer_hint, auto, description); 30 seeded names.
- `profile` / `profile_history` — narrative, version, `rederived_at`, `source ∈ {template, llm}` (v1 only writes `template`).
- `snapshot` — one row per recall: `(user_id, session_id, client, query, fact_ids[], episode_ids[])`, pruned after 90 days. "What was this session shown?"
- `cognify_queue` — `(episode_id, session_id, kind ∈ {extract, rederive_profile, embed_backfill}, priority (1 = corrections first, 5 normal), state ∈ {pending, running, done, failed, dead, skipped}, attempts, max_attempts=5, next_attempt_at, last_error, payload)`.
- `audit` — append-only: every control-plane mutation `(user_id, actor, op, target, detail)`; ops include `inserted | superseded | noop | historical | staging | blocked_tombstone | retract | forget_soft | forget_hard | update | approve | episode_delete | predicate_update | user_wipe`.
- `schema_migrations` — applied SQL versions.

## 4. Write path

### 4.1 `capture` (no LLM; `astoria/core/capture.py`)

```
text (or user_input+agent_response)
  → detect()   regex v1: /remember S P V · /correct S P V · /forget S P [V] ·
               "my (favorite|default|preferred|primary|current) X is Y" · "I (live|am based) in Y" ·
               "my name is Y" · "I (don't|no longer) (like|use|prefer) Y" · "I (like|love|enjoy) Y"
               (a leading "actually" turns remember into correct)
  → gate()     drop if: empty · starts with /word (unless the detector matched) · an ack
               (ok/okay/done/y/n/yes/no/thanks/thank you/continue/k/sure) · < 8 chars      → {dropped: reason}
  → episodes.add_episode()   embed hook via TEI (None if TEI down), idem_key dedupe
  → detector apply (in a SAVEPOINT, so a failure can't poison the episode write):
        remember/correct → facts.upsert_fact(source_kind='detector', confidence .80, origin_episode, evidence)
        retract          → facts.retract(source_kind='detector')  (tries likes→uses_tool / uses_tool→likes)
  → enqueue cognify (unless deduped or cognify=false): priority 1 if priority="high" or the text
        contains a correction hint (actually / correction / i meant / instead / no longer / "not … anymore"), else 5
  → {episode_id, deduped, dropped, detector, queued}
```

Structured writes (`POST /facts`, `/correct`, MCP `remember`, CLI `remember`) skip all of this and go
straight to `facts.upsert_fact(source_kind='explicit', confidence .90)`.

### 4.2 The supersede transaction (`facts.upsert_fact`)

Runs inside one DB transaction, serialized per key by `pg_advisory_xact_lock(hash(user|subject|predicate))`:

1. canonicalize subject/predicate/value; compute `hook`, `value_norm`, confidence, `source_trust`;
   `ensure_predicate` (auto-register / apply an explicit cardinality override).
2. **tombstone guard** — explicit lifts it; non-explicit is `blocked`.
3. load the active row for the key (functional) or for the key+value (set).
4. **idempotent no-op** — same `value_norm` already active → bump `last_seen`, `access_count`,
   corroborate if independent, upgrade `source_kind`/`source_trust` on an explicit re-assert,
   `asserted_at = GREATEST(old, new)`; `action: "noop"`.
5. **staging gate** (non-explicit, confidence < .35).
6. **explicit history** (`historical=true` or a `valid_to` in the past) → insert as `superseded`,
   touch nothing; `action: "historical"`.
7. **assertion-order guard** — functional key and the new `asserted_at` is *older* than the active row's
   → insert as `superseded` (closed at the active row's start); `action: "historical"`.
8. otherwise **close then insert**: the old active functional row (and any `contradicts` ids named by the
   resolver) get `status='superseded', valid_to = GREATEST(valid_from, new.valid_from), expired_at=now(),
   superseded_by=new`; the new row is inserted with `supersedes=old` and embedded. `action: "superseded"`
   or `"inserted"` (or `"staging"`).
9. audit row.

`retract` → `status='retracted', expired_at=now()` (valid window untouched) + tombstone.
`forget` soft → `status='archived', expired_at` + tombstone; hard → `DELETE` + tombstone.
`approve_staging` re-runs `upsert_fact` as explicit (so functional uniqueness is honoured) and archives
the staging row. `update_fact` edits `value | confidence | importance | tags | layer | valid_from |
valid_to | asserted_at | is_belief | ref | status (active|archived|staging) | evidence | detail` by id
(re-embeds when the value changes).

### 4.3 Cognify (LLM at write; `astoria/cognify/`)

`worker.run_forever` (an asyncio task started in the app lifespan) ticks every **30 s**:

- **Leader lock**: the loop only drains while it holds `pg_try_advisory_lock(43)` on a dedicated
  autocommit connection — one drainer even if two containers were ever started.
- **Claim**: up to `ASTORIA_COGNIFY_BATCH` (4) ready jobs (`pending|failed`, `next_attempt_at <= now()`)
  ordered by `(priority, occurred_at)`, `FOR UPDATE SKIP LOCKED`, `state='running'`, `attempts+1`. Rows
  stuck in `running` for > 30 min are reclaimed as `failed` (crash recovery).
- **Coalesce** by `(user_id, session_id)`, splitting a group at 8 episodes / 6 000 chars (bodies are
  truncated at 6 000 chars) — one LLM call per conversation chunk, not per turn.
- **Extract** (`resolver.extract`): prompt [`prompts/extract.md`](../astoria/cognify/prompts/extract.md)
  (Graphiti-shaped: reuse candidate ids, `contradicts`, dates only when stated) + job text with
  timestamps + ≤ 30 candidate active facts (top-20 cosine + literal subject matches) + ≤ 60 registry
  predicates. `llm.chat_json` → **SAINT** (`saint-cloud-medium`) first; on connection/HTTP failure →
  **direct Anthropic** (`claude-sonnet-4-6`, needs `ANTHROPIC_API_KEY`). Output validated by pydantic
  (`Extraction{summary, nothing_durable, facts[]}`), one repair retry on invalid JSON.
- **Apply** (`resolver.apply`, same transaction as the queue update): each proposed fact → normalize →
  `facts.upsert_fact(source_kind='extracted', confidence clamp [.3,.85], asserted_at=episode.occurred_at,
  origin_episode, evidence, contradicts=…)` or `facts.retract(source_kind='extracted')`; for a functional
  key whose active value is the same thing spelled differently (normalized-equal or value cosine ≥ .93)
  the active spelling is kept so the write NOOPs/corroborates instead of flip-flopping; `summary` → a new
  `episode(kind=summary, importance .6)`
  and the source turns drop to importance .3; `episode.processed_at` stamped; queue rows `done` with a
  result payload. A failure anywhere rolls the whole group back — **no half-applied jobs, no junk on LLM
  error.**
- **Backoff**: `failed` rows retry after 1, 5, 15, 60, 240 min; after `max_attempts` (5) → `dead`
  (`finished_at` set, `last_error` kept). `/health.queue.dead` counts them; see OPERATIONS for replay.
- Queue kinds `rederive_profile` / `embed_backfill` are routed to the curator instead of the LLM.

### 4.4 Curator (no LLM; `astoria/curator/maintenance.py`) — on the same loop

| job | cadence | what |
|---|---|---|
| `embed_backfill` | every 15 min | embed up to 200 facts + 200 episodes whose `embedding IS NULL` (after a TEI outage) |
| `rederive_profile` | every 6 h | for users whose profile-layer facts changed since `profile.rederived_at`: re-render the template narrative; bump `version` + `profile_history` only if the text changed |
| daily | every 24 h | `prune_snapshots` (> 90 d) · `archive_old_turns` (`kind='turn'` older than 14 d → `archived`) |

Intervals are measured from process start (monotonic), so a restart runs backfill/profile on the first
tick and the daily job ~24 h later.

## 5. Read path (`astoria/retrieval/recall.py`) — no LLM

```
query ─► embed_one("search_query: " + q)  (None if TEI down → BM25-only, health.degraded=true)
      ─► candidates (per layer set, status='active', valid_to IS NULL or > now()):
            facts:    top-40 cosine (HNSW, SET LOCAL hnsw.ef_search=64, cosine ≥ 0.48)  ⊕  top-40 BM25 (ts_rank_cd, OR-tsquery of the query words)
            episodes: top-20 cosine ⊕ top-20 BM25 over summary/note/import/turn, excluding THIS session's turns
      ─► RRF  score_rrf = Σ 1/(60 + rank)  over the lists an item appears in
      ─► weight  score = rrf × (0.25 + 0.25·recency + 0.25·importance + 0.25·trust)
            recency = exp(−ln2 · age_days / half_life):  episodic 30 d · semantic 180 d · beliefs (is_belief) 60 d · profile/procedural ∞
            trust   = confidence × source_trust   (episodes: fixed 0.6)
      ─► collapse  one row per FUNCTIONAL (subject, predicate); all rows for SET keys
      ─► budget    facts first, then ≤ 3 episodes; cost ≈ len(hook)/4 tokens ≤ max_tokens; ≤ limit items
      ─► stale_hint  (functional facts only) a newer active episode mentions the predicate words (and the
                     subject, unless it is the user) but NOT the current value → "· stale?" tag
      ─► working   last 4 turns of session_id (if given), oldest→newest, prepended, never searched
      ─► profile   (include_profile) narrative + active profile facts
      ─► side effects: one snapshot row; access_count+1 / last_seen=now() on everything shown
      ─► context   pre-rendered block every client injects verbatim
```

```
Relevant memory (current facts are authoritative; past conversation may be outdated):
- rick favorite beer: IPA  [profile · 0.90]
- rick uses tool: Neovim  [semantic · 0.84 · stale?]
- from a past session (2026-08-20): …  [episodic]
```
Empty store → `context: ""`.

**Time travel:** `as_of` (valid axis) and optional `as_believed_at` (belief axis) switch the fact
candidates to `facts.as_of(...)` (status `active|superseded`, `valid_from ≤ at < valid_to`, newest
assertion per key) ranked by BM25 only (no vector, no access bumps); episodes are filtered by
`occurred_at ≤ at`.

**Briefing** (`GET /briefing`): `"Known about <user> (authoritative, as of DATE):"` + narrative + every
active profile fact + the top-10 semantic facts by `importance × confidence × recency`, budgeted —
a stable, cache-friendly prompt prefix (Claude Code's SessionStart hook injects it).

## 6. API surfaces

- **REST** (`api/rest.py`): typed routes + `POST /op {action, …}` mirror; `GET /docs` (OpenAPI). Errors
  are `{"error": "..."}` with 400/404/503; TEI/LLM outages never 500.
- **MCP** (`api/mcp_tools.py`, FastMCP streamable-HTTP mounted at `/mcp/` — trailing slash): tools
  `recall · capture · remember · forget · memory(action=…)` + MemoryOS-compat `retrieve_memory ·
  add_memory · get_user_profile`. Tool docstrings are the descriptions agents see.
- **Identity** (`api/auth.py`): `Authorization: Bearer <token>` → client name via `ASTORIA_CLIENT_TOKENS`
  (`name:token,…`); else the unauthenticated `X-Astoria-Client: <name>` hint is trusted as-is (LAN-only);
  else `anonymous` (MCP without headers → `mcp`). The name becomes `fact.source`, the audit actor, the
  snapshot client and the trust cap.
- **MemoryOS-compat layer** (served during the migration so every client could move by URL alone):
  `POST /retrieve {user_id, query}` → `{short_term_history (last 4 turns across sessions), retrieved_pages
  (episodes), retrieved_user_knowledge (facts), retrieved_assistant_knowledge: [], user_profile ("None"
  when empty)}` · `POST /memories {user_id, user_input, agent_response, timestamp?}` → `{status: "ok",
  episode_id, deduped, queued}` (= capture a turn) · `GET /users/{id}/profile` → `{user_id, user_profile}`;
  the same three as MCP tools. All migrated clients now use the native routes; the compat layer stays for
  stragglers and is cheap to keep.

Full route/tool reference with examples: [API.md](API.md).

## 7. Process, deployment, degrade behaviour

- **One process**: `uvicorn astoria.api.app:app --workers 1` (Dockerfile) hosting FastAPI + FastMCP
  + the worker task. Lifespan = enter FastMCP's session manager → `db.migrate()` → start worker → serve →
  stop worker (10 s grace) → close pool. If FastMCP fails to import, REST still comes up (MCP disabled,
  logged). Request log = JSON lines on stdout `{ts, method, path, status, ms, client}`.
- **Compose** (`deploy/nas/docker-compose.yml` → `/volume1/docker/astoria/`): `astoria-postgres`
  (pgvector pg18, `./pgdata`, `127.0.0.1:8934`, 1 GB) · `astoria` (built from `./src`, `:8933`,
  768 MB, `./data`) · `astoria-backup` (pg_dump every `BACKUP_INTERVAL_S`=21600 s keep `BACKUP_KEEP`=14
  → `./backups`). Embeddings come from the existing `memoryos-tei` container (`:8931`, separate compose).
- **Degrade matrix**

  | failure | effect | recovery |
  |---|---|---|
  | TEI `:8931` down / wrong model | new rows are stored with `embedding=NULL`; recall is BM25-only and reports `health.tei=down, degraded=true`; `/health.tei.ok=false` (still 200) | curator `embed_backfill` every 15 min fills the NULLs; the served-model assertion (`nomic-embed` substring, cached 10 min) refuses a mismatched model so vector spaces never mix |
  | SAINT down (**every night** — the workstation powers off) | `llm.chat` falls to direct Anthropic `claude-sonnet-4-6` (enabled on the NAS via `ANTHROPIC_API_KEY`); cognify keeps flowing overnight at cloud cost | automatic; `/health.llm.saint="unreachable"` meanwhile |
  | SAINT and cloud both unavailable | jobs → `failed` with backoff 1/5/15/60/240 min, `dead` after 5; nothing written | ambient NL corrections are **eventually consistent** until extraction runs; structured ops and the regex detector are immediate |
  | LLM returns garbage | repair retry once, then `failed` (backoff) — never writes junk | as above |
  | Postgres down | `/health` 503 `status=error`; every action errors | compose restarts; `restart: unless-stopped` |
  | worker crash | supervised task logs and exits; API keeps serving; `queue.pending` grows | restart the container |
  | NAS reboot | all three containers `restart: unless-stopped`; migrations re-run idempotently | — |

## 8. Limitations and roadmap (v1)

- **Belief axis is lossy on supersede.** Closing the old functional row rewrites its `valid_to` *in
  place*, so `as_of(at, as_believed_at=B)` with `B` before the correction and `at` after the new
  `valid_from` no longer returns the value we believed at `B`. Roadmap: version the old row on the belief
  axis (insert a closed copy, leave the original's `valid_to` as it was believed).
- Regex detector v1 covers a handful of English patterns; everything else waits for cognify
  (overnight window above). Roadmap: an LLM *target-resolver* for natural-language forget/correct
  (deterministic execution, LLM only to resolve the target).
- Graph/edge retrieval is not built (`graph_max_depth/fanout` settings exist but are unused);
  `related_to` is just a set predicate today. Roadmap: per-layer retrieve-then-merge, a local
  cross-encoder rerank seat via johnny, AGE as an ETL.
- Profile narrative is template-rendered (`source='template'`); an LLM narrative (`source='llm'`) is
  reserved but not implemented.
- Several `Settings` knobs are reserved, not wired (`recall_limit/token_budget/min_score`,
  `vector/fts_candidates`, `w_*`, `recency_half_life_days`, `contiguity_boost`, `trust_prior_*`,
  `belief_half_life_days`, `cognify_poll_s`, `curator_interval_min`, `backup_*`, `working_window_*`,
  `decay_archive_threshold`); the live values are the module constants documented above. Wired:
  `db_*`, `user_default`, `client_tokens`, `embed_*`, `llm_*`, `anthropic_api_key`, `recall_min_cosine`,
  `confidence_floor/cap/staging_threshold`, `worker_enabled`, `cognify_batch`, `host/port/log_level`.
- Single-user in practice (`user_id` default `rick`), multi-user-ready (every table keyed by `user_id`,
  `DELETE /users/{id}` wipes one).
- No telemetry, no external calls except TEI (NAS), SAINT (LAN) and — only for the cognify fallback —
  `api.anthropic.com`.

### Addendum 2026-08-22 — trust guard on functional keys
A machine-sourced value (`source_kind` extracted / imported / curator) does **not** silently supersede a
human-stated active value (`explicit` / `detector`) on a functional key. It supersedes only when the
extractor explicitly declared `contradicts` against that row (it saw the candidate and judged a real
contradiction — e.g. *"actually my favorite beer is IPA"*). An incidental same-key assertion (e.g.
*"grew up near Skiatook"* extracted as `location`) lands in **staging** with `meta.conflict_with` and an
audit row `conflict_staged`; `astoria staging` / `astoria approve <id>` resolve it. Motivation: the
Claude/Gemini memory-export ingest on 2026-08-22, where a third-party summary briefly overrode an explicit
location fact.
