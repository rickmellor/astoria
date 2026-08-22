-- Astoria schema v1 — bitemporal, supersedable, trusted facts + typed layers.
-- Applied idempotently at service start (see astoria/store/migrate.py). Design: DESIGN.md §6-§9.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

-- ---------------------------------------------------------------------------
-- Predicate registry: functional (one current value: favorite_beer) vs set (many: likes_beer).
-- Unknown predicates default to 'set' (never destroy data by guessing functional).
CREATE TABLE IF NOT EXISTS predicate (
  name        text PRIMARY KEY,
  cardinality text NOT NULL DEFAULT 'set' CHECK (cardinality IN ('functional','set')),
  description text,
  created_at  timestamptz NOT NULL DEFAULT now());

-- ---------------------------------------------------------------------------
-- Episodes: non-lossy raw captures (turns, session summaries, hook captures). Always
-- written FIRST and durably — this is what survives the nightly LLM outage.
CREATE TABLE IF NOT EXISTS episode (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        text NOT NULL,
  kind           text NOT NULL DEFAULT 'turn',        -- turn | session_summary | note | import
  layer          text NOT NULL DEFAULT 'episodic',    -- working | episodic
  hook           text NOT NULL,                       -- short indexable summary (metadata/body firewall)
  body           text NOT NULL,
  body_embedding vector(768),
  body_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', left(hook,2000)||' '||left(body,8000))) STORED,
  occurred_at    timestamptz NOT NULL DEFAULT now(),
  ingested_at    timestamptz NOT NULL DEFAULT now(),
  source         text NOT NULL DEFAULT 'api',         -- input | claude-code | megaplan | import | api
  session_id     text,
  importance     real NOT NULL DEFAULT 0.5,
  access_count   int  NOT NULL DEFAULT 0,
  last_seen      timestamptz,
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived','deleted')),
  cognify_state  text NOT NULL DEFAULT 'pending' CHECK (cognify_state IN ('pending','done','skipped','failed')),
  meta           jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE INDEX IF NOT EXISTS episode_user_time ON episode(user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS episode_session   ON episode(user_id, session_id);
CREATE INDEX IF NOT EXISTS episode_tsv       ON episode USING gin(body_tsv);
CREATE INDEX IF NOT EXISTS episode_vec       ON episode USING hnsw(body_embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Facts: the heart. (subject,predicate,value) with two time axes + trust + lineage.
CREATE TABLE IF NOT EXISTS fact (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         text NOT NULL,
  subject         text NOT NULL,                      -- canonical: 'user' for the person, else entity name
  predicate       text NOT NULL REFERENCES predicate(name),
  value           text NOT NULL,
  value_norm      text GENERATED ALWAYS AS (lower(btrim(value))) STORED,
  hook            text,                               -- "subject predicate value" rendered for search
  value_embedding vector(768),
  hook_tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(hook, subject||' '||predicate||' '||value))) STORED,
  layer           text NOT NULL DEFAULT 'semantic' CHECK (layer IN ('semantic','profile','procedural')),
  -- valid time (real world)
  valid_from      timestamptz NOT NULL DEFAULT now(),
  valid_to        timestamptz,
  -- transaction/belief time (system)
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  expired_at      timestamptz,
  status          text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','superseded','retracted','archived','staging','deleted')),
  supersedes      uuid REFERENCES fact(id),
  superseded_by   uuid REFERENCES fact(id),
  -- trust
  confidence      real NOT NULL DEFAULT 0.6 CHECK (confidence >= 0 AND confidence <= 1),
  source          text NOT NULL DEFAULT 'api',        -- client name (input | claude-code | megaplan | import | cli | api)
  source_trust    real NOT NULL DEFAULT 0.6,          -- prior by provenance class (human 1.0 > doc .85 > tool .7 > inferred .5)
  is_belief       boolean NOT NULL DEFAULT false,     -- inference (belief) vs evidence (fact)
  importance      real NOT NULL DEFAULT 0.5,
  last_seen       timestamptz NOT NULL DEFAULT now(),
  access_count    int NOT NULL DEFAULT 0,
  corroborations  int NOT NULL DEFAULT 0,
  tags            text[] NOT NULL DEFAULT '{}',
  origin_episode  uuid REFERENCES episode(id),        -- lineage (idempotency + independence)
  ref             jsonb,                               -- procedural link: {kind: skill|megaplan|infra-doc|url, ref: ...}
  meta            jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE INDEX IF NOT EXISTS fact_key      ON fact(user_id, subject, predicate, status);
CREATE INDEX IF NOT EXISTS fact_user_status ON fact(user_id, status);
CREATE INDEX IF NOT EXISTS fact_valid    ON fact(user_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS fact_origin   ON fact(origin_episode);
CREATE INDEX IF NOT EXISTS fact_tsv      ON fact USING gin(hook_tsv);
CREATE INDEX IF NOT EXISTS fact_vec      ON fact USING hnsw(value_embedding vector_cosine_ops);
-- Functional predicates: at most ONE active row per key. Enforced by the supersede txn
-- (advisory lock + this index as the belt-and-suspenders). Set predicates: one active per value.
CREATE UNIQUE INDEX IF NOT EXISTS fact_one_active_value
  ON fact(user_id, subject, predicate, value_norm) WHERE status = 'active';

-- ---------------------------------------------------------------------------
-- Edges between facts/entities (relationships; walked by retrieval, bounded depth).
CREATE TABLE IF NOT EXISTS edge (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       text NOT NULL,
  src_fact      uuid NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
  dst_fact      uuid NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
  relation      text NOT NULL,
  weight        real NOT NULL DEFAULT 1.0,
  valid_from    timestamptz NOT NULL DEFAULT now(),
  valid_to      timestamptz,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','retracted')),
  superseded_by uuid,
  created_at    timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS edge_src ON edge(src_fact) WHERE status='active';
CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst_fact) WHERE status='active';

-- ---------------------------------------------------------------------------
-- Profile narrative — DISPLAY-ONLY dual of the profile-layer facts. Never embedded/searched.
CREATE TABLE IF NOT EXISTS profile (
  user_id      text PRIMARY KEY,
  narrative    text NOT NULL DEFAULT '',
  version      int  NOT NULL DEFAULT 0,
  rederived_at timestamptz,
  source       text NOT NULL DEFAULT 'template');     -- template | llm | import
CREATE TABLE IF NOT EXISTS profile_history (
  user_id text NOT NULL, version int NOT NULL, narrative text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(user_id, version));

-- ---------------------------------------------------------------------------
-- Immutable recall snapshots: what a session was shown (budget-trimmed ids only).
CREATE TABLE IF NOT EXISTS snapshot (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     text NOT NULL,
  session_id  text,
  actor       text,
  query       text,
  fact_ids    uuid[] NOT NULL DEFAULT '{}',
  episode_ids uuid[] NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS snapshot_user_time ON snapshot(user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Cognify queue: LLM-at-write jobs. Ordered by occurred_at; priority: corrections first.
CREATE TABLE IF NOT EXISTS cognify_queue (
  id              bigserial PRIMARY KEY,
  user_id         text NOT NULL,
  episode_id      uuid NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
  kind            text NOT NULL DEFAULT 'extract',     -- extract | reflect | rederive_profile | embed_backfill
  priority        int  NOT NULL DEFAULT 5,             -- lower = sooner (1 = correction)
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','failed','dead')),
  attempts        int NOT NULL DEFAULT 0,
  max_attempts    int NOT NULL DEFAULT 8,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz);
CREATE INDEX IF NOT EXISTS cognify_ready ON cognify_queue(state, next_attempt_at, priority, occurred_at)
  WHERE state IN ('pending','failed');

-- ---------------------------------------------------------------------------
-- Audit log of every control-plane mutation (who/what/when) — cheap, append-only.
CREATE TABLE IF NOT EXISTS audit (
  id         bigserial PRIMARY KEY,
  user_id    text NOT NULL,
  actor      text,                                     -- client name from token (or 'anonymous')
  op         text NOT NULL,
  target     uuid,
  detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS audit_user_time ON audit(user_id, created_at DESC);

-- Seed the core predicate vocabulary (functional ones matter most — they supersede).
INSERT INTO predicate(name, cardinality, description) VALUES
  ('name','functional','the person''s name'),
  ('location','functional','where the user lives/is based'),
  ('timezone','functional','user timezone'),
  ('favorite_beer','functional','favorite beer'),
  ('favorite_editor','functional','preferred editor'),
  ('favorite_language','functional','preferred programming language'),
  ('communication_preference','functional','how the user likes answers'),
  ('default_model','functional','default LLM/profile in use'),
  ('default_johnny_profile','functional','johnny boot profile'),
  ('role','functional','job/role'),
  ('employer','functional','employer'),
  ('likes','set','things the user likes'),
  ('dislikes','set','things the user dislikes'),
  ('owns','set','hardware/assets owned'),
  ('uses','set','tools/tech the user uses'),
  ('project','set','projects the user is working on'),
  ('goal','set','goals'),
  ('decision','set','decisions made (subject = topic)'),
  ('fact','set','generic factual statement about the subject'),
  ('howto','set','procedural how-to (procedural layer)'),
  ('related_to','set','generic relationship')
ON CONFLICT (name) DO NOTHING;

INSERT INTO schema_migrations(version) VALUES ('001_schema') ON CONFLICT DO NOTHING;
