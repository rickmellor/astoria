# Astoria — API reference (REST + MCP)

Every route and every MCP tool is a thin wrapper over one dispatcher (`astoria/api/service.do_action`),
so the two surfaces share parameters, semantics and error shapes. This page lists every REST route with
its request/response JSON, every MCP tool with its full action list, identity/auth, the compatibility
routes and the error shapes. Examples use `http://nas.local:8933` as the service and `alice` as the user
id; substitute your own.

## 1. Conventions

- **Transport**: JSON over HTTP. Requests with a body are `Content-Type: application/json`.
- **`user_id`**: every call is scoped to one user. Omit it to use `ASTORIA_USER_DEFAULT`.
- **Timestamps**: ISO-8601 in and out (UTC assumed for naive values; epoch seconds accepted on input).
- **Fact rows** (`fact` objects below) are the `fact` table minus `embedding`, `tsv`, `value_norm`, with
  uuids and timestamps as strings. **Episode rows** likewise minus `embedding`, `tsv`.
- **Errors**: `{"error": "<message>"}` with `400` (bad input), `401` (token required), `404` (not found),
  `503` (a dependency or module unavailable); `GET /health` is `503` when the database is down. A few
  LLM-backed responses carry extra fields (`error_kind`, see `/resolve`).
- **Numbers are clamped**, not rejected: `limit` ≤ 200 (recall) / 1000 (lists), `max_tokens` 50–20000,
  `depth` ≤ 6, `fanout` ≤ 200.
- `GET /` → `{"service":"astoria","version":"0.1.0","docs":"/docs","mcp":"/mcp/"}`; `GET /docs` is the
  OpenAPI UI.

## 2. Identity and auth

| header | effect |
|---|---|
| `Authorization: Bearer <token>` | token looked up in `ASTORIA_CLIENT_TOKENS` (`name:token,…`) → a **proven** client name |
| `X-Astoria-Client: <name>` | an unauthenticated hint (≤ 64 chars), used when no valid token is present |
| neither | `anonymous` (MCP calls without HTTP headers → `mcp`) |

The client name becomes `fact.source`, the audit `actor` and the `snapshot.client`, and selects the
ranking trust cap (`cli`/`human` 1.0, `input`/`claude-code` .85, `api`/`mcp` .7, `megaplan` .6,
`anonymous`/`curator` .5, `import` .4, other .6). With `ASTORIA_REQUIRE_TOKEN=true`, every action that is
not a read (`recall, briefing, profile, facts_list, fact_get, history, as_of, episodes_list, episode_get,
predicates_list, audit, health, retrieve, user_profile, graph, edges_list, aliases_list, resolve,
queue_stats`) requires a valid bearer token:

```
401 {"error": "action 'fact_add' requires a client token (ASTORIA_REQUIRE_TOKEN=true)"}
```

Shorthand used below:

```bash
A=http://nas.local:8933
H='Content-Type: application/json'
# add -H "Authorization: Bearer $ASTORIA_TOKEN"  or  -H 'X-Astoria-Client: my-script'
```

## 3. Routes

### 3.1 Health

`GET /health` → `200` iff the database answers, else `503`.

```json
{"status":"ok","version":"0.1.0","user_default":"alice",
 "db":{"facts_active":1240,"episodes_active":3180,"cognify_pending":0,"pgvector":"0.8.6"},
 "queue":{"pending":0,"dead":0,"by_state":{"done":412}},
 "tei":{"ok":true,"active":"http://gpu-box.local:4000","model":"nomic-embed",
        "endpoints":[{"url":"http://gpu-box.local:4000","model":"nomic-embed-text-v1.5","usable":true,"verified":true,"last_ms":61.2,"error":null},
                     {"url":"http://nas.local:8931","model":"nomic","usable":true,"verified":false,"last_ms":0,"error":null}],
        "cache":212},
 "llm":{"saint_url":"http://llm-gateway.local:4000/v1","model":"…","fallback":true,"saint":"reachable"},
 "rerank":{"ok":true,"status":"on","enabled":true,"active":"http://nas.local:8935","endpoints":[…],"top_n":30,"weight":0.6,"cache":140}}
```

