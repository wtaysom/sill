-- Explicit post-restore operation only; read docs/RESTORE.md first.
-- Supported boundary: PostgreSQL 16, AGE 1.6.0 with the catalog below.
\set ON_ERROR_STOP on
\if :{?restore_database}
\else
  \echo 'Required: -v restore_database=<isolated restored database>'
  DO $$BEGIN RAISE EXCEPTION 'Restore fence refused; see preceding diagnostic'; END$$;
\endif
\if :{?isolated_restore}
\else
  \echo 'Required: -v isolated_restore=yes (see docs/RESTORE.md)'
  DO $$BEGIN RAISE EXCEPTION 'Restore fence refused; see preceding diagnostic'; END$$;
\endif
SELECT current_database() = :'restore_database'
       AND current_database() ~ '^sill_(test_)?restore[0-9_]'
       AND :'isolated_restore' = 'yes' AS restore_fence \gset
\if :restore_fence
\else
  \echo 'Refusing: exact restore_database, sill_restore_ prefix and isolated_restore=yes required.'
  DO $$BEGIN RAISE EXCEPTION 'Restore fence refused; see preceding diagnostic'; END$$;
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = pg_catalog, ag_catalog;
DO $preflight$
BEGIN
  IF current_setting('server_version_num')::integer NOT BETWEEN 160000 AND 169999
     OR (SELECT extversion FROM pg_extension WHERE extname='age') IS DISTINCT FROM '1.6.0' THEN
    RAISE EXCEPTION 'Unsupported PostgreSQL/AGE version: this restore step supports PostgreSQL 16 / AGE 1.6.0; obtain a reviewed catalog-specific procedure';
  END IF;
  IF EXISTS (SELECT FROM pg_stat_activity WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database()) AND pid<>pg_backend_pid()) THEN
    RAISE EXCEPTION 'Restore target has other sessions; isolate it before catalog repair';
  END IF;
END
$preflight$;
LOCK TABLE ag_catalog.ag_graph, ag_catalog.ag_label IN ACCESS EXCLUSIVE MODE;

DO $repair$
DECLARE
  actual text[];
