-- 002: index the self-referential supersede chain. Without these, ON DELETE SET NULL on
-- fact.supersedes / fact.superseded_by forces a table scan per deleted row (a 100k-row wipe ran
-- for 17+ min in the scale test). Also speeds `history` chain walks.
CREATE INDEX IF NOT EXISTS fact_supersedes    ON fact(supersedes)    WHERE supersedes IS NOT NULL;
CREATE INDEX IF NOT EXISTS fact_superseded_by ON fact(superseded_by) WHERE superseded_by IS NOT NULL;
-- cognify_queue → episode and snapshot lookups by user were fine (indexed); edge FKs already indexed.
INSERT INTO schema_migrations(version) VALUES ('002_chain_indexes') ON CONFLICT DO NOTHING;