`queue.pending` = pending + failed + running. `llm.saint` is the primary OpenAI-compatible gateway
(`reachable` / `http <code>` / `unreachable (<error>)`); `llm.fallback` says whether an Anthropic key is
configured. `rerank.status` ∈ `on | off | down`.

### 3.2 Recall, capture, briefing, profile

#### `POST /recall`

Request (all optional except `query`):

| field | default | meaning |
|---|---|---|
| `user_id` | default user | scope |
| `query` | `""` | natural-language query |
| `session_id` | — | also returns the last 4 turns of this session as `working`; this session's own turns are excluded from search |
| `layers` | all four | any of `profile, semantic, procedural, episodic` (list or comma string) |
| `max_tokens` | `ASTORIA_RECALL_TOKEN_BUDGET` (1000) | budget for `items` (≈ chars/4) |
| `limit` | `ASTORIA_RECALL_LIMIT` (12) | max items |
| `facts_only` | false | skip episodes |
| `include_profile` | false | add `profile: {narrative, version, facts}` |
| `as_of` | — | point in time on the valid axis (BM25-ranked; no vector stage) |
| `as_believed_at` | — | with `as_of`: belief axis |
| `rerank` | unset | `false` bypasses the cross-encoder stage for this call |
| `min_cosine` | setting | per-call cosine floor |

```bash
curl -s -X POST $A/recall -H "$H" -d '{"user_id":"alice","query":"what beer do I like","session_id":"s-42"}'
```

Response:

```json
{"user_id":"alice","query":"what beer do I like",
 "items":[
  {"id":"9b1c…","layer":"profile","kind":"fact","text":"alice favorite beer: IPA","subject":"alice",
   "predicate":"favorite_beer","value":"IPA","score":0.0184,"confidence":0.9,"source":"cli","source_trust":0.9,
   "asserted_at":"2026-08-20T17:02:11+00:00","valid_from":"2026-08-20T17:02:11+00:00","valid_to":null,
   "occurred_at":null,"last_seen":"2026-08-22T09:10:00+00:00","is_belief":false,"stale_hint":false,
   "cardinality":"functional","status":"active","rerank_score":3.21},
  {"id":"e77a…","layer":"episodic","kind":"episode","text":"…","subject":null,"predicate":null,"value":null,
   "score":0.0091,"confidence":0.6,"source":"assistant","source_trust":0.6,"asserted_at":null,"valid_from":null,
   "valid_to":null,"occurred_at":"2026-05-02T18:30:00+00:00","last_seen":null,"is_belief":false,"stale_hint":false,
   "episode_kind":"summary","session_id":"s-7"}],
 "working":[{"user_input":"…","agent_response":"…","occurred_at":"…"}],
 "profile":null,
 "context":"Relevant memory (current facts are authoritative; past conversation may be outdated):\n- alice favorite beer: IPA  [profile · 0.90]\n- from a past session (2026-05-02): …  [episodic]",
 "health":{"tei":"ok","degraded":false,"rerank":"on"},
 "snapshot_id":"…","as_of":null,"as_believed_at":null}
```

`context` is the block clients inject verbatim; it is `""` for an empty result. `rerank_score` appears
only on items the reranker scored. `stale_hint` marks a functional fact that a newer episode seems to
contradict.

#### `POST /capture`

| field | default | meaning |
|---|---|---|
| `user_id` | default user | |
| `kind` | `turn` | `turn` · `summary` · `note` · `import` |
| `text` | — | body for non-turn kinds (or a turn given as plain text) |
| `user_input` / `agent_response` | — | the two halves of a turn |
| `source` | caller identity | override the recorded client name |
| `session_id`, `occurred_at`, `importance` (0.5), `tags`, `meta` | | |
| `cognify` | true | enqueue LLM extraction |
| `priority` | `normal` | `high` → queue priority 1 |
| `sync` | setting | `true` embeds inline instead of the asynchronous backfill |

```bash
curl -s -X POST $A/capture -H "$H" -d '{"user_id":"alice","kind":"turn","session_id":"s-42",
  "user_input":"Actually, my favorite beer is stout","agent_response":"Noted."}'
```

```json
{"episode_id":"4c0d…","deduped":false,"dropped":null,
 "detector":{"op":"correct","subject":"alice","predicate":"favorite_beer","value":"stout",
             "fact_id":"a91e…","action":"superseded","superseded":["9b1c…"]},
 "queued":true}
```

