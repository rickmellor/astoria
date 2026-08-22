# Astoria — build contract (v1)

The fixed interfaces every module and client codes against. Design rationale lives in
`~/projects/infrastructure/astoria/DESIGN.md`; this is the *what*, decided.

## Service

- **Host/port:** NAS `192.168.1.134`, REST `:8933`, MCP streamable-HTTP at `/mcp/` (trailing slash).
- **Process:** one `uvicorn` (1 worker) running FastAPI + FastMCP **and** the in-process worker loop
  (cognify drain / embed backfill / profile re-derive / snapshot prune), guarded by
  `pg_try_advisory_lock(43)`.
- **Store:** Postgres 18 + pgvector (`astoria-postgres`), schema in `astoria/sql/001_schema.sql`.
- **Embeddings:** NAS TEI nomic `:8931`, 768-d, prefixes `search_document:` / `search_query:`
  (`astoria/core/embed.py`). **LLM:** SAINT `:4000` (`saint-cloud-medium`) → fallback direct
  Anthropic (`astoria/core/llm.py`). LLM only on the write path.
- **Identity:** every request carries `user_id` (default `rick`). Optional `Authorization: Bearer <token>`
  maps to a client name via `ASTORIA_CLIENT_TOKENS="input:tok,claude-code:tok,..."`; missing token →
  client `anonymous` (LAN-only; writes allowed). The client name is the fact `source` and sets the
  trust cap (`facts.CLIENT_TRUST`).

## Layers & kinds

| layer | storage | kind |
|---|---|---|
| working | `episode.kind='turn'` (per `session_id`) | raw turns |
| episodic | `episode.kind in (summary, note, import)` | summaries/notes |
| semantic / profile / procedural | `fact.layer` | `(subject, predicate, value)` |

Subject canonicalization: first-person (`I/me/my/user/rick`) → literally `user_id`. Predicates snake_case;
unknown predicates auto-register (`functional` iff prefix `favorite_|default_|primary_|preferred_|current_`
or suffix `_is|_name`, else `set`). Profile layer = subject==user_id AND predicate.layer_hint=profile.

## Trust numbers

- confidence defaults by `source_kind`: explicit .90 · detector .80 · extracted = LLM value clamped [.3,.85] · imported .45 · curator .50 (`is_belief`)
- gate: extracted/imported/curator with confidence < **0.35** → `status='staging'` (not recalled)
- corroboration (distinct `origin_episode` AND distinct client): `conf = 1-(1-conf)*0.6` (saturating)
- `source_trust = min(CLIENT_TRUST[client], KIND_TRUST[kind], confidence)` — ranking only, never a resolver

## Python module interfaces (internal)

