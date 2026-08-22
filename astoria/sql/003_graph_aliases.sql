-- 003: graph layer (edges between fact/entity nodes) + entity registry + subject aliases.
-- Nodes are addressed by (kind, id): kind='fact' → id = fact uuid as text; kind='entity' → id = the
-- canonical entity name (lower-case, what fact.subject holds). No FK to fact on purpose: hard-deleted
-- facts leave dangling edges that readers filter (store/graph.py joins fact WHERE status='active').

CREATE TABLE IF NOT EXISTS entity (
  user_id    text NOT NULL,
  name       text NOT NULL,                            -- canonical, lower-case (== fact.subject spelling)
  kind       text,                                     -- person | project | system | place | org | tool | ... (free)
  summary    text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, name));

-- alias → canonical subject (case-insensitive; both stored lower). facts.canon_subject consults
-- graph.resolve_alias so every write/read on `alias` lands on `canonical`. Kept flat (no chains).
CREATE TABLE IF NOT EXISTS alias (
  user_id     text NOT NULL,
  alias       text NOT NULL,
  canonical   text NOT NULL,
  source      text NOT NULL DEFAULT 'api',             -- client that created it
  source_kind text NOT NULL DEFAULT 'explicit' CHECK (source_kind IN ('explicit','detector','extracted','imported','curator')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, alias));
CREATE INDEX IF NOT EXISTS alias_canonical ON alias(user_id, canonical);

CREATE TABLE IF NOT EXISTS edge (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        text NOT NULL,
  src_kind       text NOT NULL CHECK (src_kind IN ('fact','entity')),
  src_id         text NOT NULL,
  dst_kind       text NOT NULL CHECK (dst_kind IN ('fact','entity')),
  dst_id         text NOT NULL,
  relation       text NOT NULL,                        -- snake_case: part_of | located_in | works_at | owns | related_to | ...
  weight         real NOT NULL DEFAULT 1,
  valid_from     timestamptz NOT NULL DEFAULT now(),
  valid_to       timestamptz,
  asserted_at    timestamptz NOT NULL DEFAULT now(),
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','retracted','archived')),
  superseded_by  uuid REFERENCES edge(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED,
  source         text NOT NULL DEFAULT 'api',
  source_kind    text NOT NULL DEFAULT 'explicit' CHECK (source_kind IN ('explicit','detector','extracted','imported','curator')),
  confidence     real NOT NULL DEFAULT 0.6 CHECK (confidence >= 0 AND confidence <= 1),
  origin_episode uuid REFERENCES episode(id) ON DELETE SET NULL,
  evidence       text,
  meta           jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE INDEX IF NOT EXISTS edge_src      ON edge(user_id, src_id);
CREATE INDEX IF NOT EXISTS edge_dst      ON edge(user_id, dst_id);
CREATE INDEX IF NOT EXISTS edge_relation ON edge(user_id, relation);
CREATE INDEX IF NOT EXISTS edge_origin   ON edge(origin_episode) WHERE origin_episode IS NOT NULL;
-- one ACTIVE edge per (src, dst, relation) — add_edge is idempotent (noop/bump) on this key
CREATE UNIQUE INDEX IF NOT EXISTS edge_one_active
  ON edge(user_id, src_kind, src_id, dst_kind, dst_id, relation) WHERE status='active';

INSERT INTO schema_migrations(version) VALUES ('003_graph_aliases') ON CONFLICT DO NOTHING;
