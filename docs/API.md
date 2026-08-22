# Astoria — API reference (REST + MCP)

Base URL: **`http://192.168.1.134:8933`** (NAS, LAN-only). MCP: **`http://192.168.1.134:8933/mcp/`**
(streamable-HTTP, trailing slash required). Interactive OpenAPI: `GET /docs`. Root: `GET /` →
`{"service":"astoria","version":"0.1.0","docs":"/docs","mcp":"/mcp/"}`.

Every REST route, `POST /op`, and every MCP tool goes through the same dispatcher
(`astoria/api/service.py: do_action(action, params, client)`), so the two surfaces always agree. This
page is the route-by-route reference; the semantics are in [ARCHITECTURE.md](ARCHITECTURE.md) and the
fixed contract in [CONTRACT.md](CONTRACT.md).

---

## 1. Conventions

- **JSON in, JSON out.** `Content-Type: application/json` on POST/PATCH. Timestamps are ISO-8601
  (naive values are read as UTC; epoch numbers accepted); responses use `+00:00` offsets.
- **`user_id`** is accepted by every action (body field or query param) and defaults to
  `ASTORIA_USER_DEFAULT` (`rick`). All data is scoped by it.
- **Ids** are UUIDs. The CLI accepts short prefixes; the API needs full UUIDs.
- **Errors**: `{"error": "<message>"}` with status **400** (validation / unknown action / bad uuid),
  **404** (`fact not found`, `episode not found`, `predicate not found`), **503** (`module not ready`,
  or `/health` when the DB is down). The body never contains a stack trace. Examples:

  ```
  HTTP/1.1 400  {"error":"unknown action: 'nope'. valid: recall, capture, briefing, profile, facts_list, ..."}
  HTTP/1.1 404  {"error":"fact not found"}
  HTTP/1.1 400  {"error":"retract needs fact_id or subject+predicate"}
  ```
- **TEI / LLM outages never 500.** Recall degrades to BM25 (`health.degraded=true`); capture still stores
  the episode; cognify retries later.
- **Lists** return a bare JSON array (`GET /facts`, `/episodes`, `/history`, `/as_of`, `/predicates`,
  `/audit`); via MCP `memory(...)` they are wrapped as `{action, count, items}`.

## 2. Identity, auth, trust

| header | effect |
|---|---|
| `Authorization: Bearer <token>` | token looked up in `ASTORIA_CLIENT_TOKENS` (`input:tok,claude-code:tok,cli:tok,megaplan:tok`) → **proven** client name |
| `X-Astoria-Client: <name>` | unauthenticated hint (≤ 64 chars), trusted as-is on the LAN |
| neither | client `anonymous` (MCP tool calls without headers: `mcp`) |

The resolved client name becomes `fact.source`, the audit `actor`, `snapshot.client`, and selects the
trust cap (`cli` 1.0 · `input`/`claude-code` .85 · `api`/`mcp` .7 · `megaplan` .6 · `anonymous` .5 ·
unknown .6). **Writes are allowed without a token** (LAN posture, see [SECURITY.md](SECURITY.md)); a
token only raises the trust cap and proves the `source`. The CLI sends `ASTORIA_TOKEN` automatically;
the tokens live in `~/.config/astoria/env` on the workstation and `/volume1/docker/astoria/.env` on the NAS.

```bash
# shorthand used below
A=http://192.168.1.134:8933
H='Content-Type: application/json'
# optional:  -H "Authorization: Bearer $ASTORIA_TOKEN"   or   -H 'X-Astoria-Client: my-script'
```

## 3. Routes

### 3.1 Health

#### `GET /health` → 200 iff the DB is reachable, else 503