```python
# astoria/store/db.py
db.conn()                      # context manager → psycopg connection in a txn (commit/rollback)
db.migrate(); db.healthcheck()
# astoria/store/facts.py  (done)
facts.upsert_fact(c, *, user_id, subject, predicate, value, source, source_kind, confidence=None,
                  valid_from=None, valid_to=None, asserted_at=None, layer=None, is_belief=False,
                  importance=.5, tags=(), origin_episode=None, evidence=None, ref=None,
                  cardinality=None, actor=None, embed=True, meta=None, contradicts=(),
                  historical=False) -> {"fact": row|None, "action": str, "superseded": [ids]}
facts.retract(c, *, user_id, subject=None, predicate=None, value=None, fact_id=None, actor=None,
              source_kind="explicit", reason="retract") -> [rows]
facts.forget(c, *, user_id, fact_id, mode="soft"|"hard", actor=None) -> row|None
facts.update_fact(c, *, user_id, fact_id, actor=None, **fields) -> row|None
facts.approve_staging(c, *, user_id, fact_id, actor=None) -> row|None
facts.get_fact / list_facts(c, *, user_id, subject, predicate, status, layer, q, limit, offset)
facts.history(c, *, user_id, subject, predicate) -> [rows newest-first]
facts.as_of(c, *, user_id, at, as_believed_at=None, subject=None, predicate=None, limit=50)
facts.row_public(row) -> JSON-safe dict (drops embeddings)
# astoria/store/episodes.py  (TO BUILD)
episodes.add_episode(c, *, user_id, kind, text=None, user_input=None, agent_response=None,
                     source, session_id=None, occurred_at=None, importance=.5, tags=(), meta=None,
                     embed=True) -> {"episode": row, "deduped": bool}     # idem_key = sha256(user_id|session_id|kind|text)
episodes.recent_turns(c, *, user_id, session_id, n=4) -> [rows]
episodes.get_episode / list_episodes / archive_episode / delete_episode / row_public
# astoria/core/capture.py  (TO BUILD)
capture.gate(text) -> reason|None          # drop: ^/\w+, len<8, {ok,done,y,n,yes,no,thanks,continue}
capture.detect(text, user_id) -> {"op": "correct"|"retract"|"remember", "subject","predicate","value"}|None  # regex v1
capture.capture(c, *, user_id, kind, text|user_input+agent_response, source, session_id, occurred_at,
                importance, tags, meta, cognify=True, priority="normal"|"high")
        -> {"episode_id", "deduped", "dropped", "detector": {...}|None, "queued": bool}
# astoria/retrieval/recall.py  (TO BUILD)
recall.recall(c, *, user_id, query, session_id=None, layers=("profile","semantic","procedural","episodic"),
              max_tokens=1000, limit=12, facts_only=False, include_profile=False, as_of=None,
              as_believed_at=None, client=None) -> RecallResult (dict, see REST below)
recall.briefing(c, *, user_id, max_tokens=1200) -> {"narrative","facts":[...],"context": str}
# astoria/cognify/resolver.py + worker.py  (TO BUILD)
resolver.extract(job_text, occurred_at, candidates, registry) -> parsed JSON (pydantic-validated) | None
resolver.apply(c, *, user_id, episode_ids, parsed, source, session_id) -> {"facts": [...], "summary_episode": id|None}
worker.run_forever(stop_event)   # asyncio task started in app lifespan; tick 30s; coalesce by session
worker.drain_once(limit=4) -> {"processed": n, "failed": n}
# astoria/curator/*.py (TO BUILD): embed_backfill(c), rederive_profile(c, user_id), prune_snapshots(c), archive_old_turns(c)
```

## REST (canonical) — `astoria/api/rest.py`

All JSON. Timestamps ISO-8601 (UTC if naive). Errors `{"error": "..."}` with 4xx.

| Method & path | Body → Response |
|---|---|
| `GET /health` | `{status:"ok", db:{...}, tei:{ok,model}, llm:{saint,fallback}, queue:{pending,dead}, version}` — **200 iff DB ok** |
| `POST /recall` | `{user_id, query, session_id?, layers?, max_tokens=1000, limit=12, facts_only=false, include_profile=false, as_of?, as_believed_at?}` → `{user_id, query, items:[RecallItem], working:[{user_input,agent_response,occurred_at}], profile:{narrative,facts}|null, context:"<pre-rendered block>", health:{tei,degraded}, snapshot_id}` |
| `POST /capture` | `{user_id, kind:"turn"|"summary"|"note", text? | user_input?+agent_response?, source?, session_id?, occurred_at?, importance?, tags?, meta?, cognify=true, priority="normal"}` → `{episode_id, deduped, dropped?, detector?, queued}` |
| `GET /briefing?user_id&max_tokens` | `{narrative, facts, context}` (stable prefix for prompt caching) |
| `GET /profile?user_id` | `{user_id, narrative, version, facts:[profile-layer facts]}` |
| `GET /facts?user_id&subject&predicate&status&layer&q&limit&offset` | `[fact]` |
| `POST /facts` | `{user_id, subject, predicate, value, valid_from?, valid_to?, confidence?, layer?, tags?, cardinality?, historical?}` → `{fact, action, superseded}` (source_kind=explicit) |
| `GET /facts/{id}` · `PATCH /facts/{id}` · `DELETE /facts/{id}?mode=soft|hard` | fact / fact / `{deleted:true}` |
| `POST /correct` | `{user_id, subject, predicate, value, valid_from?}` → `{fact, action, superseded}` (= POST /facts) |
| `POST /retract` | `{user_id, subject?, predicate?, value?, fact_id?}` → `{retracted:[ids]}` |
| `POST /forget` | `{user_id, fact_id? | query?, mode:"soft"|"hard"}` → `{forgotten:[...]}` |
| `POST /approve` | `{user_id, fact_id}` → `{fact}` (staging→active) |
| `GET /history?user_id&subject&predicate` | `[fact]` chain newest-first |
| `POST /as_of` | `{user_id, at, as_believed_at?, subject?, predicate?, query?}` → `[fact]` |
| `GET /episodes?user_id&session_id&kind&limit` · `DELETE /episodes/{id}` | |
| `GET /predicates` · `PATCH /predicates/{name}` `{cardinality, layer_hint}` | |
| `GET /audit?user_id&limit` | |
| `POST /op` | `{action, ...}` dispatcher mirror (MegaPlan precedent) |
| `DELETE /users/{user_id}` | wipe a user (tests) |
| **compat (MemoryOS)** `POST /retrieve` `{user_id, query}` · `POST /memories` `{user_id,user_input,agent_response,timestamp?}` · `GET /users/{id}/profile` | MemoryOS-shaped responses (`user_profile` is `"None"` when empty) |

