-- 009_production_and_minted_in.sql
-- Ported from Sili migration 034 (2026-11-13 public adaptation).
-- Optional declared production is never inferred from speaker. Successors inherit
-- the declaration, append claims or explicitly redeclare while retaining history.
-- Mint context belongs to this row. Intentional declaration repair uses transaction-
-- local SET LOCAL sill.production_repair = on; never enable it globally.
-- No historical production or mint context is backfilled.

BEGIN;
SET LOCAL search_path = public, ag_catalog;

ALTER TABLE memories ADD COLUMN IF NOT EXISTS production jsonb
    CONSTRAINT memories_production_chk
    CHECK (production IS NULL OR (jsonb_typeof(production) = 'object' AND jsonb_typeof(production->'made') IS NOT DISTINCT FROM 'array'));

ALTER TABLE memories ADD COLUMN IF NOT EXISTS minted_in jsonb
    CONSTRAINT memories_minted_in_chk
    CHECK (minted_in IS NULL OR jsonb_typeof(minted_in) = 'object');

COMMENT ON COLUMN memories.production IS
    'Declared intellectual production, object with made array. NULL means undeclared; never inferred from speaker. Successors inherit verbatim. Intentional replacement requires SET LOCAL sill.production_repair = on and stacks the prior declaration.';
COMMENT ON COLUMN memories.minted_in IS
    'Context in which this row was written, supplied by the host: harness, session, precise model if known, entrypoint and time. Per row; never inherited.';
COMMENT ON COLUMN memories.speaker IS
    'Whose utterance this records. NULL means no particular speaker. It is not evidence of who produced the idea (production).';

CREATE OR REPLACE FUNCTION public.memories_production_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
BEGIN
    NEW.production:=NULLIF(NEW.production,'null'::jsonb);
    NEW.minted_in:=NULLIF(NEW.minted_in,'null'::jsonb);
    IF TG_OP = 'INSERT' THEN
        -- A raw supersession insert that bypassed supersede_memory() inherits verbatim.
        IF NEW.supersedes_id IS NOT NULL AND NEW.production IS NULL THEN
            SELECT production INTO NEW.production FROM memories WHERE id = NEW.supersedes_id;
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.production IS NULL THEN
        RETURN NEW;  -- null to a value: the post-insert tag
    END IF;
    IF NEW.production IS NOT DISTINCT FROM OLD.production THEN
        RETURN NEW;  -- same value: a no-op
    END IF;
    IF NEW.production IS NULL THEN
        RAISE EXCEPTION 'production is never erased; re-declare with a new object'
            USING ERRCODE = '22023';
    END IF;
    IF current_setting('sill.production_repair', true) = 'on' THEN
        -- A sanctioned repair: the previous declaration was false; stack it, lose nothing.
        -- A missing or JSON-null stack counts as empty.
        NEW.production := jsonb_set(NEW.production, '{superseded_declarations}',
            CASE jsonb_typeof(NEW.production->'superseded_declarations')
                WHEN 'array' THEN NEW.production->'superseded_declarations' ELSE '[]'::jsonb END
            || jsonb_build_array(OLD.production));
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'production already declared on %; a different value needs SET LOCAL sill.production_repair = on (stacks the old declaration) or a supersede', OLD.id
        USING ERRCODE = '22023';
END $$;

DROP TRIGGER IF EXISTS trg_production_guard ON memories;
CREATE TRIGGER trg_production_guard
    BEFORE INSERT OR UPDATE OF production, minted_in ON memories
    FOR EACH ROW EXECUTE FUNCTION public.memories_production_guard();

CREATE OR REPLACE FUNCTION public.supersede_memory(
    p_old_id UUID, p_new_content TEXT, p_reason TEXT, p_metadata JSONB
) RETURNS UUID LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE
    old_mem memories%ROWTYPE;
    new_id UUID;
    historical_type JSONB;
    historical_ancestors JSONB;
    current_type JSONB;
    current_source JSONB;
    current_concepts JSONB;
    concept_source UUID;
    concept_item JSONB;
    concept_uuid UUID;
    semantic_data semantic_memories%ROWTYPE;
    episodic_data episodic_memories%ROWTYPE;
    procedural_data procedural_memories%ROWTYPE;
    strategic_data strategic_memories%ROWTYPE;
    allowed_type_keys TEXT[];
    optional_key TEXT;
    new_production JSONB;
    production_item JSONB;
    instr JSONB;
    match_count INT;
    match_index INT;