```bash
curl -s $A/health | jq .
```
```json
{"status":"ok","version":"0.1.0",
 "db":{"facts_active":12,"episodes_active":2,"cognify_pending":0,"pgvector":"0.8.6"},
 "queue":{"pending":0,"dead":0,"by_state":{"done":1}},
 "tei":{"ok":true,"model":"/models/nomic-embed-text-v1.5","url":"http://192.168.1.134:8931"},
 "llm":{"saint_url":"http://192.168.1.221:4000/v1","model":"saint-cloud-medium","fallback":true,"saint":"reachable"}}
```
`queue.pending` = pending + failed + running. `llm.fallback` = an `ANTHROPIC_API_KEY` is configured.
`tei.ok=false` → vector recall is degraded (still 200). `llm.saint` is `reachable | http NNN | unreachable (Error)`.

### 3.2 Recall / capture / briefing / profile

#### `POST /recall`

Request body:

| field | type | default | meaning |
|---|---|---|---|
| `user_id` | str | `rick` | |
| `query` | str | `""` | natural-language query (hybrid vector + BM25). Empty → no search (still returns working/profile) |
| `session_id` | str | – | returns the last 4 `turn`s of this session as `working`; excludes them from the episodic search |
| `layers` | list or comma-str | all four | subset of `profile, semantic, procedural, episodic` |
| `max_tokens` | int 50–20000 | 1000 | budget for the rendered `context` (≈ chars/4) |
| `limit` | int 1–200 | 12 | max items after collapse |
| `facts_only` | bool | false | skip episodes |
| `include_profile` | bool | false | add `profile: {narrative, version, facts}` |
| `as_of` | ISO | – | time travel on the valid axis (facts ranked by BM25 only) |
| `as_believed_at` | ISO | – | also restrict to what the system believed then |

```bash
curl -s -X POST $A/recall -H "$H" -d '{"user_id":"rick","query":"what editor do I use","limit":3}' | jq .
```
```json
{"user_id":"rick","query":"what editor do I use",
 "items":[
  {"id":"43209a50-…","layer":"profile","kind":"fact","text":"rick favorite editor: Neovim",
   "subject":"rick","predicate":"favorite_editor","value":"Neovim","score":0.0267,"confidence":0.9,
   "source":"cli","source_trust":0.9,"asserted_at":"2026-08-22T17:11:25.234542+00:00",
   "valid_from":"2026-08-22T17:11:25.234542+00:00","valid_to":null,"occurred_at":null,
   "last_seen":"2026-08-22T17:12:11.633907+00:00","is_belief":false,"stale_hint":false,
   "cardinality":"functional","status":"active"},
  {"id":"…","layer":"episodic","kind":"episode","text":"…","subject":null,"predicate":null,"value":null,
   "score":0.0123,"confidence":0.6,"source":"claude-code","source_trust":0.6,"occurred_at":"2026-08-20T…",
   "episode_kind":"summary","session_id":"…","is_belief":false,"stale_hint":false}],
 "working":[{"user_input":"…","agent_response":"…","occurred_at":"…"}],
 "profile":null,
 "context":"Relevant memory (current facts are authoritative; past conversation may be outdated):\n- rick favorite editor: Neovim  [profile · 0.90]\n- from a past session (2026-08-20): …  [episodic]",
 "health":{"tei":"ok","degraded":false},
 "snapshot_id":"…","as_of":null,"as_believed_at":null}
```
Inject `context` verbatim. Empty store → `items: []`, `context: ""`.

#### `POST /capture`

| field | type | default | meaning |
|---|---|---|---|
| `user_id` | str | `rick` | |
| `kind` | `turn` \| `summary` \| `note` | `turn` (REST) / `note` (MCP) | `import` is reserved for the importer |
| `text` | str | – | for note/summary (or a one-sided turn) |
| `user_input` + `agent_response` | str | – | for a turn |
| `source` | str | the caller's client name | overrides the `source` stored on the episode |
| `session_id` | str | – | conversation id (working memory, cognify coalescing, dedupe key) |
| `occurred_at` | ISO | now | when it happened |
| `importance` | float | 0.5 | |
| `tags` | list | `[]` | |
| `meta` | object | `{}` | |
| `cognify` | bool | true | queue LLM fact extraction |
| `priority` | `normal` \| `high` | `normal` | `high` → queue priority 1 |

