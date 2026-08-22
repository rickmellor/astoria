-- 004: storage tuning from the scale validation (docs/PERFORMANCE.md §5).
--  * autovacuum earlier on the hot tables (default 20% of rows is 200k dead tuples at 1M rows);
--  * fillfactor 90 keeps recall's touch-updates (last_seen/access_count) HOT → no HNSW/GIN index bloat.
ALTER TABLE fact    SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02, fillfactor = 90);
ALTER TABLE episode SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.02, fillfactor = 90);
ALTER TABLE cognify_queue SET (autovacuum_vacuum_scale_factor = 0.05);
ALTER TABLE snapshot      SET (autovacuum_vacuum_scale_factor = 0.05);
INSERT INTO schema_migrations(version) VALUES ('004_pg_tuning') ON CONFLICT DO NOTHING;
