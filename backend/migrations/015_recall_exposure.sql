-- 015_recall_exposure.sql — sill original, adapted from Sili exposure policy.
-- Display is exposure; explicit by-ID use/citation alone refreshes recency.
-- No historical dates are rewritten. Access counts never feed importance.
BEGIN;

-- Baseline installations differ in the schema owning record_memory_reuse.
-- Amend existing identities, defaults and provenance event writes in place.
DO $$
DECLARE f record; definition text; changed text;
BEGIN
 FOR f IN SELECT p.oid,p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname IN ('public','ag_catalog')
     AND p.proname IN ('record_memory_reuse','search_working_memory')
 LOOP
  definition := pg_get_functiondef(f.oid);
  IF f.proname='record_memory_reuse' THEN
   changed := replace(definition, 'last_reused = NOW()',
    'last_reused = NOW(), last_accessed = CASE WHEN p_evidence_kind = ''memory_id'' THEN NOW() ELSE last_accessed END');
  ELSE
   -- Retain the profile-aware query overload/barrier and expiry semantics.
   changed := regexp_replace(definition,
    'SET access_count = access_count \+ 1,\s*last_accessed = CURRENT_TIMESTAMP',
    'SET access_count = COALESCE(access_count, 0) + 1', 'g');
  END IF;
  IF changed=definition THEN
   RAISE EXCEPTION 'Unexpected % definition; exposure migration refused',f.proname;
  END IF;
  EXECUTE changed;
 END LOOP;
END $$;

COMMENT ON COLUMN memories.last_accessed IS
 'Last explicit use (by-ID retrieval, selected batch, touch or memory-ID citation). Automatic display does not refresh this date; historical values are preserved.';
COMMENT ON COLUMN memories.access_count IS
 'Exposure telemetry only. Counts displays, independently of importance and ranking.';
COMMIT;