```bash
curl -s -X POST $A/capture -H "$H" -d '{"user_id":"rick","kind":"note","text":"Actually, my favorite beer is stout","cognify":false}'
```
```json
{"episode_id":"0b6f…","deduped":false,"dropped":null,
 "detector":{"op":"correct","subject":"rick","predicate":"favorite_beer","value":"stout","fact_id":"a04…","action":"superseded","superseded":["f4dd…"]},
 "queued":false}
```
Dropped noise → `{"episode_id":null,"deduped":false,"dropped":"slash_command|ack|too_short|empty","detector":null,"queued":false}`.
Re-sending identical content → `deduped: true, queued: false`. A detector failure is reported as
`detector.action="error"` with `detector.error` — the episode is still stored.
Missing text → 400 `capture needs text or user_input/agent_response`.

#### `GET /briefing?user_id=rick&max_tokens=1200`

```bash
curl -s "$A/briefing?user_id=rick&max_tokens=600" | jq -r .context
```
→ `{narrative, facts:[fact…], context}`; `context` starts `Known about rick (authoritative, as of 2026-08-22):`
followed by the narrative and `- rick favorite editor: Neovim  [profile · 0.90]` lines. `""` when nothing is known.

#### `GET /profile?user_id=rick[&limit=200]`
→ `{user_id, narrative, version, rederived_at, facts:[profile-layer active facts]}`.

### 3.3 Facts (control plane)

Fact object (as returned everywhere; vectors/tsvectors are never serialized):

```json
{"id":"53649784-…","user_id":"rick","subject":"rick","predicate":"communication_preference","cardinality":"functional",
 "value":"concise, direct answers, no filler","hook":"rick communication preference: concise, direct answers, no filler",
 "detail":null,"layer":"profile","valid_from":"2026-08-22T17:11:33.706455+00:00","valid_to":null,
 "asserted_at":"2026-08-22T17:11:33.706455+00:00","ingested_at":"2026-08-22T17:11:33.706836+00:00","expired_at":null,
 "status":"active","supersedes":null,"superseded_by":null,"confidence":0.9,"source":"cli","source_kind":"explicit",
 "source_trust":0.9,"is_belief":false,"importance":0.5,"last_seen":"…","access_count":4,"corroborations":0,
 "tags":[],"origin_episode":null,"evidence":null,"ref":null,"meta":{}}
```

#### `GET /facts` — list
Query: `user_id`, `subject`, `predicate`, `status` (`active` default · `staging` · `superseded` · `retracted` ·
`archived` · `any`), `layer`, `q` (ILIKE over the hook), `limit` (1–1000, default 50), `offset`.
```bash
curl -s "$A/facts?user_id=rick&predicate=favorite_beer&status=any" | jq '.[] | {value,status,asserted_at}'
```

#### `POST /facts` — assert an explicit fact (source_kind `explicit`, confidence .90)
Body: `subject`, `predicate`, `value` (required); `user_id`, `valid_from`, `valid_to`, `confidence`,
`layer`, `tags`, `cardinality` (`functional|set`, overrides/updates the registry), `historical` (bool),
`importance`, `is_belief`, `evidence`, `ref`, `meta`, `source`.
```bash
curl -s -X POST $A/facts -H "$H" -d '{"subject":"rick","predicate":"favorite_beer","value":"Guinness"}'
# {"fact":{…,"status":"active"},"action":"inserted","superseded":[]}
curl -s -X POST $A/facts -H "$H" -d '{"subject":"rick","predicate":"favorite_beer","value":"IPA"}'
# {"fact":{…},"action":"superseded","superseded":["f4dd893d-…"]}
curl -s -X POST $A/facts -H "$H" -d '{"subject":"rick","predicate":"favorite_beer","value":"IPA"}'
# {"fact":{…,"access_count":1},"action":"noop","superseded":[]}
curl -s -X POST $A/facts -H "$H" -d '{"subject":"rick","predicate":"employer","value":"Acme","valid_from":"2019-01-01","valid_to":"2023-12-31","historical":true}'
# {"fact":{…,"status":"superseded"},"action":"historical","superseded":[]}
```
`action ∈ inserted | superseded | noop | historical | staging | blocked` (`blocked` only for non-explicit
writes hitting a tombstone — not reachable from this route). Errors: 400 `subject required`, `cardinality must be functional|set`.