`dropped` ∈ `empty | slash_command | ack | too_short` (then `episode_id` is null and nothing is queued).
`deduped:true` means the identical body was captured before (idempotent; not re-queued). `detector` is
null when no explicit-statement pattern matched; on a detector failure it carries `action:"error"` and
`error` (the episode is still stored).

#### `GET /briefing?user_id=alice&max_tokens=1200`

`{"narrative": "…", "facts": [fact…], "context": "Known about alice (authoritative, as of 2026-08-22):\n…"}` —
a stable prompt prefix (profile narrative + all profile facts + top-10 semantic facts). Empty user →
`context: ""`.

#### `GET /profile?user_id=alice[&limit=200]`

`{"user_id","narrative","version","rederived_at","facts":[profile-layer active facts]}`.

### 3.3 Facts (control plane)

#### `GET /facts`

Query: `user_id, subject, predicate, status (default active; any|active|staging|superseded|retracted|archived|deleted),
layer, q (hook ILIKE), limit (≤1000), offset` → `[fact…]` ordered by layer, subject, predicate, newest
assertion first.

#### `POST /facts` — assert an explicit fact

Body: `subject, predicate, value` (required); `user_id, valid_from, valid_to, asserted_at, confidence,
layer, tags, cardinality (functional|set), historical, importance, is_belief, evidence, meta, ref, sync,
source`.

```bash
curl -s -X POST $A/facts -H "$H" -d '{"user_id":"alice","subject":"alice","predicate":"favorite_beer","value":"Guinness"}'
# {"fact":{…,"status":"active"},"action":"inserted","superseded":[]}
curl -s -X POST $A/facts -H "$H" -d '{"user_id":"alice","subject":"alice","predicate":"favorite_beer","value":"IPA"}'
# {"fact":{…},"action":"superseded","superseded":["<old id>"]}
curl -s -X POST $A/facts -H "$H" -d '{"user_id":"alice","subject":"alice","predicate":"favorite_beer","value":"IPA"}'
# {"fact":{…,"access_count":1},"action":"noop","superseded":[]}
curl -s -X POST $A/facts -H "$H" -d '{"user_id":"alice","subject":"alice","predicate":"employer","value":"Acme","valid_from":"2019-01-01","valid_to":"2023-12-31"}'
# {"fact":{…,"status":"superseded"},"action":"historical","superseded":[]}
```

`action` ∈ `inserted | superseded | noop | historical | staging | blocked` (blocked: a tombstone refused a
non-explicit write — explicit writes lift tombstones, so `POST /facts` itself never blocks). `source_kind`
is always `explicit` (confidence .90 unless given).

#### `POST /correct`

Identical to `POST /facts` (a semantic alias: "supersede the current value").

#### `GET /facts/{id}?user_id=alice` → fact · `404`

#### `PATCH /facts/{id}`

Body: any of `value, confidence, importance, tags, layer, valid_from, valid_to, asserted_at, is_belief,
ref, status (active|archived|staging), evidence, detail` (+ `user_id`). Changing `value` re-renders the
hook and re-embeds (inline with `ASTORIA_EMBED_SYNC=true`, otherwise nulled and backfilled by the worker).
→ updated fact · `404`.

#### `DELETE /facts/{id}?user_id=alice&mode=soft|hard`

`{"deleted":true,"mode":"soft","fact_id":"…"}` · `404`. `soft` archives (row kept, not recalled), `hard`
removes the row. Both tombstone the triple.

#### `POST /retract` — "no longer true"

Body: `fact_id` **or** `subject` + `predicate` [+ `value`] (+ `user_id`, `source_kind`, `reason`).
→ `{"retracted":["id",…],"facts":[fact…]}`. Rows become `status='retracted'`, `expired_at=now`
(belief closed, `valid_to` untouched); each triple is tombstoned.

#### `POST /forget` — remove from memory

Body: `fact_id` · or `subject`[+`predicate`[+`value`]] · or `query` (hybrid search; `limit` default 1 — the
best match only, widen deliberately); `mode` `soft|hard`.
→ `{"forgotten":[{"id","subject","predicate","value","mode"}]}` · `404` when a given `fact_id` is unknown.