BEGIN
    p_metadata:=COALESCE(NULLIF(p_metadata,'null'::jsonb),'{}'::jsonb);
    IF p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
       OR EXISTS (SELECT 1 FROM jsonb_object_keys(p_metadata) k
                  WHERE k NOT IN ('speaker','force','source_attribution','concepts','type_metadata',
                                  'production','production_add','production_not_carried','minted_in')) THEN
        RAISE EXCEPTION 'supersession metadata must be an object with supported keys'
            USING ERRCODE = '22023';
    END IF;
    -- Null optional top-level values mean omission. Keep explicit [] (clearing)
    -- and nested nulls in source/declaration objects exactly as supplied.
    FOREACH optional_key IN ARRAY ARRAY['speaker','force','source_attribution','concepts','type_metadata',
        'production','production_add','production_not_carried','minted_in'] LOOP
        IF p_metadata->optional_key = 'null'::jsonb THEN
            p_metadata:=p_metadata-optional_key;
        END IF;
    END LOOP;
    IF p_metadata ? 'production' AND (p_metadata ? 'production_add' OR p_metadata ? 'production_not_carried') THEN
        RAISE EXCEPTION 'production re-declares the whole object; do not combine it with production_add or production_not_carried'
            USING ERRCODE='22023';
    END IF;
    IF p_new_content IS NULL OR btrim(p_new_content) = '' THEN
        RAISE EXCEPTION 'replacement content must not be empty' USING ERRCODE = '22023';
    END IF;
    -- Serialize contenders: after waiting, the losing caller sees archived state.
    SELECT * INTO old_mem FROM memories WHERE id=p_old_id FOR UPDATE;
    IF NOT FOUND OR old_mem.status <> 'active' OR old_mem.archived_at IS NOT NULL THEN
        RAISE EXCEPTION 'Memory % not found or already archived',p_old_id;
    END IF;
    -- Production: a full re-declaration stacks the predecessor's whole object;
    -- otherwise inherit verbatim, attach not_carried claims to inherited entries,
    -- then append new entries.
    IF p_metadata ? 'production' THEN
        new_production:=p_metadata->'production';
        IF jsonb_typeof(new_production) <> 'object'
           OR jsonb_typeof(new_production->'made') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'production must be an object with a made array' USING ERRCODE='22023';
        END IF;
        IF old_mem.production IS NOT NULL THEN
            -- A missing or JSON-null stack counts as empty.
            new_production:=jsonb_set(new_production,'{superseded_declarations}',
                CASE jsonb_typeof(new_production->'superseded_declarations')
                    WHEN 'array' THEN new_production->'superseded_declarations' ELSE '[]'::jsonb END
                ||jsonb_build_array(old_mem.production));
        END IF;
    ELSE
        new_production:=old_mem.production;
        IF p_metadata ? 'production_not_carried' AND jsonb_typeof(p_metadata->'production_not_carried') <> 'array' THEN
            RAISE EXCEPTION 'production_not_carried must be an array of instructions' USING ERRCODE='22023';
        END IF;
        IF p_metadata ? 'production_add' AND jsonb_typeof(p_metadata->'production_add') <> 'array' THEN
            RAISE EXCEPTION 'production_add must be an array of entries' USING ERRCODE='22023';
        END IF;
        -- Empty lists declare nothing: an undeclared predecessor stays undeclared.
        IF new_production IS NULL
           AND (jsonb_array_length(COALESCE(p_metadata->'production_add','[]'::jsonb)) > 0
                OR jsonb_array_length(COALESCE(p_metadata->'production_not_carried','[]'::jsonb)) > 0) THEN
            new_production:='{"made":[],"joint":null,"unknown":null,"none":null,"superseded_declarations":[],"note":null}'::jsonb;
        END IF;
        -- not_carried first, so an instruction can only match an inherited entry,
        -- never one this call adds. An entry already not carried is not matched
        -- again, so its claim is never rewritten.
        IF p_metadata ? 'production_not_carried' THEN
            FOR instr IN SELECT value FROM jsonb_array_elements(p_metadata->'production_not_carried') LOOP
                SELECT count(*),min(e.ord) INTO match_count,match_index
                  FROM jsonb_array_elements(new_production->'made') WITH ORDINALITY e(value,ord)
                 WHERE NOT (e.value ? 'not_carried')
                   AND e.value->>'work' = instr#>>'{match,work}'
                   AND (instr#>>'{match,who}' IS NULL OR e.value->>'who' = instr#>>'{match,who}')
                   AND (instr#>>'{match,session}' IS NULL OR e.value->>'session' = instr#>>'{match,session}');
                IF match_count <> 1 THEN
                    RAISE EXCEPTION 'production_not_carried must match exactly one inherited entry (matched %)',match_count
                        USING ERRCODE='22023';
                END IF;
                new_production:=jsonb_set(new_production,ARRAY['made',(match_index-1)::text],
                    (new_production->'made'->(match_index-1))||jsonb_build_object('not_carried',
                        jsonb_build_object('because',instr->'because','declared',instr->'declared')));
            END LOOP;
        END IF;
        IF p_metadata ? 'production_add' THEN
            -- A duplicate is only an entry identical as JSON; no collapse on who or as.
            FOR production_item IN SELECT value FROM jsonb_array_elements(p_metadata->'production_add') LOOP
                IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(new_production->'made') e WHERE e.value=production_item) THEN
                    new_production:=jsonb_set(new_production,'{made}',(new_production->'made')||jsonb_build_array(production_item));
                END IF;
            END LOOP;
        END IF;
    END IF;
    IF old_mem.type='semantic' THEN
        SELECT to_jsonb(s)-'memory_id' INTO historical_type FROM semantic_memories s WHERE memory_id=p_old_id;
        allowed_type_keys:=ARRAY['confidence','last_validated','source_references','contradictions','category','related_concepts'];
    ELSIF old_mem.type='episodic' THEN
        SELECT to_jsonb(s)-'memory_id' INTO historical_type FROM episodic_memories s WHERE memory_id=p_old_id;
        allowed_type_keys:=ARRAY['action_taken','context','result','emotional_valence','verification_status','event_time'];
    ELSIF old_mem.type='procedural' THEN
        SELECT to_jsonb(s)-'memory_id' INTO historical_type FROM procedural_memories s WHERE memory_id=p_old_id;
        allowed_type_keys:=ARRAY['steps','prerequisites','success_count','total_attempts','average_duration','failure_points'];
    ELSIF old_mem.type='strategic' THEN
        SELECT to_jsonb(s)-'memory_id' INTO historical_type FROM strategic_memories s WHERE memory_id=p_old_id;
        allowed_type_keys:=ARRAY['pattern_description','supporting_evidence','confidence_score','success_metrics','adaptation_history','context_applicability'];
    END IF;
    current_type:=COALESCE(NULLIF(p_metadata->'type_metadata','null'::jsonb),'{}'::jsonb);
    IF jsonb_typeof(current_type) <> 'object'
       OR EXISTS(SELECT 1 FROM jsonb_object_keys(current_type) k WHERE NOT k=ANY(allowed_type_keys)) THEN
        RAISE EXCEPTION 'type_metadata contains unsupported fields for %',old_mem.type USING ERRCODE='22023';
    END IF;
    current_source:=COALESCE(p_metadata->'source_attribution',
        jsonb_build_object('kind','supersession','attribution_status','unspecified','observed_at',now()));
    IF jsonb_typeof(current_source) <> 'object' THEN
        RAISE EXCEPTION 'source_attribution must be an object' USING ERRCODE='22023';
    END IF;
    current_source:=current_source || jsonb_build_object('predecessor',jsonb_build_object(
        'id',old_mem.id,'speaker',old_mem.speaker,'force',old_mem.force,
        'source_attribution',old_mem.source_attribution,'type',old_mem.type,
        'type_metadata',historical_type,'trust_level',old_mem.trust_level,
        'trust_updated_at',old_mem.trust_updated_at,'production',old_mem.production));
    -- A pre-migration intermediate may have dropped its metadata. Keep older
    -- source records visible without promoting any ancestor to current author.
    WITH RECURSIVE ancestry AS (
        SELECT id,supersedes_id,0 AS depth,ARRAY[id] AS seen FROM memories WHERE id=p_old_id
        UNION ALL
        SELECT m.id,m.supersedes_id,a.depth+1,a.seen||m.id
        FROM memories m JOIN ancestry a ON m.id=a.supersedes_id WHERE NOT m.id=ANY(a.seen)
    ) SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id',m.id,'speaker',m.speaker,'force',m.force,'type',m.type,
        'source_attribution',m.source_attribution-'predecessor'-'metadata_ancestors',
        'type_metadata',COALESCE(to_jsonb(sm)-'memory_id',to_jsonb(em)-'memory_id',
                                to_jsonb(pm)-'memory_id',to_jsonb(stm)-'memory_id'),
        'production',m.production) ORDER BY a.depth),'[]'::jsonb)
      INTO historical_ancestors FROM ancestry a JOIN memories m ON m.id=a.id
      LEFT JOIN semantic_memories sm ON sm.memory_id=m.id
      LEFT JOIN episodic_memories em ON em.memory_id=m.id
      LEFT JOIN procedural_memories pm ON pm.memory_id=m.id
      LEFT JOIN strategic_memories stm ON stm.memory_id=m.id WHERE a.depth>0;
    current_source:=(current_source-'concepts_inherited_from')||jsonb_build_object(
        'metadata_ancestors',historical_ancestors,'concepts_explicit',p_metadata ? 'concepts');

    IF p_metadata ? 'concepts' THEN
        current_concepts:=p_metadata->'concepts';
        IF jsonb_typeof(current_concepts) <> 'array' THEN
            RAISE EXCEPTION 'concepts must be an array (use [] to clear)' USING ERRCODE='22023';
        END IF;
    ELSE
        -- Archived intermediates are part of provenance, not a retrieval filter.
        WITH RECURSIVE ancestry AS (
            SELECT id,supersedes_id,0 AS depth,ARRAY[id] AS seen FROM memories WHERE id=p_old_id
            UNION ALL
            SELECT m.id,m.supersedes_id,a.depth+1,a.seen||m.id
            FROM memories m JOIN ancestry a ON m.id=a.supersedes_id
            WHERE NOT m.id=ANY(a.seen)
        ) SELECT a.id INTO concept_source FROM ancestry a JOIN memories m ON m.id=a.id
          WHERE EXISTS(SELECT 1 FROM memory_concepts mc WHERE mc.memory_id=a.id)
             OR m.source_attribution->'concepts_explicit'='true'::jsonb
          ORDER BY a.depth LIMIT 1;
        SELECT COALESCE(jsonb_agg(jsonb_build_object('concept_id',concept_id,'strength',strength)),'[]'::jsonb)
          INTO current_concepts FROM memory_concepts WHERE memory_id=concept_source;
        current_source:=current_source||jsonb_build_object('concepts_inherited_from',concept_source);
    END IF;

    INSERT INTO memories(content,type,importance,decay_rate,speaker,force,source_attribution,
                         supersedes_id,derived_from,embedding,production,minted_in)
    VALUES(p_new_content,old_mem.type,old_mem.importance,old_mem.decay_rate,
           p_metadata->>'speaker',p_metadata->>'force',current_source,p_old_id,
           jsonb_build_object('premises',jsonb_build_array(p_old_id::text),'reason',COALESCE(p_reason,'supersession')),
           public.get_embedding(p_new_content),new_production,
           COALESCE(NULLIF(p_metadata->'minted_in','null'::jsonb),
                    jsonb_build_object('via','supersede_memory()','when',now()))) RETURNING id INTO new_id;

    IF old_mem.type='semantic' THEN
        SELECT * INTO semantic_data FROM jsonb_populate_record(NULL::semantic_memories,current_type);
        INSERT INTO semantic_memories(memory_id,confidence,last_validated,source_references,contradictions,category,related_concepts)
        VALUES(new_id,COALESCE(semantic_data.confidence,0.5),semantic_data.last_validated,
               semantic_data.source_references,semantic_data.contradictions,semantic_data.category,semantic_data.related_concepts);
    ELSIF old_mem.type='episodic' THEN
        SELECT * INTO episodic_data FROM jsonb_populate_record(NULL::episodic_memories,current_type);
        INSERT INTO episodic_memories(memory_id,action_taken,context,result,emotional_valence,verification_status,event_time)
        VALUES(new_id,episodic_data.action_taken,episodic_data.context,episodic_data.result,
               episodic_data.emotional_valence,episodic_data.verification_status,episodic_data.event_time);
    ELSIF old_mem.type='procedural' THEN
        SELECT * INTO procedural_data FROM jsonb_populate_record(NULL::procedural_memories,current_type);
        INSERT INTO procedural_memories(memory_id,steps,prerequisites,success_count,total_attempts,average_duration,failure_points)
        VALUES(new_id,COALESCE(procedural_data.steps,'[]'::jsonb),procedural_data.prerequisites,
               COALESCE(procedural_data.success_count,0),COALESCE(procedural_data.total_attempts,0),
               procedural_data.average_duration,procedural_data.failure_points);
    ELSIF old_mem.type='strategic' THEN
        SELECT * INTO strategic_data FROM jsonb_populate_record(NULL::strategic_memories,current_type);
        INSERT INTO strategic_memories(memory_id,pattern_description,supporting_evidence,confidence_score,success_metrics,adaptation_history,context_applicability)
        VALUES(new_id,COALESCE(strategic_data.pattern_description,p_new_content),strategic_data.supporting_evidence,
               strategic_data.confidence_score,strategic_data.success_metrics,strategic_data.adaptation_history,strategic_data.context_applicability);
    END IF;
    FOR concept_item IN SELECT value FROM jsonb_array_elements(current_concepts) LOOP
        IF jsonb_typeof(concept_item)<>'object' OR
           EXISTS(SELECT 1 FROM jsonb_object_keys(concept_item) k WHERE k NOT IN ('concept_id','name','strength')) OR
           (concept_item ? 'concept_id') = (concept_item ? 'name') THEN
            RAISE EXCEPTION 'each concept needs exactly one of concept_id or name, and optional strength' USING ERRCODE='22023';
        END IF;
        IF concept_item ? 'concept_id' THEN
            concept_uuid:=(concept_item->>'concept_id')::uuid;
        ELSE
            IF NULLIF(btrim(concept_item->>'name'),'') IS NULL THEN
                RAISE EXCEPTION 'concept name must not be empty' USING ERRCODE='22023';
            END IF;
            INSERT INTO concepts(name) VALUES(concept_item->>'name') ON CONFLICT(name) DO NOTHING;
            SELECT id INTO STRICT concept_uuid FROM concepts WHERE name=concept_item->>'name';
        END IF;
        INSERT INTO memory_concepts(memory_id,concept_id,strength)
        VALUES(new_id,concept_uuid,COALESCE((concept_item->>'strength')::float,1.0));
    END LOOP;
    UPDATE memories SET status='archived',archived_at=now(),archive_reason='superseded by '||new_id::text WHERE id=p_old_id;
    RETURN new_id;