#### `POST /correct` — identical to `POST /facts` (semantic alias used by clients for "supersede the current value").

#### `GET /facts/{id}?user_id=rick` → fact · 404
#### `PATCH /facts/{id}` — direct edit
Body: any of `value, confidence, importance, tags, layer, valid_from, valid_to, asserted_at, is_belief, ref,
status (active|archived|staging), evidence, detail` (+ `user_id`). Changing `value` re-renders the hook and
re-embeds. → the updated fact · 404 · 400 `status must be active|archived|staging (use retract/forget otherwise)`.
```bash
curl -s -X PATCH $A/facts/$ID -H "$H" -d '{"value":"IPA (hazy)","importance":0.8}'
```
#### `DELETE /facts/{id}?user_id=rick&mode=soft|hard` → `{"deleted":true,"mode":"soft","fact_id":"…"}` · 404
soft = `status='archived'` + tombstone; hard = row removed + tombstone.

#### `POST /retract` — "no longer true" (status `retracted`, history kept, tombstone)
Body: `fact_id` **or** `subject`+`predicate` (+ optional `value` to retract one member of a set); `user_id`.
```bash
curl -s -X POST $A/retract -H "$H" -d '{"subject":"rick","predicate":"uses_tool","value":"Emacs"}'
# {"retracted":["…"],"facts":[{…,"status":"retracted"}]}
```
Errors: 400 `retract needs fact_id or subject+predicate`, `retract by triple needs predicate`. No match → `{"retracted":[],"facts":[]}`.

#### `POST /forget` — remove from memory (soft = archive, hard = delete); always tombstones
Body: `fact_id` **or** `subject`[+`predicate`[+`value`]] **or** `query` (semantic search; only the best match
unless `limit` is raised, max 50); `mode` `soft`|`hard`; `user_id`.
```bash
curl -s -X POST $A/forget -H "$H" -d '{"query":"old address","mode":"soft"}'
# {"forgotten":[{"id":"…","subject":"rick","predicate":"location","value":"Portland","mode":"soft"}]}
```
Errors: 400 `forget needs fact_id, subject/predicate, or query`, `mode must be soft|hard`; 404 only when a given `fact_id` does not exist.

#### `POST /approve` `{user_id, fact_id}` → `{"fact":{…active…}}` — promote a `staging` row (404 if missing; a non-staging id is returned unchanged).

#### `GET /history?user_id=rick&subject=rick&predicate=favorite_beer` → `[fact…]` newest assertion first (all statuses). `subject` defaults to the user. 400 `subject and predicate required` without `predicate`.

#### `POST /as_of` — point in time
Body: `at` (ISO, required), `as_believed_at`, `subject`, `predicate`, `query` (substring filter on the hook), `limit` (≤ 1000), `user_id`.
```bash
curl -s -X POST $A/as_of -H "$H" -d '{"at":"2026-07-15","predicate":"default_johnny_profile"}'
# [{"value":"coder","status":"superseded","valid_from":"2026-07-01T…","valid_to":"2026-08-18T…",…}]
```
Rows with `valid_from ≤ at < valid_to` and status `active|superseded`, newest assertion per key; with
`as_believed_at` also `ingested_at ≤ B < expired_at`. See the belief-axis limitation in ARCHITECTURE §8.

