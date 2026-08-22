# Astoria — interface contract

The fixed interfaces every module, client and test codes against: the *what*, decided. Rationale lives in
the design record; explanations in [ARCHITECTURE.md](ARCHITECTURE.md); request/response detail in
[API.md](API.md). When this page and the code disagree, the code is right and this page has a bug.

## Service

- **Process**: one `uvicorn` worker running FastAPI + FastMCP (`/mcp/`, trailing slash) **and** the
  in-process worker loop (cognify drain, embed backfill, curator), leader-elected with
  `pg_try_advisory_lock(43)`.
- **Store**: Postgres + pgvector (≥ 0.8), schema `astoria/sql/001…004`, applied at start-up.
- **Embeddings**: nomic-embed-text-v1.5, 768-d, prefixes `search_document:` / `search_query:`, any
  OpenAI-compatible endpoint(s) (`ASTORIA_EMBED_URLS`, priority order, verified vector space).
  **Reranker** (optional): TEI `POST /rerank`. **LLM**: OpenAI-compatible primary → Anthropic fallback;
  write path only (cognify, curator) plus the on-demand target resolver.
- **Identity**: every request carries `user_id` (omitted/empty → `ASTORIA_USER_DEFAULT`, reported in `/health.user_default`). `Authorization: Bearer
  <token>` → client name via `ASTORIA_CLIENT_TOKENS`; else `X-Astoria-Client` hint; else `anonymous`
  (MCP: `mcp`). The client name is the fact `source` and selects the trust cap. `ASTORIA_REQUIRE_TOKEN`
  gates writes (`401`).

## Layers and kinds

| layer | storage | kind |
|---|---|---|
| working | `episode.kind='turn'` per `session_id` | raw turns (prepended as `working`, never searched for the same session) |
| episodic | `episode.kind in (summary, note, import)` + other sessions' turns | summaries / notes / imports / past turns |
| semantic / profile / procedural | `fact.layer` | `(subject, predicate, value)` |
| graph | `entity`, `alias`, `edge` | typed links, subject aliases |