#### `POST /approve` `{user_id, fact_id}`

Promotes a `staging` row (re-asserted as explicit, confidence ≥ 0.8, through the supersede path; the
staging row is archived). → `{"fact": {…active…}}` · `404`. A non-staging id is returned unchanged.

#### `GET /history?user_id=alice&subject=alice&predicate=favorite_beer`

`[fact…]` — the chain for a key, newest assertion first, all statuses; belief-closed originals (rows
superseded by a versioned copy) are hidden. `subject` defaults to the user; `400` without `predicate`.

#### `POST /as_of`

Body: `at` (required), `as_believed_at`, `subject`, `predicate`, `query` (substring filter on the hook),
`limit` (50). → `[fact…]` — the newest assertion per key that was valid at `at` (current belief), or, with
`as_believed_at`, the rows that were ingested and not expired by that instant — whatever their status now.

```bash
curl -s -X POST $A/as_of -H "$H" -d '{"user_id":"alice","at":"2026-07-15","predicate":"favorite_beer"}'
# [{"value":"Guinness","status":"superseded","valid_from":"…","valid_to":"2026-08-20T…",…}]
curl -s -X POST $A/as_of -H "$H" -d '{"user_id":"alice","at":"2026-08-22","as_believed_at":"2026-08-19","predicate":"favorite_beer"}'
# what the store believed on 08-19: Guinness, still open-ended
```

### 3.4 Target resolver (LLM on demand)

#### `POST /resolve` `{user_id, text, limit=8}`

Turns a natural-language memory instruction into a **plan without applying it**.

```bash
curl -s -X POST $A/resolve -H "$H" -d '{"user_id":"alice","text":"forget the thing about Guinness"}'
```

```json
{"intent":"forget",
 "targets":[{…fact row…,"reason":"favorite_beer history mentions Guinness"}],
 "new_fact":null,"confidence":0.91,
 "explanation":"Forget the favorite_beer=Guinness fact.",
 "requires_confirmation":false,"text":"forget the thing about Guinness","candidates":14}
```

`intent` ∈ `forget | retract | correct | remember | none`; `new_fact` = `{subject, predicate, value,
valid_from}` for `correct`/`remember`; `requires_confirmation` is false only for `remember`, `none`, or
exactly one target with confidence ≥ 0.85. On failure: `{"intent":"none", …, "error":"…",
"error_kind":"bad_input"|"llm_unavailable"|"invalid_plan"}` — `llm_unavailable` is returned with HTTP
`503`.

#### `POST /resolve/apply` `{user_id, plan?, text?, confirm=false, limit=8}`

Apply a plan from `/resolve`, or resolve `text` and apply it in one step. → `{"applied": bool, "plan":
{…}, "intent", "changed":[{op, fact, …}], "superseded":[ids], "fact": new row|null, "action",
"reason"?}`. `applied:false` with `reason:"requires_confirmation"` when the plan asks for confirmation and
`confirm` is not true; `reason:"no memory operation"` for `intent none`. Execution is deterministic:
`forget` → soft forget per target; `retract` → retract per target (explicit, reason `resolved`); `correct`
→ explicit upsert with `contradicts=targets`; `remember` → explicit upsert.

### 3.5 Episodes

- `GET /episodes?user_id&session_id&kind&status=active&limit=50&offset=0` → `[episode…]` newest first.
- `GET /episodes/{id}?user_id` → episode · `404`.
- `DELETE /episodes/{id}?user_id` → `{"deleted":true,"episode_id":"…"}` · `404` (hard delete; queue rows
  cascade; facts keep lineage with `origin_episode` nulled).

### 3.6 Predicates, audit

- `GET /predicates` → `[{name, cardinality, layer_hint, auto, description, created_at}…]`.
- `PATCH /predicates/{name}` `{cardinality?, layer_hint?, description?}` → the row (`auto` cleared). A
  `set → functional` flip makes the next upsert on that predicate supersede.
