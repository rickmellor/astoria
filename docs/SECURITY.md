# Astoria — security posture

Astoria is a **LAN-only, single-operator** service. This page states plainly what protects it, what
does not, where the secrets are, and what leaves the box.

## 1. Network posture

| endpoint | bound to | auth | notes |
|---|---|---|---|
| REST `:8933` + MCP `/mcp/` | `0.0.0.0` on the NAS (192.168.1.134) → reachable from the whole LAN | **none required** | plain HTTP, no TLS; the LAN is the trust boundary |
| Postgres `:8934` | `127.0.0.1` on the NAS only | password (`POSTGRES_PASSWORD`) | never exposed to the LAN; reach it with `docker exec` on the NAS |
| TEI `:8931` (`memoryos-tei`) | `0.0.0.0` on the NAS | none | read-only embedder |
| SAINT `:4000` (workstation) | LAN | none | called by cognify only; routes to billed cloud seats — see the infra docs' note on SAINT |

Consequences: **anyone on the LAN can read, write, correct, forget and wipe memory** (`DELETE
/users/{id}` has no confirmation). There is no rate limiting and no TLS. This is the same posture as
MemoryOS/MegaPlan before it and is acceptable for a private home LAN; it is **not** safe to port-forward
or expose through a reverse proxy without adding authentication in front.

Hardening options, if the posture ever changes: require a valid Bearer token for writes (the token map
already exists — make `anonymous` read-only in `service.do_action`); put the port behind a reverse proxy
with TLS + auth; bind `:8933` to the NAS's LAN address only; firewall rules on the NAS.

## 2. Identity and trust model

- `Authorization: Bearer <token>` → looked up in `ASTORIA_CLIENT_TOKENS` (`name:token,…`) → a **proven**
  client name (`input`, `claude-code`, `cli`, `megaplan`).
- `X-Astoria-Client: <name>` → an unauthenticated *hint*, trusted as-is on the LAN (≤ 64 chars).
- Neither → `anonymous` (MCP calls without HTTP headers → `mcp`).

The client name is **provenance, not authorization**: it becomes `fact.source`, the audit `actor`, the
snapshot `client`, and sets the trust cap used *for ranking only* (`cli`/`human` 1.0 · `input`/`claude-code`
.85 · `api`/`mcp` .7 · `megaplan` .6 · `anonymous` .5 · unknown .6). No action is refused for lack of a
token. Assertion order (newest statement wins), tombstones and the staging gate — not trust scores —
decide conflicts, so a spoofed `X-Astoria-Client` can at most make a fact rank a little higher.

Tokens are opaque random strings you generate; rotate by editing `ASTORIA_CLIENT_TOKENS` on the NAS,
`./deploy/nas/deploy.sh --no-build`, then updating the clients (`~/.config/astoria/env`,
`NOVA_MEMORY_TOKEN` for the hooks, `MEGAPLAN_MEMORY_TOKEN` for MegaPlan, the MCP `headers` block if used).

Every mutation is audited (`audit` table: actor, op, target, detail, time) — `GET /audit`, `astoria audit`.

## 3. Secrets — where they live

| secret | location | mode / owner | notes |
|---|---|---|---|
| Postgres password, per-client tokens, `ANTHROPIC_API_KEY` | NAS `/volume1/docker/astoria/.env` | `600 root` | read by compose (`env_file`); **never in git** (`deploy.sh` excludes it; `.env.example` is the template) |
| `ASTORIA_URL`, `ASTORIA_TOKEN`, `ASTORIA_TOKEN_INPUT`, `ASTORIA_TOKEN_CLAUDE_CODE`, `ASTORIA_TOKEN_MEGAPLAN` | workstation `~/.config/astoria/env` | `600 rick` | `set -a; . ~/.config/astoria/env; set +a` before using the CLI with a token |
| MegaPlan's token (if set) | NAS `/volume1/docker/megaplan/.env` (`MEGAPLAN_MEMORY_TOKEN`) | — | optional |
| backups | NAS `/volume1/docker/astoria/backups/*.dump` | `700`, root | **plaintext memory** (pg_dump custom format is compressed, not encrypted) — treat the directory as sensitive |
| `pgdata/` | NAS `/volume1/docker/astoria/pgdata` | postgres uid, `700` | the live database |

The infra repo (`~/projects/infrastructure`) is private and references these files **by path only**.
The container runs as non-root uid 1000 (`astoria`); the only writable mounts are `/data` and the DB.

## 4. Data handling

- **What is stored**: facts (triples with provenance), episodes (raw captured text — turns, summaries,
  notes), the template profile narrative, recall snapshots (ids only), the audit log. Claude Code's hook
  captures **summaries only**, never raw transcripts; `input` captures turns.
- **Secrets in memory content**: the extraction prompt forbids storing "secrets/API keys/passwords/tokens
  (never, even if asked)"; the `astoria` skill tells agents the same. There is no automatic scrubber on
  `capture`, so a user pasting a key into a captured turn will have it in the `episode.body`. Remedy:
  `DELETE /episodes/{id}` (`astoria episode delete`) and `forget --hard` any fact derived from it.
- **Logs**: the JSON request log records method/path/status/latency/client — no bodies. uvicorn's
  access log includes **query strings** (`/facts?…&q=…`); docker log rotation keeps ≤ 30 MB per container.
- **Deletion semantics**: `forget --hard` / `DELETE /facts/{id}?mode=hard` removes the row; `DELETE
  /users/{id}` removes everything for a user (facts, episodes, queue, snapshots, tombstones, profile,
  audit). Soft forgets and retracts keep rows for history/audit. Backups keep deleted data until they
  rotate out (14 × 6 h ≈ 3.5 days).
- Snapshots record which fact/episode ids a session was shown, not the text; pruned after 90 days.

## 5. What leaves the box (no telemetry)

Astoria makes exactly three kinds of outbound calls:
1. `http://192.168.1.134:8931` — TEI embeddings (NAS-local).
2. `http://192.168.1.221:4000/v1` — SAINT chat completions for cognify (LAN; SAINT itself may route to
   cloud seats per its own policy).
3. `https://api.anthropic.com` — **only** the cognify fallback when SAINT is unreachable and
   `ANTHROPIC_API_KEY` is set; the payload is the extraction prompt + the coalesced episode text
   (≤ 6 000 chars per call) + up to 30 candidate facts. No reads ever leave the LAN; `recall`, `briefing`
   and all CRUD are local SQL.

No usage metrics, crash reporting, update checks or third-party SDK telemetry. Dependencies are pinned
by `pyproject.toml` ranges and installed at image build; the TEI image is digest-pinned.

## 6. Upstream / supply chain

- Postgres via `pgvector/pgvector:pg18` (tag, not digest — pin a digest if reproducibility matters);
  Python deps: FastAPI, uvicorn, FastMCP (`>=2.3,<3`), psycopg 3, pgvector, httpx, pydantic, typer,
  rich, python-dateutil, anthropic SDK.
- The extraction prompt's *shape* is adapted from Graphiti (Apache-2.0, attributed in the prompt file);
  no Graphiti code runs at runtime.
- Astoria itself is MIT (`LICENSE`).