END $$;

CREATE OR REPLACE FUNCTION public.supersede_memory(p_old_id UUID,p_new_content TEXT,p_reason TEXT DEFAULT NULL)
RETURNS UUID LANGUAGE sql SET search_path = public, ag_catalog AS $$ SELECT public.supersede_memory(p_old_id,p_new_content,p_reason,'{}'::jsonb) $$;

-- Verify (an exception rolls back the whole migration).
DO $$
DECLARE n_columns int; has_trigger boolean; has_function boolean; has_body boolean:=false;
BEGIN
    SELECT count(*) INTO n_columns FROM information_schema.columns
     WHERE table_schema='public' AND table_name='memories' AND column_name IN ('production','minted_in');
    has_trigger:=EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='trg_production_guard');
    has_function:=to_regprocedure('public.supersede_memory(uuid,text,text,jsonb)') IS NOT NULL;
    -- The function existed before 009; check that its body is this migration's.
    IF has_function THEN
        has_body:=EXISTS(SELECT 1 FROM pg_proc
                         WHERE oid='public.supersede_memory(uuid,text,text,jsonb)'::regprocedure
                           AND prosrc LIKE '%production_not_carried%');
    END IF;
    RAISE NOTICE '009: production and minted_in columns present: % of 2', n_columns;
    RAISE NOTICE '009: trg_production_guard present: %', has_trigger;
    RAISE NOTICE '009: public.supersede_memory(uuid,text,text,jsonb) present: %, with the 009 body: %',
        has_function, has_body;
    IF n_columns <> 2 OR NOT has_trigger OR NOT has_function OR NOT has_body THEN
        RAISE EXCEPTION '009 verification failed (columns %, trigger %, function %, 009 body %); aborting',
            n_columns, has_trigger, has_function, has_body;
    END IF;
END $$;

COMMIT;