### 3.4 Episodes

- `GET /episodes?user_id&session_id&kind&status=active&limit=50&offset=0` → `[episode…]` newest first.
  Episode object: `{id, user_id, kind, hook, body, occurred_at, ingested_at, source, session_id, importance,
  access_count, last_seen, status, processed_at, tags, meta}`.
- `GET /episodes/{id}?user_id` → episode · 404.
- `DELETE /episodes/{id}?user_id` → `{"deleted":true,"episode_id":"…"}` · 404 (facts already extracted are kept; the queued job is skipped; audited).

### 3.5 Predicates / audit

- `GET /predicates` → `[{name, cardinality, layer_hint, auto, description, created_at}…]` (33 today: 30 seeded + auto-registered).
- `PATCH /predicates/{name}` body `{cardinality?: functional|set, layer_hint?: semantic|profile|procedural, description?}` → the row (clears `auto`); with an empty body → the current row; 404 · 400 `cardinality must be functional|set`.
  ```bash
  curl -s -X PATCH $A/predicates/collects_x -H "$H" -d '{"cardinality":"set","layer_hint":"profile"}'
  ```
- `GET /audit?user_id&limit=50&offset=0` → `[{id, user_id, actor, op, target, detail, created_at}…]` newest first.

### 3.6 Dispatcher and admin

- `POST /op` `{"action": "<name>", ...params}` — raw mirror of every action (scripts, MegaPlan precedent).
  Actions: `recall capture briefing profile facts_list fact_get fact_add fact_update fact_delete correct retract
  forget approve history as_of episodes_list episode_get episode_delete predicates_list predicate_update audit
  health user_wipe retrieve memories_add user_profile`. Unknown → 400 listing the valid names.
  ```bash
  curl -s -X POST $A/op -H "$H" -d '{"action":"history","subject":"rick","predicate":"favorite_beer"}'
  ```
- `DELETE /users/{user_id}` → `{"deleted":true,"user_id":"…","counts":{"cognify_queue":n,"snapshot":n,"fact":n,"episode":n,"tombstone":n,"profile_history":n,"profile":n,"audit":n}}` — wipes one user (tests / throwaway users). **No confirmation, no auth.** The CLI wraps it in `wipe-user --yes` + typed confirmation.

### 3.7 MemoryOS-compat routes (kept for stragglers; prefer the native ones)

| route | body | response |
|---|---|---|
| `POST /retrieve` | `{user_id, query, limit?}` | `{user_id, query, short_term_history:[{user_input, agent_response, timestamp}] (last 4 turns, any session), retrieved_pages:[{user_input, agent_response, timestamp, meta_info}], retrieved_user_knowledge:[{knowledge, timestamp}], retrieved_assistant_knowledge:[], user_profile: "<narrative>" or "None"}` |
| `POST /memories` | `{user_id, user_input, agent_response, timestamp?, session_id?}` | `{status:"ok", user_id, episode_id, deduped, queued}` (= capture a turn; 400 when both texts are empty) |
| `GET /users/{id}/profile` | – | `{user_id, user_profile: "<narrative>" or "None"}` |

## 4. MCP tools (`/mcp/`)

Transport: MCP streamable-HTTP (JSON-RPC over POST, `Accept: application/json, text/event-stream`,
`Mcp-Session-Id` header after `initialize`). Register in Claude Code (`~/.claude.json`):

```json
{"mcpServers": {"astoria": {"type": "http", "url": "http://192.168.1.134:8933/mcp/"}}}
```
(optionally `"headers": {"Authorization": "Bearer <token>"}` — see `deploy/clients/claude-mcp.json`).
`input` points `memory_url` at the same URL. Tool docstrings in
[`astoria/api/mcp_tools.py`](../astoria/api/mcp_tools.py) are the descriptions agents see; a skill that
teaches when to call them is `deploy/clients/skill-astoria.md` (installed at `~/.claude/skills/astoria`).