- `GET /audit?user_id&limit=50&offset=0` → `[{id, user_id, actor, op, target, detail, created_at}…]`
  newest first. Ops include `inserted, superseded, noop, historical, staging, conflict_staged,
  blocked_tombstone, retract, forget_soft, forget_hard, update, edge_add, edge_noop, edge_retract,
  alias_add, alias_delete, predicate_update, episode_delete, user_wipe, curator-dedup, curator-decay`.

### 3.7 Graph layer and aliases

Nodes are written as `entity:<name>`, `fact:<uuid>`, a bare uuid (→ fact) or a bare name (→ entity, alias
resolved).

- `GET /graph?node=<ref>&user_id&depth&fanout` → `{"root":"entity:…","depth":2,"nodes":[{id, kind, name, hops,
  via, direction, path, label, …}],"edges":[edge…],"counts":{"nodes":n,"edges":m}}` — the induced subgraph
  within `depth` hops (entity nodes carry `entity_kind, summary, aliases, facts`; fact nodes carry
  `subject, predicate, value, status, layer, confidence`).
- `GET /edges?user_id&node&relation&depth=0&status=active&limit=200&offset=0` → `[edge…]` (edges touching
  `node`, or any node within `depth` hops of it; `status=any` lifts the filter). Edge rows carry `src` /
  `dst` refs in addition to the kind/id columns.
- `POST /edges` `{src, relation, dst, src_kind?, dst_kind?, weight?, confidence?, valid_from?, valid_to?,
  evidence?, meta?, source?, source_kind?}` → `{"edge": {…}, "action": "inserted"|"noop"}`; `404` when a
  fact endpoint does not exist; `400` for a self-loop.
- `DELETE /edges/{id}?user_id&mode=retract|archive|hard` → `{"deleted": bool, "mode", "edge": {…}}` ·
  `404`.
- `GET /aliases?user_id&canonical&limit&offset` → `[{user_id, alias, canonical, source, source_kind,
  created_at}…]`.
- `POST /aliases` `{alias, canonical}` → `{"alias": {…}, "action": "inserted"|"updated"|"noop",
  "repointed": n}`; `400` for self-alias or aliasing the user id.
- `DELETE /aliases/{alias}?user_id` → `{"deleted":true,"alias":{…}}` · `404`.

### 3.8 Dispatcher and admin

- `POST /op` `{"action": "<name>", …params}` — every action by name, for scripts. Actions: `queue_stats,
  recall, capture, briefing, profile, facts_list, fact_get, fact_add, fact_update, fact_delete, correct,
  retract, forget, approve, history, as_of, episodes_list, episode_get, episode_delete, predicates_list,
  predicate_update, audit, health, user_wipe, retrieve, memories_add, user_profile, graph, edges_list,
  edge_add, edge_delete, aliases_list, alias_add, alias_delete, resolve, resolve_apply`. Unknown action →
  `400`.
- `POST /op {"action":"queue_stats"}` → `{"by_state":{…},"oldest":{state: iso},"pending":n,"dead":n,
  "dead_jobs":[{id,user_id,episode_id,attempts,last_error,next_attempt_at}…(≤20)],
  "embed_backlog":{"facts":n,"episodes":n}}`.
- `DELETE /users/{user_id}` → `{"deleted":true,"user_id":"…","counts":{table: rows}}` — removes the
  user's rows from `cognify_queue, snapshot, edge, alias, entity, fact, episode, tombstone,
  profile_history, profile, audit` (then writes one audit row). No confirmation.

### 3.9 Compatibility routes

Kept for integrations written against an earlier memory service's API; new clients should use `recall`
and `capture`.

| route | body | response |
|---|---|---|
| `POST /retrieve` | `{user_id, query}` | `{user_id, query, short_term_history:[{user_input, agent_response, timestamp}] (last 4 turns, oldest first), retrieved_pages:[{user_input, agent_response, timestamp, meta_info}], retrieved_user_knowledge:[{knowledge, timestamp}], retrieved_assistant_knowledge:[], user_profile: "<narrative>" or "None"}` |
| `POST /memories` | `{user_id, user_input, agent_response, timestamp?, session_id?}` | `{status:"ok", user_id, episode_id, deduped, queued}` (a `turn` capture) |
| `GET /users/{id}/profile` | — | `{user_id, user_profile}` (`"None"` when empty) |

## 4. MCP tools (`/mcp/`, streamable HTTP)

Connect an MCP client to `http://nas.local:8933/mcp/` (trailing slash). Identity comes from the same HTTP
headers as REST (`Authorization: Bearer …` or `X-Astoria-Client`); without headers the client is `mcp`.
Every tool takes `user_id` (default `""` → the server applies `ASTORIA_USER_DEFAULT`).