BEGIN
  SELECT array_agg(c.relname || ':' || a.attname || ':' || a.atttypid::text || ':' || a.attnotnull::text ORDER BY c.relname,a.attnum)
    INTO actual
    FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
    WHERE a.attrelid IN ('ag_catalog.ag_graph'::regclass,'ag_catalog.ag_label'::regclass) AND a.attnum>0;
  IF actual IS DISTINCT FROM ARRAY[
    'ag_graph:graphid:' || 'oid'::regtype::oid || ':true',
    'ag_graph:name:' || 'name'::regtype::oid || ':true',
    'ag_graph:namespace:' || 'regnamespace'::regtype::oid || ':true',
    'ag_label:name:' || 'name'::regtype::oid || ':true',
    'ag_label:graph:' || 'oid'::regtype::oid || ':true',
    'ag_label:id:' || 'ag_catalog.label_id'::regtype::oid || ':false',
    'ag_label:kind:' || 'ag_catalog.label_kind'::regtype::oid || ':false',
    'ag_label:relation:' || 'regclass'::regtype::oid || ':true',
    'ag_label:seq_name:' || 'name'::regtype::oid || ':true'
  ] THEN
    RAISE EXCEPTION 'Unsupported AGE catalog layout; no repair attempted';
  END IF;
  -- Exactly the immediate, validated NO ACTION FK observed in supported AGE.
  -- Refuse alternate constraints rather than silently changing their semantics.
  IF (SELECT count(*) FROM pg_constraint WHERE conrelid IN ('ag_catalog.ag_graph'::regclass,'ag_catalog.ag_label'::regclass)
      OR confrelid IN ('ag_catalog.ag_graph'::regclass,'ag_catalog.ag_label'::regclass)) <> 1
    OR NOT EXISTS (SELECT FROM pg_constraint WHERE conname='fk_graph_oid'
      AND conrelid='ag_catalog.ag_label'::regclass AND confrelid='ag_catalog.ag_graph'::regclass
      AND contype='f' AND conkey=ARRAY[2]::smallint[] AND confkey=ARRAY[1]::smallint[]
      AND confupdtype='a' AND confdeltype='a' AND confmatchtype='s'
      AND NOT condeferrable AND NOT condeferred AND convalidated) THEN
    RAISE EXCEPTION 'Unsupported AGE foreign key layout; no repair attempted';
  END IF;
  IF NOT EXISTS (SELECT FROM ag_catalog.ag_graph WHERE name='memory_graph')
    OR EXISTS (SELECT FROM ag_catalog.ag_graph g LEFT JOIN pg_namespace n ON n.oid=g.namespace::oid
      WHERE n.oid IS NULL OR n.nspname<>g.name OR n.nspname IN ('public','ag_catalog','information_schema') OR n.nspname LIKE 'pg_%')
    OR EXISTS (SELECT namespace FROM ag_catalog.ag_graph GROUP BY namespace HAVING count(*)<>1)
    OR EXISTS (SELECT graphid FROM ag_catalog.ag_graph GROUP BY graphid HAVING count(*)<>1) THEN
    RAISE EXCEPTION 'Invalid or ambiguous AGE graph namespace mapping; no repair attempted';
  END IF;
  -- Fail closed even for cycles/swaps; a generic OID allocator is out of scope.
  IF EXISTS (SELECT FROM ag_catalog.ag_graph a JOIN ag_catalog.ag_graph b
       ON a.namespace::oid=b.graphid WHERE a.graphid<>b.graphid) THEN
    RAISE EXCEPTION 'AGE OID mapping collision: a target namespace OID belongs to another graph; obtain a reviewed restore procedure';
  END IF;
  IF EXISTS (SELECT FROM ag_catalog.ag_label l LEFT JOIN ag_catalog.ag_graph g ON g.graphid=l.graph
      LEFT JOIN pg_class c ON c.oid=l.relation::oid
      LEFT JOIN pg_class s ON s.relnamespace=g.namespace::oid AND s.relname=l.seq_name AND s.relkind='S'
      WHERE g.graphid IS NULL OR c.oid IS NULL OR c.relnamespace<>g.namespace::oid
         OR c.relname<>l.name OR c.relkind<>'r' OR s.oid IS NULL
         OR l.id IS NULL OR l.id::integer NOT BETWEEN 1 AND 65535
         OR l.kind IS NULL OR l.kind::text NOT IN ('v','e')) THEN
    RAISE EXCEPTION 'Invalid AGE label relation, sequence or graph mapping; no repair attempted';
  END IF;
  IF EXISTS (SELECT FROM ag_catalog.ag_graph g WHERE
      NOT EXISTS (SELECT FROM ag_catalog.ag_label WHERE graph=g.graphid AND name='_ag_label_vertex' AND id=1 AND kind='v')
      OR NOT EXISTS (SELECT FROM ag_catalog.ag_label WHERE graph=g.graphid AND name='_ag_label_edge' AND id=2 AND kind='e')) THEN
    RAISE EXCEPTION 'Invalid AGE base labels; no repair attempted';
  END IF;
  IF EXISTS (SELECT FROM ag_catalog.ag_label l WHERE
      (l.id IN (1,2) AND EXISTS (SELECT FROM pg_inherits WHERE inhrelid=l.relation))
      OR (l.id NOT IN (1,2) AND (
        (SELECT count(*) FROM pg_inherits WHERE inhrelid=l.relation)<>1
        OR NOT EXISTS (SELECT FROM pg_inherits i JOIN ag_catalog.ag_label base
          ON base.relation=i.inhparent WHERE i.inhrelid=l.relation
          AND base.graph=l.graph AND base.kind=l.kind
          AND base.id=CASE WHEN l.kind='v' THEN 1 ELSE 2 END)))) THEN
    RAISE EXCEPTION 'Invalid AGE label inheritance; no repair attempted';
  END IF;
  -- Reject catalog uniqueness corruption before the first catalog mutation.
  IF EXISTS (SELECT graph,id FROM ag_catalog.ag_label GROUP BY graph,id HAVING count(*)>1)
    OR EXISTS (SELECT graph,name FROM ag_catalog.ag_label GROUP BY graph,name HAVING count(*)>1)
    OR EXISTS (SELECT relation FROM ag_catalog.ag_label GROUP BY relation HAVING count(*)>1)
    OR EXISTS (SELECT graph,seq_name FROM ag_catalog.ag_label GROUP BY graph,seq_name HAVING count(*)>1) THEN
    RAISE EXCEPTION 'Invalid AGE label uniqueness; no repair attempted';
  END IF;

  ALTER TABLE ag_catalog.ag_label ALTER CONSTRAINT fk_graph_oid DEFERRABLE INITIALLY IMMEDIATE;
  SET CONSTRAINTS ag_catalog.fk_graph_oid DEFERRED;
  UPDATE ag_catalog.ag_label l SET graph=g.namespace::oid FROM ag_catalog.ag_graph g
    WHERE l.graph=g.graphid AND g.graphid<>g.namespace::oid;
  UPDATE ag_catalog.ag_graph SET graphid=namespace::oid WHERE graphid<>namespace::oid;
  -- Force all queued FK checks now, before restoring the original flags.
  SET CONSTRAINTS ag_catalog.fk_graph_oid IMMEDIATE;
  ALTER TABLE ag_catalog.ag_label ALTER CONSTRAINT fk_graph_oid NOT DEFERRABLE INITIALLY IMMEDIATE;
  IF EXISTS (SELECT FROM ag_catalog.ag_graph WHERE graphid<>namespace::oid)
    OR EXISTS (SELECT FROM ag_catalog.ag_label l LEFT JOIN ag_catalog.ag_graph g ON g.graphid=l.graph WHERE g.graphid IS NULL) THEN
    RAISE EXCEPTION 'AGE catalog verification failed; transaction rolled back';
  END IF;
END
$repair$;
COMMIT;
\echo 'Catalog step committed. Restore is NOT verified: run the fresh-session checks in docs/RESTORE.md.'
