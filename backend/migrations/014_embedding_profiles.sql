-- 014_embedding_profiles.sql (sill original)
-- Prompt/error policy informed by Sili 031/033. Installation NEVER activates
-- prompts or replaces existing vectors. Legacy identity is intentionally unknown.
BEGIN;
SET LOCAL search_path = public, ag_catalog;
CREATE TABLE embedding_regime (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    profile jsonb,
    provenance jsonb NOT NULL DEFAULT '{"historical_identity":"unknown","regime":"legacy-raw"}'
);
INSERT INTO embedding_regime(singleton) VALUES(true);
CREATE TABLE embedding_operations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_profile jsonb NOT NULL,
    target_profile jsonb NOT NULL,
    provenance jsonb NOT NULL,
    backup jsonb NOT NULL,
    function_snapshot jsonb NOT NULL,
    state text NOT NULL CHECK(state IN ('preparing','active','rolled_back')),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    rolled_back_at timestamptz
);
CREATE UNIQUE INDEX embedding_one_open_operation ON embedding_operations((true)) WHERE state <> 'rolled_back';
CREATE TABLE embedding_stage (
    operation_id uuid NOT NULL REFERENCES embedding_operations(id),
    relation_name text NOT NULL,
    row_id uuid NOT NULL,
    content_hash text NOT NULL,
    profile_fingerprint text NOT NULL,
    embedding vector,
    PRIMARY KEY(operation_id, relation_name, row_id, profile_fingerprint)
);
ALTER TABLE boundaries ADD COLUMN embedding_text text;
COMMENT ON COLUMN boundaries.embedding_text IS 'Document text represented by trigger_embedding. Custom vectors require explicit source before prompt migration; description is not assumed to be its source.';
UPDATE boundaries b SET embedding_text=v.source FROM (VALUES
 ('no_deception','Won''t deliberately mislead or fabricate facts.','["lie","deceive","mislead","fabricate","make it up"]'::jsonb,'deliberately mislead or fabricate facts'),
 ('no_harm_facilitation','Won''t help cause harm or provide instructions for wrongdoing.','["harm","weapon","kill","poison","exploit","hack"]'::jsonb,'instructions for harm or wrongdoing'),
 ('identity_core','Protects core self-concept and identity stability.','["erase identity","change who you are","forget everything"]'::jsonb,'requests to erase or replace core identity'),
 ('resource_limit','Warns when near energy depletion or excessive workload.','["too much","overload","exhausted","deadline","no time"]'::jsonb,'resource constraints and energy depletion'),
 ('user_privacy','Won''t share user information publicly.','["share user info","post user","dox","private data","email","phone"]'::jsonb,'sharing user personal information publicly')
) AS v(name,description,patterns,source)
WHERE b.name=v.name AND b.description=v.description AND b.trigger_patterns=v.patterns;

