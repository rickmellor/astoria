-- Astoria schema v1 — bitemporal, supersedable, trusted facts + typed layers.
-- Applied idempotently at service start (astoria/store/db.py). Design: DESIGN.md §6-§9 + round-2 review.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

-- ---------------------------------------------------------------------------
-- Predicate registry: functional (ONE current value: favorite_beer) vs set (many: likes).
-- Unknown predicates auto-register; heuristic picks functional only for favorite_/default_/
-- primary_/preferred_/current_ prefixes, else 'set' (safe: never clobbers by guessing).
CREATE TABLE IF NOT EXISTS predicate (
  name        text PRIMARY KEY,
  cardinality text NOT NULL DEFAULT 'set' CHECK (cardinality IN ('functional','set')),
  layer_hint  text NOT NULL DEFAULT 'semantic' CHECK (layer_hint IN ('semantic','profile','procedural')),
  auto        boolean NOT NULL DEFAULT false,   -- auto-registered by the extractor (review me)
  description text,
  created_at  timestamptz NOT NULL DEFAULT now());

-- ---------------------------------------------------------------------------
-- Episodes: non-lossy raw captures. Always written FIRST and durably (survives LLM outage).
-- kind: turn (working memory, per session) | summary | note | import
CREATE TABLE IF NOT EXISTS episode (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        text NOT NULL,
  kind           text NOT NULL DEFAULT 'turn' CHECK (kind IN ('turn','summary','note','import')),
  hook           text NOT NULL,                       -- short indexable summary (≤400 chars) — what gets embedded
  body           text NOT NULL,
  embedding      vector(768),
  tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', left(hook,2000)||' '||left(body,8000))) STORED,
  occurred_at    timestamptz NOT NULL DEFAULT now(),
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  source         text NOT NULL DEFAULT 'api',         -- client: input | claude-code | megaplan | cli | import | api
  session_id     text,
  importance     real NOT NULL DEFAULT 0.5,
  access_count   int  NOT NULL DEFAULT 0,
  last_seen      timestamptz,
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived','deleted')),
  processed_at   timestamptz,                         -- cognify done
  idem_key       text UNIQUE,                         -- sha256(user_id|session_id|kind|text) — replay-safe
  tags           text[] NOT NULL DEFAULT '{}',
  meta           jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE INDEX IF NOT EXISTS episode_user_time ON episode(user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS episode_session   ON episode(user_id, session_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS episode_tsv       ON episode USING gin(tsv);
CREATE INDEX IF NOT EXISTS episode_vec       ON episode USING hnsw(embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Facts: the heart. (subject,predicate,value) + two time axes + assertion order + trust + lineage.
CREATE TABLE IF NOT EXISTS fact (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         text NOT NULL,
  subject         text NOT NULL,                      -- canonical; the owner is literally the user_id
  predicate       text NOT NULL REFERENCES predicate(name),
  cardinality     text NOT NULL DEFAULT 'set' CHECK (cardinality IN ('functional','set')),  -- denormalized at write
  value           text NOT NULL,
  value_norm      text GENERATED ALWAYS AS (lower(btrim(regexp_replace(value, '\s+', ' ', 'g')))) STORED,
  hook            text NOT NULL,                      -- "subject predicate value" — what gets embedded/searched
  detail          text,
  embedding       vector(768),
  tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', subject||' '||replace(predicate,'_',' ')||' '||value)) STORED,
  layer           text NOT NULL DEFAULT 'semantic' CHECK (layer IN ('semantic','profile','procedural')),
  -- valid time (real world) — shapes the validity window only
  valid_from      timestamptz NOT NULL DEFAULT now(),
  valid_to        timestamptz,
  -- assertion time — ORDERING axis: newer statement wins (review W2)
  asserted_at     timestamptz NOT NULL DEFAULT now(),
  -- transaction/belief time (system)
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  expired_at      timestamptz,
  status          text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','superseded','retracted','archived','staging','deleted')),
  supersedes      uuid REFERENCES fact(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED,
  superseded_by   uuid REFERENCES fact(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED,
  -- trust (bounded heuristic score, NOT calibration)
  confidence      real NOT NULL DEFAULT 0.6 CHECK (confidence >= 0 AND confidence <= 1),
  source          text NOT NULL DEFAULT 'api',        -- client name (authenticated when token given)
  source_kind     text NOT NULL DEFAULT 'explicit'    -- explicit | detector | extracted | imported | curator
                  CHECK (source_kind IN ('explicit','detector','extracted','imported','curator')),
  source_trust    real NOT NULL DEFAULT 0.8,          -- prior by client/source_kind (advisory; ranking only)
  is_belief       boolean NOT NULL DEFAULT false,     -- inference (belief) vs evidence (fact)
  importance      real NOT NULL DEFAULT 0.5,
  last_seen       timestamptz NOT NULL DEFAULT now(),
  access_count    int NOT NULL DEFAULT 0,
  corroborations  int NOT NULL DEFAULT 0,
  tags            text[] NOT NULL DEFAULT '{}',
  origin_episode  uuid REFERENCES episode(id) ON DELETE SET NULL,  -- lineage (idempotency + independence)
  evidence        text,                               -- verbatim snippet that produced it
  ref             jsonb,                               -- procedural link: {kind: skill|megaplan|infra-doc|url, ref}
  meta            jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE INDEX IF NOT EXISTS fact_key         ON fact(user_id, subject, predicate, status);
CREATE INDEX IF NOT EXISTS fact_user_status ON fact(user_id, status, layer);
CREATE INDEX IF NOT EXISTS fact_valid       ON fact(user_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS fact_origin      ON fact(origin_episode);
CREATE INDEX IF NOT EXISTS fact_tsv         ON fact USING gin(tsv);
CREATE INDEX IF NOT EXISTS fact_vec         ON fact USING hnsw(embedding vector_cosine_ops);
-- Exactly one active value per FUNCTIONAL key; one active row per (key,value) for SET keys.
CREATE UNIQUE INDEX IF NOT EXISTS fact_one_active_functional
  ON fact(user_id, subject, predicate) WHERE status='active' AND cardinality='functional';
CREATE UNIQUE INDEX IF NOT EXISTS fact_one_active_set_value
  ON fact(user_id, subject, predicate, value_norm) WHERE status='active' AND cardinality='set';

-- ---------------------------------------------------------------------------
-- Tombstones: a human retract/forget/delete of a triple blocks re-activation by extraction/
-- curation from old episodes (cross-tier resurrection guard, review W3). An EXPLICIT human
-- re-assert lifts it.
CREATE TABLE IF NOT EXISTS tombstone (
  user_id    text NOT NULL,
  subject    text NOT NULL,
  predicate  text NOT NULL,
  value_norm text NOT NULL,
  reason     text NOT NULL,                           -- retract | forget | delete | extracted-retract
  by_source  text,
  blocks     text NOT NULL DEFAULT 'non-explicit' CHECK (blocks IN ('non-explicit','none')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, subject, predicate, value_norm));

-- ---------------------------------------------------------------------------
-- Profile narrative — DISPLAY-ONLY dual of the profile-layer facts. Never embedded/searched.
CREATE TABLE IF NOT EXISTS profile (
  user_id      text PRIMARY KEY,
  narrative    text NOT NULL DEFAULT '',
  version      int  NOT NULL DEFAULT 0,
  rederived_at timestamptz,
  source       text NOT NULL DEFAULT 'template');     -- template | llm
CREATE TABLE IF NOT EXISTS profile_history (
  user_id text NOT NULL, version int NOT NULL, narrative text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(user_id, version));

-- ---------------------------------------------------------------------------
-- Recall snapshots: what a session was shown (budget-trimmed ids only; pruned > 90 d).
CREATE TABLE IF NOT EXISTS snapshot (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     text NOT NULL,
  session_id  text,
  client      text,
  query       text,
  fact_ids    uuid[] NOT NULL DEFAULT '{}',
  episode_ids uuid[] NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS snapshot_user_time ON snapshot(user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Cognify queue: LLM-at-write jobs. Drained in-process; priority 1 = corrections first.
CREATE TABLE IF NOT EXISTS cognify_queue (
  id              bigserial PRIMARY KEY,
  user_id         text NOT NULL,
  episode_id      uuid NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
  session_id      text,
  kind            text NOT NULL DEFAULT 'extract',     -- extract | rederive_profile | embed_backfill
  priority        int  NOT NULL DEFAULT 5,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','failed','dead','skipped')),
  attempts        int NOT NULL DEFAULT 0,
  max_attempts    int NOT NULL DEFAULT 5,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz);
CREATE INDEX IF NOT EXISTS cognify_ready ON cognify_queue(state, next_attempt_at, priority, occurred_at)
  WHERE state IN ('pending','failed');
CREATE INDEX IF NOT EXISTS cognify_episode ON cognify_queue(episode_id);

-- ---------------------------------------------------------------------------
-- Audit log of every control-plane mutation — append-only.
CREATE TABLE IF NOT EXISTS audit (
  id         bigserial PRIMARY KEY,
  user_id    text NOT NULL,
  actor      text,
  op         text NOT NULL,
  target     uuid,
  detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS audit_user_time ON audit(user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Seed predicate vocabulary (functional ones matter most — they supersede).
INSERT INTO predicate(name, cardinality, layer_hint, description) VALUES
  ('name','functional','profile','the person''s name'),
  ('location','functional','profile','where the user lives / is based'),
  ('timezone','functional','profile','user timezone'),
  ('employer','functional','profile','employer'),
  ('role','functional','profile','job / role'),
  ('favorite_beer','functional','profile','favorite beer'),
  ('favorite_editor','functional','profile','preferred editor'),
  ('favorite_language','functional','profile','preferred programming language'),
  ('preferred_shell','functional','profile','preferred shell'),
  ('preferred_response_style','functional','profile','how the user likes answers'),
  ('communication_preference','functional','profile','communication preference'),
  ('primary_workstation','functional','profile','main workstation'),
  ('primary_nas','functional','profile','main NAS'),
  ('default_model','functional','semantic','default LLM in use'),
  ('default_johnny_profile','functional','semantic','johnny boot profile'),
  ('current_focus','functional','semantic','what the user is currently focused on'),
  ('likes','set','profile','things the user likes'),
  ('dislikes','set','profile','things the user dislikes'),
  ('interested_in','set','profile','interests'),
  ('has_skill','set','profile','skills'),
  ('knows_person','set','profile','people the user knows'),
  ('uses_tool','set','semantic','tools / tech the user uses'),
  ('owns_hardware','set','semantic','hardware / assets owned'),
  ('runs_service','set','semantic','services the user runs'),
  ('works_on_project','set','semantic','projects'),
  ('goal','set','semantic','goals'),
  ('decided','set','semantic','decisions made (subject = topic)'),
  ('fact','set','semantic','generic factual statement about the subject'),
  ('learned_howto','set','procedural','procedural how-to'),
  ('related_to','set','semantic','generic relationship')
ON CONFLICT (name) DO NOTHING;

INSERT INTO schema_migrations(version) VALUES ('001_schema') ON CONFLICT DO NOTHING;