| tool | signature | behaviour |
|---|---|---|
| `recall` | `(query, user_id, layers=None, limit=12, max_tokens=1000, as_of="", include_profile=False, session_id="", facts_only=False)` | `POST /recall`; the docstring instructs agents to inject `context` verbatim |
| `capture` | `(text="", user_input="", agent_response="", kind="note", user_id, session_id="", source="", importance=0.5, tags=None, cognify=True, priority="normal")` | `POST /capture` |
| `remember` | `(subject, predicate, value, user_id, valid_from="", valid_to="", retract=False, layer="", confidence=None, tags=None)` | `POST /facts`; with `retract=true` → `POST /retract` on the triple |
| `forget` | `(fact_id="", subject="", predicate="", value="", query="", mode="soft", user_id)` | `POST /forget` |
| `memory` | `(action, user_id, …)` | admin dispatcher; list results come back as `{"action", "count", "items"}` |
| `retrieve_memory` | `(user_id, query)` | compat `POST /retrieve` |
| `add_memory` | `(user_id, user_input, agent_response, timestamp="")` | compat `POST /memories` |
| `get_user_profile` | `(user_id)` | compat `GET /users/{id}/profile` |

`memory(action=…)` actions and the parameters they read:

| action | params | dispatches to |
|---|---|---|
| `resolve` | `text`, `limit` | `resolve` |
| `resolve_apply` | `plan` or `text`, `confirm`, `limit` | `resolve_apply` |
| `list` / `facts` | `subject, predicate, status, layer, q, limit` | `facts_list` |
| `get` | `fact_id` | `fact_get` |
| `update` | `fact_id` + `value, status, confidence, importance, layer, valid_from, valid_to, tags` | `fact_update` |
| `delete` | `fact_id` | `fact_delete` |
| `history` | `subject, predicate` | `history` |
| `as_of` | `at, as_believed_at, subject, predicate` | `as_of` |
| `profile` | — | `profile` |
| `briefing` | `max_tokens` | `briefing` |
| `predicates` | `name` + `cardinality`/`layer_hint` to update, else list | `predicate_update` / `predicates_list` |
| `approve` | `fact_id` | `approve` |
| `episodes` | `session_id, kind, limit` | `episodes_list` |
| `audit` | `limit` | `audit` |
| `health` | — | `health` |
| `graph` | `node, depth` | `graph` |
| `edges` | `node, relation, depth` | `edges_list` |
| `edge_add` | `src, relation, dst, confidence, evidence` | `edge_add` |
| `edge_delete` | `edge_id` | `edge_delete` |
| `aliases` | `canonical` | `aliases_list` |
| `alias_add` | `alias, canonical` | `alias_add` |
| `alias_delete` | `alias` | `alias_delete` |

Handshake check from a shell (what `scripts/smoke.sh` does):

```bash
curl -s -D - -X POST $A/mcp/ -H "$H" -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# → 200, mcp-session-id header, body/SSE containing "serverInfo"; then notifications/initialized + tools/list
```

## 5. Error shapes, in one place

| situation | HTTP | body |
|---|---|---|
| missing/invalid parameter, bad uuid, unknown action | 400 | `{"error": "<message>"}` |
| write without a token while `ASTORIA_REQUIRE_TOKEN=true` | 401 | `{"error": "action '…' requires a client token (ASTORIA_REQUIRE_TOKEN=true)"}` |
| fact / episode / edge / alias / predicate not found | 404 | `{"error": "… not found"}` |
| a sibling module not importable, or the LLM unreachable for `/resolve` | 503 | `{"error": "module not ready"}` / `{…plan…, "error": "llm unavailable: …", "error_kind": "llm_unavailable"}` |
| database down | 503 on `/health` (`status: "error"`), errors elsewhere | |
| embedder / reranker down | **200** — degraded results flagged in `health` | `{"health": {"tei": "down", "degraded": true, "rerank": "down"}}` |