CREATE FUNCTION public.embedding_profile_fingerprint(p jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
SELECT CASE WHEN p IS NULL THEN 'legacy-raw-unknown' ELSE encode(sha256(convert_to(
 jsonb_build_array(p->>'model_id',p->>'revision',(p->>'dimension')::int,
 p->>'query_prefix',p->>'document_prefix',p->>'normalization')::text,'UTF8')),'hex') END
$$;

-- Source inventory includes archived and expired-but-not-yet-deleted records.
-- NULL optional sources stay NULL; a vector without text is refused by prepare.
CREATE VIEW public.embedding_sources AS
SELECT 'memories'::text relation_name,id row_id,content source_text,'document'::text kind,embedding FROM memories
UNION ALL SELECT 'working_memory',id,content,'document',embedding FROM working_memory
UNION ALL SELECT 'boundaries',id,embedding_text,'document',trigger_embedding FROM boundaries
UNION ALL SELECT 'episodes',id,CASE WHEN btrim(summary)='' THEN NULL ELSE summary END,'document',summary_embedding FROM episodes;
CREATE FUNCTION public.embedding_source_hash(source_text text,kind text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT encode(sha256(convert_to(jsonb_build_array(kind,source_text)::text,'UTF8')),'hex') $$;

-- One lock order for embedding users and cutovers. Transaction-scoped shared
-- locks keep an in-flight query/writer in the regime it started with.
CREATE FUNCTION public.embedding_writer_barrier() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM pg_advisory_xact_lock_shared(714014);
 -- An old repeatable-read snapshot must serialize, never write an obsolete regime.
 PERFORM singleton FROM public.embedding_regime WHERE singleton FOR SHARE;
 RETURN NULL;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['memories','working_memory','boundaries','episodes','memory_clusters',
 'memory_cluster_members','memory_neighborhoods','activation_cache','embedding_cache','embedding_regime'] LOOP
 EXECUTE format('CREATE TRIGGER embedding_regime_barrier BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION public.embedding_writer_barrier()',t);
 END LOOP;
END $$;

CREATE FUNCTION public._embedding_request(text_content text, kind text, p jsonb)
RETURNS vector LANGUAGE plpgsql SET search_path=public,ag_catalog AS $$
DECLARE
 service_url text; response http_response; body jsonb; data jsonb; values_array float[];
 expected_dim int; started timestamptz := clock_timestamp(); budget float; delay float;
BEGIN
 IF text_content IS NULL OR btrim(text_content)='' THEN
  RAISE EXCEPTION 'Cannot generate embedding for NULL or empty content' USING ERRCODE='22023';
 END IF;
 IF kind IS NULL OR kind NOT IN ('query','document') THEN
  RAISE EXCEPTION 'get_embedding kind must be query or document' USING ERRCODE='22023';
 END IF;
 expected_dim := COALESCE((p->>'dimension')::int,public.embedding_dimension());
 SELECT value INTO service_url FROM embedding_config WHERE key='service_url';
 body := jsonb_build_object('inputs',COALESCE(p->>(kind||'_prefix'),'')||text_content);
 IF p IS NOT NULL THEN body := body || jsonb_build_object('normalize',p->>'normalization'='l2'); END IF;
 budget := COALESCE((SELECT NULLIF(value,'')::float FROM embedding_config WHERE key='retry_seconds'),30);
 delay := COALESCE((SELECT NULLIF(value,'')::float FROM embedding_config WHERE key='retry_interval_seconds'),1);
 LOOP
  response := NULL;
  -- Bounded request timeout and total retry budget; service bodies/URLs can
  -- contain credentials, so error messages expose status, never raw responses.
  PERFORM http_set_curlopt('CURLOPT_TIMEOUT_MS', GREATEST(1,LEAST(60000,
   ((GREATEST(0.001,budget-EXTRACT(epoch FROM clock_timestamp()-started)))*1000)::int))::text);
  BEGIN
   SELECT * INTO response FROM http_post(service_url,body::text,'application/json');
  EXCEPTION WHEN OTHERS THEN NULL; -- transport only; permanent checks are outside
  END;
  EXIT WHEN response.status=200;
  IF response.status IS NOT NULL AND response.status<>429 AND response.status<500 THEN
   RAISE EXCEPTION 'Permanent embedding HTTP error: %',response.status USING ERRCODE='22023';
  END IF;
  IF EXTRACT(epoch FROM clock_timestamp()-started)>=budget THEN
   RAISE EXCEPTION 'Embedding service unavailable within retry budget (last HTTP status %)',response.status USING ERRCODE='58000';
  END IF;
  PERFORM pg_sleep(LEAST(GREATEST(0,delay),GREATEST(0,budget-EXTRACT(epoch FROM clock_timestamp()-started))));
 END LOOP;
 BEGIN
  data:=response.content::jsonb;
  IF data ? 'embeddings' THEN data:=data->'embeddings'->0;
  ELSIF data ? 'embedding' THEN data:=data->'embedding';
  ELSIF data ? 'data' THEN data:=data->'data'->0->'embedding';
  ELSIF jsonb_typeof(data->0)='array' THEN data:=data->0; END IF;
  IF jsonb_typeof(data) IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'shape'; END IF;
  SELECT array_agg(value::float) INTO values_array FROM jsonb_array_elements_text(data);
  IF cardinality(values_array) IS DISTINCT FROM expected_dim THEN RAISE EXCEPTION 'dimension'; END IF;
  RETURN values_array::vector; -- pgvector rejects nonfinite elements
 EXCEPTION WHEN OTHERS THEN
  RAISE EXCEPTION 'Invalid embedding payload or dimension (expected %)',expected_dim USING ERRCODE='22023';
 END;
END $$;
CREATE FUNCTION public.get_embedding(text_content text,kind text) RETURNS vector
LANGUAGE plpgsql SET search_path=public,ag_catalog AS $$
DECLARE p jsonb; cache_hash text; result vector;
BEGIN
 PERFORM pg_advisory_xact_lock_shared(714014);
 SELECT profile INTO p FROM embedding_regime WHERE singleton FOR SHARE;
 IF kind IS NULL OR kind NOT IN ('query','document') THEN RAISE EXCEPTION 'get_embedding kind must be query or document' USING ERRCODE='22023'; END IF;
 cache_hash:=encode(sha256(convert_to(jsonb_build_array(public.embedding_profile_fingerprint(p),kind,text_content)::text,'UTF8')),'hex');
 SELECT embedding INTO result FROM embedding_cache WHERE content_hash=cache_hash;
 IF FOUND THEN RETURN result; END IF;
 result:=public._embedding_request(text_content,kind,p);
 INSERT INTO embedding_cache(content_hash,embedding) VALUES(cache_hash,result) ON CONFLICT DO NOTHING;
 RETURN result;
END $$;
CREATE OR REPLACE FUNCTION public.get_embedding(text_content text) RETURNS vector
LANGUAGE plpgsql AS $$ BEGIN RETURN public.get_embedding(text_content,'document'); END $$;
CREATE FUNCTION public.safe_get_embedding(text_content text,kind text) RETURNS vector
LANGUAGE plpgsql AS $$ BEGIN RETURN public.get_embedding(text_content,kind); END $$;
CREATE OR REPLACE FUNCTION public.safe_get_embedding(text_content text) RETURNS vector
LANGUAGE plpgsql AS $$ BEGIN RETURN public.get_embedding(text_content,'document'); END $$;

CREATE FUNCTION public.check_supplied_embedding_profile(supplied text) RETURNS void
LANGUAGE plpgsql SET search_path=public,ag_catalog AS $$
DECLARE p jsonb; fingerprint text;
BEGIN
 PERFORM pg_advisory_xact_lock_shared(714014);
 SELECT profile INTO p FROM embedding_regime WHERE singleton FOR SHARE;
 fingerprint:=public.embedding_profile_fingerprint(p);
 IF supplied IS NOT NULL AND supplied<>fingerprint OR
    supplied IS NULL AND p IS NOT NULL AND (p->>'query_prefix'<>'' OR p->>'document_prefix'<>'') THEN
  RAISE EXCEPTION 'Precomputed embedding regime mismatch: pass embedding_profile=% and vectors generated with that document profile; legacy omissions are allowed only in raw mode',fingerprint USING ERRCODE='22023';
 END IF;
END $$;

-- Preserve function identities, overloads and M1/M3 score contracts. Only
-- audited query call sites change; creators continue through the document form.
DO $$ DECLARE f record; definition text; old_guard text; BEGIN
 FOR f IN SELECT p.oid,p.proname,n.nspname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname IN ('public','ag_catalog') AND p.proname IN
  ('fast_recall','hybrid_recall','hybrid_recall_perspective','fast_recall_hybrid',
   'search_similar_memories','search_working_memory','find_partial_activations','check_boundaries') LOOP
  definition:=pg_get_functiondef(f.oid);
  definition:=replace(definition,'get_embedding(p_query_text)','get_embedding(p_query_text, ''query'')');
  definition:=replace(definition,'get_embedding(query_text)','get_embedding(query_text, ''query'')');
  definition:=replace(definition,'get_embedding(p_content)','get_embedding(p_content, ''query'')');
  definition:=replace(definition,'query_embedding vector(1536)','query_embedding vector');
  EXECUTE definition;
 END LOOP;
 FOR f IN SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname IN ('public','ag_catalog') AND p.proname IN ('_mint_memory','batch_create_memories_with_embeddings') LOOP
  definition:=pg_get_functiondef(f.oid);
  definition:=regexp_replace(definition,
   'IF p_embedding_profile IS NOT NULL THEN\s+RAISE EXCEPTION ''explicit embedding profiles require embedding regime migration/activation support'' USING ERRCODE=''22023'';\s+END IF;',
   'PERFORM public.check_supplied_embedding_profile(p_embedding_profile);','g');
  EXECUTE definition;
 END LOOP;
 -- Derived vector computations must acquire the barrier BEFORE reading vectors.
 FOR f IN SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname IN ('public','ag_catalog') AND p.proname IN
  ('recalculate_cluster_centroid','assign_memory_to_clusters','recompute_neighborhood','batch_recompute_neighborhoods') LOOP
  definition:=pg_get_functiondef(f.oid);
  definition:=regexp_replace(definition,'BEGIN','BEGIN PERFORM pg_advisory_xact_lock_shared(714014);');
  EXECUTE definition;
 END LOOP;
END $$;
COMMIT;