Subject canonicalisation: first person (`I/me/my/myself/user/owner/self/you`, the user's id) → literally
`user_id`; otherwise lower-case, whitespace-collapsed, ≤ 64 chars, alias-resolved. Predicates snake_case
(≤ 64). Unknown predicates auto-register: `functional` iff prefix `favorite_|default_|primary_|preferred_|current_`
or suffix `_is|_name`, else `set`. Layer = `profile` iff subject == user_id and `predicate.layer_hint='profile'`.

## Trust numbers

- default confidence by `source_kind` (`KIND_CONF`): explicit .90 · detector .80 · extracted = LLM value
  clamped [.30, .85] · imported .45 · curator .50; clamps `[0.05, 0.98]`.
- staging gate: extracted / imported / curator with confidence < **0.35** → `status='staging'`.
- corroboration (distinct `origin_episode` **and** distinct client): `conf ← 1 − (1 − conf) × 0.6`.
- `source_trust = min(CLIENT_TRUST[client], KIND_TRUST[kind], confidence)` — ranking only.
  `CLIENT_TRUST`: cli/human 1.0 · input/claude-code .85 · api/mcp .7 · megaplan .6 · curator/anonymous .5 ·
  import .4 · other .6. `KIND_TRUST`: explicit 1.0 · detector .9 · extracted .75 · curator .5 · imported .45.
- trust guard: extracted / imported / curator never silently supersede an active explicit / detector value
  on a functional key — only with a declared `contradicts`; otherwise `staging` + `meta.conflict_with`.

## Python module interfaces (internal)

```python
# astoria/store/db.py
db.conn()                      # context manager → psycopg connection in a txn (commit/rollback)
db.migrate() -> [versions]; db.healthcheck() -> {facts_active, episodes_active, cognify_pending, pgvector}

# astoria/store/facts.py
facts.upsert_fact(c, *, user_id, subject, predicate, value, source="api", source_kind="explicit",
                  confidence=None, valid_from=None, valid_to=None, asserted_at=None, layer=None,
                  is_belief=False, importance=.5, tags=(), origin_episode=None, evidence=None, ref=None,
                  cardinality=None, actor=None, embed=True, meta=None, contradicts=(), historical=False,
                  status_override=None)
    -> {"fact": row|None, "action": inserted|superseded|noop|historical|staging|blocked, "superseded": [ids]}
facts.retract(c, *, user_id, subject=None, predicate=None, value=None, fact_id=None, actor=None,
              source_kind="explicit", reason="retract") -> [rows]
facts.forget(c, *, user_id, fact_id, mode="soft"|"hard", actor=None) -> row|None
facts.update_fact(c, *, user_id, fact_id, actor=None, embed=None, **fields) -> row|None   # embed=None → settings.embed_sync
facts.approve_staging(c, *, user_id, fact_id, actor=None) -> row|None
facts.get_fact / list_facts(c, *, user_id, subject, predicate, status="active"|"any", layer, q, limit, offset)
facts.history(c, *, user_id, subject, predicate, include_expired=False) -> [rows newest-first]
facts.as_of(c, *, user_id, at, as_believed_at=None, subject=None, predicate=None, limit=50) -> [rows]
facts.row_public(row) -> JSON-safe dict (drops embedding/tsv/value_norm)
facts.canon_subject(s, user_id); canon_subject_db(c, s, user_id); canon_predicate(p); guess_cardinality(p)

# astoria/store/episodes.py
episodes.add_episode(c, *, user_id, kind="turn", text=None, user_input=None, agent_response=None,
                     source="api", session_id=None, occurred_at=None, importance=.5, tags=(), meta=None,
                     embed=True) -> {"episode": row, "deduped": bool}     # idem_key = sha256(user_id|session_id|kind|body)
episodes.recent_turns(c, *, user_id, session_id, n=4) -> [rows oldest-first, + user_input/agent_response]
episodes.enqueue_cognify(c, *, user_id, episode_id, session_id=None, priority=5, occurred_at=None,
                         payload=None, kind="extract") -> row
episodes.get_episode / list_episodes / archive_episode / delete_episode / touch / row_public

# astoria/store/graph.py
graph.resolve_alias(c, user_id, name) -> canonical|None
graph.add_alias(c, *, user_id, alias, canonical, source="api", source_kind="explicit", actor=None)
    -> {"alias": row, "action": inserted|updated|noop, "repointed": n}
graph.list_aliases / delete_alias; ensure_entity / get_entity / list_entities
graph.add_edge(c, *, user_id, src, relation, dst, src_kind=None, dst_kind=None, weight=1.0,
               valid_from=None, valid_to=None, asserted_at=None, source="api", source_kind="explicit",
               confidence=None, origin_episode=None, evidence=None, meta=None, actor=None)
    -> {"edge": row, "action": inserted|noop}
graph.get_edge / retract_edge(mode=retract|archive|hard) / list_edges(node, relation, depth, status, …)
graph.neighbors(c, user_id, node_ids, max_depth=2, max_fanout=20, max_results=None)
    -> [{node, kind, id, hops, via, direction, path, relations}]
graph.parse_node("fact:<uuid>"|"entity:<name>"|uuid|name) -> (kind, id); node_ref(kind, id); row_public

# astoria/core/capture.py
capture.gate(text) -> reason|None           # empty | slash_command | ack | too_short
capture.detect(text, user_id) -> {"op": correct|retract|remember, "subject", "predicate", "value", …}|None
capture.is_correction_hint(text) -> bool
capture.capture(c, *, user_id, kind="turn", text=None, user_input=None, agent_response=None, source="api",
                session_id=None, occurred_at=None, importance=.5, tags=(), meta=None, cognify=True,
                priority="normal"|"high", actor=None, sync=None)
    -> {"episode_id", "deduped", "dropped", "detector": {...}|None, "queued": bool}

# astoria/core/embed.py · rerank.py · llm.py
embed.embed_texts(texts, *, query=False) -> [vec|None]; embed_one(text, *, query=False); embed_health()
rerank.rerank(query, docs) -> [logit|None]|None; rerank.blend(base, logits, weight); rerank.enabled(); rerank_health()
llm.chat(messages, *, model=None, max_tokens=1500, temperature=0.0) -> LLMResult(text, model, route, latency_s)
llm.chat_json(messages, *, model=None, max_tokens=1500) -> dict|list|None; llm.llm_health()   # raises LLMUnavailable

# astoria/retrieval/recall.py · graph.py
recall.recall(c, *, user_id, query, session_id=None, layers=("profile","semantic","procedural","episodic"),
              max_tokens=1000, limit=12, facts_only=False, include_profile=False, as_of=None,
              as_believed_at=None, client=None, min_cosine=None, rerank=None) -> dict (see REST)
              # service passes settings.recall_token_budget / recall_limit when the request omits them
recall.briefing(c, *, user_id, max_tokens=1200) -> {"narrative", "facts": [...], "context": str}
recall.search_facts_simple(c, *, user_id, query, limit=20, min_cosine=None) -> [public rows + score]
recall.render_context(items) -> str
graph.expand_candidates(c, user_id, seed_fact_ids, depth=None, fanout=None, max_results=None, seed_subjects=True)
    -> [fact rows + graph_hops, graph_via, graph_path]
graph.render_graph(c, user_id, node, depth=None, fanout=None) -> {"root", "depth", "nodes", "edges", "counts"}

# astoria/cognify/resolver.py · targets.py · worker.py
resolver.gather_context(c, *, user_id, job_text, limit=30) -> (candidates, registry)
resolver.extract(job_text, occurred_at, user_id, candidates, registry) -> Extraction|None   # raises LLMUnavailable
resolver.apply(c, *, user_id, episode_ids, parsed, source, session_id, occurred_at=None)
    -> {"facts": [...], "retracted": [...], "summary_episode": id|None, "edges": [...], "aliases": [...]}
targets.resolve(c, *, user_id, text, limit=8) -> plan dict (intent, targets, new_fact, confidence,
                                                  explanation, requires_confirmation, text, candidates[, error, error_kind])
targets.apply(c, *, user_id, plan, source="api", actor=None)
    -> {"intent", "applied", "changed": [...], "superseded": [ids], "fact", "action"[, "reason"]}
worker.run_forever(stop_event, tick_s=None)        # asyncio task started in app lifespan
worker.drain_once(limit=None) -> {"processed", "failed", "dead", "skipped"}
worker.claim_jobs / coalesce / process_group / embed_backfill_tick

# astoria/curator/maintenance.py  (all take dry_run where it makes sense, return a report dict)
curator.embed_backfill(c, limit=200); prune_snapshots(c, days=90); archive_old_turns(c, hours=None, per_session=None)
curator.rederive_profile(c, user_id, *, llm=False, dry_run=False); users_with_profile_changes(c); users_with_facts(c)
curator.dedup_facts(c, user_id, *, dry_run=False, cosine=None); find_duplicate_pairs(c, user_id, *, cosine=None)
curator.decay(c, user_id, *, dry_run=False, threshold=None, min_age_days=None); decay_score(row, *, now=None)
curator.reflect(c, user_id, *, dry_run=False, days=7); users_with_unreflected(c, *, days=7)

# astoria/api/service.py
service.do_action(action, params, client="anonymous") -> dict|list   # errors: {"error", "status_code"}
service.VALID_ACTIONS                                                 # the full action list (API.md §3.8)
```

## REST (canonical) — `astoria/api/rest.py`

All JSON; timestamps ISO-8601 (UTC if naive); errors `{"error": "..."}` with 400/401/404/503.

| Method & path | Body → Response |
|---|---|
| `GET /` | `{service, version, docs, mcp}` |
| `GET /health` | `{status, version, user_default, db, queue, tei, llm, rerank}` — **200 iff DB ok** |
| `POST /recall` | `{user_id, query, session_id?, layers?, max_tokens=1000, limit=12, facts_only, include_profile, as_of?, as_believed_at?, rerank?, min_cosine?}` → `{user_id, query, items:[RecallItem], working, profile, context, health:{tei, degraded, rerank}, snapshot_id, as_of, as_believed_at}` |
| `POST /capture` | `{user_id, kind, text? \| user_input?+agent_response?, source?, session_id?, occurred_at?, importance?, tags?, meta?, cognify=true, priority, sync?}` → `{episode_id, deduped, dropped, detector, queued}` |
| `GET /briefing?user_id&max_tokens` | `{narrative, facts, context}` |
| `GET /profile?user_id&limit` | `{user_id, narrative, version, rederived_at, facts}` |
| `GET /facts?user_id&subject&predicate&status&layer&q&limit&offset` | `[fact]` |
| `POST /facts` · `POST /correct` | `{user_id, subject, predicate, value, valid_from?, valid_to?, asserted_at?, confidence?, layer?, tags?, cardinality?, historical?, importance?, is_belief?, evidence?, meta?, ref?, sync?}` → `{fact, action, superseded}` |
| `GET /facts/{id}` · `PATCH /facts/{id}` · `DELETE /facts/{id}?mode=soft\|hard` | fact · fact · `{deleted, mode, fact_id}` |
| `POST /retract` | `{user_id, subject?, predicate?, value?, fact_id?, source_kind?, reason?}` → `{retracted:[ids], facts}` |
| `POST /forget` | `{user_id, fact_id? \| subject?/predicate?/value? \| query?, mode, limit?}` → `{forgotten:[...]}` |
| `POST /approve` | `{user_id, fact_id}` → `{fact}` |
| `GET /history?user_id&subject&predicate` | `[fact]` newest first (belief-closed originals hidden) |
| `POST /as_of` | `{user_id, at, as_believed_at?, subject?, predicate?, query?, limit}` → `[fact]` |
| `POST /resolve` | `{user_id, text, limit}` → plan |
| `POST /resolve/apply` | `{user_id, plan? \| text?, confirm, limit}` → `{applied, plan, …}` |
| `GET /episodes?…` · `GET /episodes/{id}` · `DELETE /episodes/{id}` | `[episode]` · episode · `{deleted, episode_id}` |
| `GET /predicates` · `PATCH /predicates/{name}` | `[predicate]` · predicate |
| `GET /audit?user_id&limit&offset` | `[audit]` |
| `GET /graph?node&user_id&depth&fanout` | `{root, depth, nodes, edges, counts}` |
| `GET /edges?…` · `POST /edges` · `DELETE /edges/{id}?mode=retract\|archive\|hard` | `[edge]` · `{edge, action}` · `{deleted, mode, edge}` |
| `GET /aliases?…` · `POST /aliases` · `DELETE /aliases/{alias}` | `[alias]` · `{alias, action, repointed}` · `{deleted, alias}` |
| `POST /op` | `{action, ...}` dispatcher mirror (incl. `queue_stats`) |
| `DELETE /users/{user_id}` | `{deleted, user_id, counts}` |
| compat: `POST /retrieve` · `POST /memories` · `GET /users/{id}/profile` | earlier-service shapes (`user_profile` is `"None"` when empty) |

`RecallItem = {id, layer, kind: fact|episode, text, subject?, predicate?, value?, score, confidence, source,
source_trust, asserted_at?, valid_from?, valid_to?, occurred_at?, last_seen?, is_belief, stale_hint,
cardinality?, status?, episode_kind?, session_id?, rerank_score?}`.

`context` rendering (injected verbatim by clients; `""` when empty):

```
Relevant memory (current facts are authoritative; past conversation may be outdated):
- alice favorite beer: IPA  [profile · 0.90]
- alice uses tool: Neovim  [semantic · 0.84 · stale?]
- from a past session (2026-05-02): …  [episodic]
```

## MCP tools — `astoria/api/mcp_tools.py`

- `recall(query, user_id="", layers=None, limit=12, max_tokens=1000, as_of="", include_profile=False, session_id="", facts_only=False)`
- `capture(text="", user_input="", agent_response="", kind="note", user_id="", session_id="", source="", importance=0.5, tags=None, cognify=True, priority="normal")`
- `remember(subject, predicate, value, user_id="", valid_from="", valid_to="", retract=False, layer="", confidence=None, tags=None)`
- `forget(fact_id="", subject="", predicate="", value="", query="", mode="soft", user_id="")`
- `memory(action, …)` — `resolve | resolve_apply | list | facts | get | update | delete | history | as_of | profile |
  briefing | predicates | approve | episodes | audit | health | graph | edges | edge_add | edge_delete | aliases |
  alias_add | alias_delete`
- compat: `retrieve_memory(user_id, query)`, `add_memory(user_id, user_input, agent_response, timestamp="")`,
  `get_user_profile(user_id)`

## Recall algorithm (defaults)

candidates: facts top-40 cosine (HNSW, `hnsw.ef_search=64`, `hnsw.iterative_scan=relaxed_order`, cosine ≥
**0.45**) ⊕ top-40 BM25 (`ts_rank_cd`, OR-tsquery of query words + synonym expansion); episodes 20 ⊕ 20
(not this session's turns) → RRF k=60 → `score = rrf × (0.25 + 0.25·recency + 0.25·importance +
0.25·trust)`, recency `2^(−age/half_life)` (settings: episodic 30 d · semantic 180 d · beliefs 60 d; profile/procedural
∞), `trust = confidence × source_trust` (episodes 0.6) → graph expansion (top-10 seeds, ≤ `graph_max_depth`
hops, `score = min/(1+hops)`) → optional rerank (top-`rerank_top_n` facts + 6 episodes, `(1−w)·norm(score)
+ w·norm(sigmoid(logit))`, w = 0.6) → collapse by `(subject, predicate)` (functional → 1 row) → budget
`max_tokens` (≈ chars/4), facts before episodes, ≤ 3 episodes, ≤ `limit` → `stale_hint` (FTS check) →
one `snapshot` row + touch. `as_of` → `facts.as_of` rows ranked by BM25 only. Embedder down → BM25 only +
`health.degraded`; reranker down → base order + `health.rerank="down"`.

## Cognify (resolver v1)

One LLM call per coalesced group (≤ 8 episodes / 6000 chars, `priority=1` first). Prompt: job text +
`OCCURRED_AT`; ≤ 30 candidate active facts (top-20 cosine + literal subject matches) as `{id, subject,
predicate, value, valid_from}`; registry (≤ 60); rules. Returns strict JSON:

```json
{"summary": "one sentence or null", "nothing_durable": false,
 "facts": [{"subject":"alice","predicate":"favorite_beer","value":"IPA","layer":"profile",
            "is_belief":false,"confidence":0.85,"valid_from":null,"valid_to":null,
            "action":"assert","contradicts":["<candidate id>"],"evidence":"verbatim"}],
 "edges": [{"src":"<subject or fact:N>","relation":"runs_on","dst":"<subject>","confidence":0.7,"evidence":"…"}],
 "aliases": [{"alias":"<other name>","canonical":"<name to keep>","evidence":"…"}]}
```

Apply (one transaction): aliases → facts (`upsert_fact(source_kind="extracted", asserted_at=occurred_at,
contradicts=…)` with the functional near-duplicate guard, or `retract(source_kind="extracted")`) → edges
(`add_edge(source_kind="extracted")`) → `summary` episode (turn groups only) → `episode.processed_at` →
queue rows `done`. One repair retry on invalid JSON; else `failed` (back-off 1, 5, 15, 60, 240 min; `dead`
at 5).
