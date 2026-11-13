-- Run using a NEW psql process after restore_age_catalog.sql commits.
-- Reads every node and edge through Cypher and compares IDs/properties/endpoints
-- with the restored label tables. Does not establish archive/source equality.
\set ON_ERROR_STOP on
LOAD 'age';
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL search_path = ag_catalog, pg_catalog;
SET LOCAL statement_timeout = '5min';
DO $verify$
DECLARE
  g record;
  differs boolean;
BEGIN
  IF NOT EXISTS (SELECT FROM ag_catalog.ag_graph WHERE name='memory_graph') THEN
    RAISE EXCEPTION 'Missing Sill memory_graph; restore verification failed';
  END IF;
  FOR g IN SELECT name, namespace FROM ag_catalog.ag_graph ORDER BY name LOOP
    EXECUTE format($query$
      WITH physical AS (
        SELECT id::text, properties::text FROM %I._ag_label_vertex
      ), traversed AS (
        SELECT id::text, properties::text FROM ag_catalog.cypher(%L,
          $cypher$ MATCH (n) RETURN id(n), properties(n) $cypher$)
          AS (id ag_catalog.agtype, properties ag_catalog.agtype)
      ) SELECT EXISTS (
        (TABLE physical EXCEPT ALL TABLE traversed)
        UNION ALL (TABLE traversed EXCEPT ALL TABLE physical))
    $query$, g.name, g.name) INTO differs;
    IF differs THEN
      RAISE EXCEPTION 'Graph % vertex IDs/properties differ between tables and Cypher', g.name;
    END IF;
    EXECUTE format($query$
      WITH physical AS (
        SELECT id::text, start_id::text, end_id::text, properties::text FROM %I._ag_label_edge
      ), traversed AS (
        SELECT id::text, start_id::text, end_id::text, properties::text
        FROM ag_catalog.cypher(%L,
          $cypher$ MATCH (a)-[e]->(b) RETURN id(e), id(a), id(b), properties(e) $cypher$)
          AS (id ag_catalog.agtype, start_id ag_catalog.agtype,
              end_id ag_catalog.agtype, properties ag_catalog.agtype)
      ) SELECT EXISTS (
        (TABLE physical EXCEPT ALL TABLE traversed)
        UNION ALL (TABLE traversed EXCEPT ALL TABLE physical))
    $query$, g.name, g.name) INTO differs;
    IF differs THEN
      RAISE EXCEPTION 'Graph % edge IDs/endpoints/properties differ between tables and Cypher', g.name;
    END IF;
    RAISE NOTICE 'Verified graph %: all vertex and edge rows match Cypher', g.name;
  END LOOP;
END
$verify$;
COMMIT;
\echo 'Graph rows verified. Complete the archive/source data, schema and ledger comparisons in docs/RESTORE.md.'