| tool | arguments (defaults) | returns / notes |
|---|---|---|
| `recall` | `query`, `user_id="rick"`, `layers=None`, `limit=12`, `max_tokens=1000`, `as_of=""`, `include_profile=False`, `session_id=""`, `facts_only=False` | the `/recall` dict; inject `context` verbatim |
| `capture` | `text=""`, `user_input=""`, `agent_response=""`, `kind="note"`, `user_id="rick"`, `session_id=""`, `source=""`, `importance=0.5`, `tags=None`, `cognify=True`, `priority="normal"` | `{episode_id, deduped, dropped?, detector?, queued}` |
| `remember` | `subject`, `predicate`, `value`, `user_id="rick"`, `valid_from=""`, `valid_to=""`, `retract=False`, `layer=""`, `confidence=None`, `tags=None` | `{fact, action, superseded}`; with `retract=True` → `{retracted:[ids], facts}` |
| `forget` | `fact_id=""`, `subject=""`, `predicate=""`, `value=""`, `query=""`, `mode="soft"`, `user_id="rick"` | `{forgotten:[{id, subject, predicate, value, mode}]}` |
| `memory` | `action` + only what it needs: `list [subject, predicate, status, layer, q, limit]` · `get fact_id` · `update fact_id [value, status, confidence, importance, layer, valid_from, valid_to, tags]` · `history subject, predicate` · `as_of at [as_believed_at, subject, predicate]` · `profile` · `briefing [max_tokens]` · `predicates [name + cardinality/layer_hint]` · `approve fact_id` · `episodes [session_id, kind, limit]` · `audit [limit]` · `health` · (also `facts`, `delete fact_id`) | lists come back as `{action, count, items}`; unknown action → `{error}` |
| `retrieve_memory` | `user_id`, `query` | MemoryOS shape (= `/retrieve`) |
| `add_memory` | `user_id`, `user_input`, `agent_response`, `timestamp=""` | = `/memories` |
| `get_user_profile` | `user_id` | = `/users/{id}/profile` |

Empty-string/None arguments are dropped before dispatch, so defaults apply server-side. Caller identity
comes from the HTTP headers of the MCP request (Bearer / `X-Astoria-Client`), else `mcp`.

Handshake check from a shell (what `scripts/smoke.sh` does):
```bash
curl -s -D - -X POST $A/mcp/ -H "$H" -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# → 200, header mcp-session-id: …, body/SSE with "serverInfo"; then notifications/initialized + tools/list
```

## 5. Client wiring (who calls what)

| client | how |
|---|---|
| `input` (terminal agent) | `settings.json` `memory_url: http://192.168.1.134:8933/mcp/`; MCP server claimed/hidden; `/memory …` subcommands (browse/facts/profile/remember/correct/retract/forget/history) and a built-in `memory` tool; ambient `recall` before a turn / `capture` after |
| Claude Code hooks | `~/.claude/hooks/nova_recall.py` (SessionStart: `GET /briefing` + `POST /recall`, layers profile/semantic/procedural) · `nova_capture.py` (SessionEnd: `POST /capture kind=summary`, summary via SAINT); header `X-Astoria-Client: claude-code`; optional `NOVA_MEMORY_TOKEN` |
| Claude Code MCP | `~/.claude.json` `mcpServers.astoria` → `/mcp/`; skill `~/.claude/skills/astoria` |
| MegaPlan | `MEGAPLAN_MEMORY_URL=http://192.168.1.134:8933/recall`, header `X-Astoria-Client: megaplan`, optional `MEGAPLAN_MEMORY_TOKEN`; `/health` reports `memory: reachable` |
| `astoria` CLI | REST; `ASTORIA_URL` / `ASTORIA_TOKEN` / `ASTORIA_USER` (see [CLI.md](CLI.md)) |
