-- zzz_initdb_stamp.sql  (sill original)
-- Runs LAST during first-boot initdb (initdb executes *.sql in sorted order,
-- after 000_schema.sql and every numbered migration). Records that a fresh
-- install is already at the current schema level, so upgrade.sh has nothing
-- to apply. upgrade.sh never runs this file — its glob takes only
-- [0-9][0-9][0-9]_*.sql — and re-creates the table itself when upgrading a
-- pre-migration-lane install.

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id text PRIMARY KEY,                       -- e.g. '001'
    applied_at   timestamptz NOT NULL DEFAULT now(),
    source       text NOT NULL DEFAULT 'upgrade.sh'      -- 'initdb' | 'upgrade.sh'
);

COMMENT ON TABLE schema_migrations IS
    'Applied schema migrations. Fresh installs are stamped by initdb; upgrades are stamped by upgrade.sh. verify.sh check 5 compares this against backend/migrations/.';

INSERT INTO schema_migrations (migration_id, source) VALUES
    ('001', 'initdb'),
    ('002', 'initdb'),
    ('003', 'initdb'),
    ('004', 'initdb'),
    ('005', 'initdb'),
    ('006', 'initdb'),
    ('007', 'initdb'),
    ('008', 'initdb'),
    ('009', 'initdb'),
    ('010', 'initdb'),
    ('011', 'initdb'),
    ('012', 'initdb'),
    ('013', 'initdb'),
    ('014', 'initdb'),
    ('015', 'initdb')
ON CONFLICT (migration_id) DO NOTHING;
