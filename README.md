# Astoria

**Astoria** is the memory layer of the NOVA stack — *layered, trusted, scalable, deep* memory as a
network service on the always-on NAS, shared by every Nova client (`input`, Claude Code, MegaPlan,
Nova Core / web / mobile). Named for *Short Circuit*'s Astoria, Oregon — it joins Nova, SAINT and Johnny 5.

It replaces MemoryOS, whose failure modes became Astoria's requirements: **facts you can correct and
delete**, corrections that **propagate** (and never resurrect), **provenance + confidence** on every
fact, **"what was true when"**, and a **deterministic, LLM-free control plane**.

```
clients ──REST :8933 / MCP /mcp/──►  astoria (FastAPI + FastMCP, in-process worker)
                                        │ control plane (no LLM): recall · capture · remember · correct · retract · forget · as_of
                                        │ cognify (LLM at WRITE only, via SAINT → Anthropic fallback): extract → resolve → supersede
                                        ▼
                                   Postgres 18 + pgvector (facts · episodes · tombstones · snapshots · queue · audit)
                                   embeddings: NAS TEI nomic-embed-text-v1.5 (768-d)
```

## Model in one minute

| layer | what | where |
|---|---|---|
| working | the live session's last turns | `episode.kind='turn'` |
| episodic | session summaries / notes | `episode.kind in (summary, note, import)` |
| semantic · profile · procedural | `(subject, predicate, value)` facts | `fact.layer` |

- **Facts are bitemporal + assertion-ordered.** `valid_from/valid_to` say when it was true in the world;
  `asserted_at` orders statements (newer statement wins, even if it back-dates its validity);
  `ingested_at/expired_at` say when *we* believed it. A correction **supersedes** (audit chain kept) —
  nothing is overwritten. `retract` closes belief, `forget` archives/deletes, `as_of` time-travels.
- **Predicates have cardinality.** `favorite_beer` is *functional* (one current value → supersede);
  `likes` is a *set* (many values → add/retract). Unknown predicates auto-register safely as sets.
- **Trust is explicit and bounded.** `confidence` (by how it was stated/extracted, saturating with
  independent corroboration) × `source_trust` (capped by client and by kind: explicit > detector >
  extracted > curator > import). Used for ranking — never as the conflict resolver.
- **Tombstones** stop a retracted fact from being re-extracted out of old conversations.
- **LLM only at write.** `capture` stores the raw episode immediately (survives the nightly fleet
  power-off), then a queued *cognify* job extracts facts (SAINT when up, direct Anthropic otherwise).
  `recall` is pure hybrid search: pgvector cosine ⊕ BM25 → RRF → recency × importance × trust → budget →
  a pre-rendered `context` block every client injects verbatim.

## Quick start

```bash
# service (NAS): see deploy/nas/ — docker compose up -d --build ; health: curl :8933/health
# CLI (anywhere on the LAN):
pip install -e .            # or pipx install .
export ASTORIA_URL=http://192.168.1.134:8933   # + ASTORIA_TOKEN for trusted writes
astoria --help
astoria remember rick favorite_beer IPA
astoria correct  rick favorite_beer Pilsner       # supersedes, keeps history
astoria history  rick favorite_beer
astoria as-of 2026-07-01 --predicate favorite_beer
astoria recall "what beer do I like"
```

## Layout

```
astoria/
  config.py            settings (ASTORIA_* env)
  sql/001_schema.sql   the schema (applied idempotently at boot)
  store/   db.py · facts.py (the supersede txn) · episodes.py
  core/    embed.py (TEI) · llm.py (SAINT→Anthropic) · capture.py (gate + detector + enqueue)
  retrieval/recall.py  hybrid recall, briefing, context rendering
  cognify/ prompts/extract.md · resolver.py · worker.py (in-process queue drain)
  curator/maintenance.py  embed backfill · profile re-derive · prune/archive
  api/     app.py · rest.py · mcp_tools.py · service.py (one dispatcher) · auth.py
  cli/     main.py (typer; `astoria --help`)
deploy/nas/  docker-compose.yml · deploy.sh · .env.example
docs/        CONTRACT.md (the API/MCP contract) · more in docs/
tests/       unit · concurrency · acceptance (T1–T12) · scripts/smoke.sh
```

## Docs

| doc | what |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | layers, data model (bitemporal + `asserted_at`, cardinality, tombstones, trust numbers), write path (capture → detector → cognify), read path, worker/curator schedule, degrade behaviour, compat layer, limitations/roadmap |
| [docs/API.md](docs/API.md) | every REST route with request/response + curl examples, MCP tools and arguments, auth/tokens, error shapes, MemoryOS-compat routes, client wiring |
| [docs/CLI.md](docs/CLI.md) | the `astoria` CLI: setup, workflows, a test-drive script, full `--help` for every subcommand |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | deploy/redeploy, `.env` keys, logs, health, queue/dead-letter handling, backups + step-by-step restore, schema upgrades, troubleshooting, capacity knobs, tests |
| [docs/SECURITY.md](docs/SECURITY.md) | LAN-only posture, tokens and trust model, secrets locations, data handling, no telemetry |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | measured latency/throughput, the 10k-fact benchmark (`scripts/bench/`) |
| [docs/CONTRACT.md](docs/CONTRACT.md) | the fixed build contract (interfaces, numbers) |

Status: **live on the NAS since 2026-08-22** (`http://192.168.1.134:8933`), replacing MemoryOS;
homelab-level notes (ports, client wiring, decommission record) in `~/projects/infrastructure/astoria.md`.

Design & rationale: `~/projects/infrastructure/astoria/{research,requirements,dossiers,DESIGN}.md`.
License: MIT.