`RecallItem = {id, layer, kind:"fact"|"episode", text, subject?, predicate?, value?, score, confidence,
source, source_trust, asserted_at?, valid_from?, valid_to?, occurred_at?, last_seen?, is_belief, stale_hint}`

**`context` rendering** (what clients inject verbatim):
```
Relevant memory (current facts are authoritative; past conversation may be outdated):
- rick favorite beer: IPA  [profile · 0.90]
- rick uses tool: Neovim  [semantic · 0.84]
- from a past session (2026-08-20): …  [episodic]
```
Empty store → `context: ""`.

## MCP tools — `astoria/api/mcp_tools.py` (FastMCP, return dicts)

- `recall(query, user_id="rick", layers=None, limit=12, max_tokens=1000, as_of="", include_profile=False)`
- `capture(text="", user_input="", agent_response="", kind="note", user_id="rick", session_id="", source="mcp")`
- `remember(subject, predicate, value, user_id="rick", valid_from="", valid_to="", retract=False)` — structured triple (add / or retract)
- `forget(fact_id="", subject="", predicate="", value="", mode="soft", user_id="rick")`
- `memory(action, ...)` admin dispatcher: `list|get|update|history|as_of|profile|briefing|predicates|approve|audit|health`
- compat: `retrieve_memory(user_id, query)`, `add_memory(user_id, user_input, agent_response)`, `get_user_profile(user_id)`

## Recall algorithm (defaults)

candidates: per layer top-40 cosine (HNSW, `hnsw.ef_search=64`, min cosine **0.45**) ⊕ top-40 BM25
(`ts_rank_cd`) → RRF k=60 → `score = rrf × (0.25 + 0.25·recency + 0.25·importance + 0.25·trust)`;
`recency = exp(-ln2·age_days/half_life)` with half-life episodic 30d · semantic 180d · beliefs 60d ·
profile/procedural ∞; `trust = confidence × source_trust`. Collapse by `(subject,predicate)` (functional
→ 1 row; set → all). Budget `max_tokens` (≈chars/4), facts before episodes, episodes ≤ 3. `stale_hint`
when a newer episode mentions a different value for the key (FTS check). Working memory = last 4 turns
of `session_id`, prepended, not searched. TEI down → BM25 only + `health.degraded=true`. One `snapshot`
row per recall.

## Cognify (resolver v1)

One LLM call per job (coalesced turns of a session, ≤8 turns/6k chars; `priority=high` first).
Prompt gives: job text + occurred_at; ≤30 candidate active facts (top-20 cosine + literal subject
matches) as `{id, subject, predicate, value, valid_from}`; registry summary; rules. Returns JSON:
```json
{"summary": "one sentence or null", "nothing_durable": false,
 "facts": [{"subject":"rick","predicate":"favorite_beer","value":"IPA","layer":"profile",
            "is_belief":false,"confidence":0.85,"valid_from":null,"valid_to":null,
            "action":"assert"|"retract","contradicts":["<fact id>"],"evidence":"verbatim"}]}
```
Apply: normalize → `facts.upsert_fact(source_kind="extracted", asserted_at=occurred_at, contradicts=…)`
/ `facts.retract(source_kind="extracted")` → summary → `episode(kind=summary)`; mark queue row done
in the same txn; `episode.processed_at`. One repair retry on invalid JSON; else job stays pending
(backoff 1,5,15,60,240 min; dead at 5).
