-- 010_atomic_mint.sql
-- Ported from Sili migration 035; public adaptation preserves 008/009 contracts.
-- Never auto-resolve historical duplicates. Review the listed IDs, explicitly
-- archive/merge the unwanted rows with their owners, then retry the upgrade.
BEGIN;
SET LOCAL search_path = public, ag_catalog;
LOCK TABLE memories IN SHARE ROW EXCLUSIVE MODE;
DO $$
DECLARE conflicts text;
BEGIN
    SELECT string_agg(ids, '; ' ORDER BY ids) INTO conflicts FROM (
        SELECT string_agg(id::text, ', ' ORDER BY id) AS ids
        FROM memories WHERE archived_at IS NULL
        GROUP BY coalesce(speaker,''),md5(content) HAVING count(*)>1
    ) d;
    IF conflicts IS NOT NULL THEN
        RAISE EXCEPTION '010 preflight duplicate live speaker/content hashes: %. Review these IDs and explicitly resolve duplicates before retrying; no rows changed.', conflicts USING ERRCODE='55000';
    END IF;
END $$;
ALTER TABLE memories ADD COLUMN restates_id uuid REFERENCES memories(id) ON DELETE SET NULL;
CREATE INDEX idx_memories_restates ON memories(restates_id) WHERE restates_id IS NOT NULL;
CREATE UNIQUE INDEX idx_memories_live_mouth_content ON memories((coalesce(speaker,'')),md5(content)) WHERE archived_at IS NULL;
COMMENT ON COLUMN memories.restates_id IS 'Earliest live byte-identical utterance by a different speaker at mint time, ordered by created_at and id; never a supersession.';

CREATE OR REPLACE FUNCTION public._mint_memory(
    p_type memory_type, p_content text,
    p_importance double precision DEFAULT 0.5,
    p_source_attribution jsonb DEFAULT NULL,
    p_speaker text DEFAULT NULL, p_force text DEFAULT NULL,
    p_minted_in jsonb DEFAULT NULL, p_production jsonb DEFAULT NULL,
    p_type_metadata jsonb DEFAULT NULL, p_concepts text[] DEFAULT NULL,
    p_trust_level double precision DEFAULT NULL,
    p_embedding vector DEFAULT NULL, p_embedding_profile text DEFAULT NULL,
    p_supplied_embedding boolean DEFAULT false
) RETURNS TABLE (memory_id uuid, outcome text, restates uuid)
LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE
    existing_id uuid;
    existing_content text;
    new_id uuid;
    restates_row uuid;
    wrapper_source jsonb;
    wrapper_trust double precision;
    semantic_confidence double precision;
    semantic_refs jsonb;
    normalized_source jsonb;
    effective_trust double precision;
    embedding_vec vector;
    concept_name text;
BEGIN
    -- A JSON null is no value: no subtype row, no stamp (as mint_memories reads its items).
    p_type_metadata := NULLIF(p_type_metadata, 'null'::jsonb);
    p_minted_in := NULLIF(p_minted_in, 'null'::jsonb);
    p_production := NULLIF(p_production, 'null'::jsonb);

    p_source_attribution := NULLIF(p_source_attribution, 'null'::jsonb);
    IF p_supplied_embedding THEN
        IF p_embedding_profile IS NOT NULL THEN
            RAISE EXCEPTION 'explicit embedding profiles require embedding regime migration/activation support' USING ERRCODE='22023';
        END IF;
        IF p_embedding IS NULL OR vector_dims(p_embedding) <> public.embedding_dimension() THEN
            RAISE EXCEPTION 'embedding dimension mismatch or NULL embedding' USING ERRCODE='22023';
        END IF;
    END IF;
    -- 1. The content, checked as create_memory checks it.
    IF p_content IS NULL OR trim(p_content) = '' THEN
        RAISE EXCEPTION 'create_memory(%) called with NULL or empty content', p_type;
    END IF;

    -- Serialize by words, not mouth: concurrent different speakers must observe
    -- the first committed act before choosing their restatement target.
    PERFORM pg_advisory_xact_lock(hashtextextended(md5(p_content), 10));
    -- 2. Same mouth, same words: a retry or a glitch. The row already stands and is
    --    left exactly as it is: no patch, no concepts, no stamp. Type is not in the key.
    SELECT m.id, m.content INTO existing_id, existing_content
      FROM memories m
     WHERE coalesce(m.speaker, '') = coalesce(p_speaker, '')
       AND md5(m.content) = md5(p_content)
       AND m.archived_at IS NULL
     ORDER BY m.created_at, m.id
     LIMIT 1;
    IF existing_id IS NOT NULL THEN
        IF existing_content <> p_content THEN
            RAISE EXCEPTION 'content hash collision for speaker; conflicting memory %',existing_id USING ERRCODE='23505';
        END IF;
        RETURN QUERY SELECT existing_id, 'reused'::text, NULL::uuid;
        RETURN;
    END IF;

    IF p_type_metadata IS NOT NULL AND jsonb_typeof(p_type_metadata) <> 'object' THEN
        RAISE EXCEPTION 'type_metadata must be an object or null' USING ERRCODE='22023';
    END IF;
    -- 3. Source and trust. With metadata (the API), the typed wrapper's own rules run
    --    first, as create_<type>_memory runs them before it calls create_memory; then,
    --    with or without metadata, create_memory's normalization, defaults and clamp.
    wrapper_source := p_source_attribution;
    wrapper_trust := p_trust_level;
    IF p_type_metadata IS NOT NULL THEN
        IF p_type = 'semantic' THEN
            -- Legacy create_semantic_memory carries its explicit NULL verbatim:
            -- the stock NOT NULL constraint rejects it. Only omitted confidence
            -- (e.g. {} metadata) receives the ordinary creation default.
            semantic_confidence := CASE
                WHEN p_type_metadata->'confidence' = 'null'::jsonb THEN NULL
                ELSE COALESCE(NULLIF(p_type_metadata->>'confidence', '')::float, 0.8)
            END;
            semantic_refs := dedupe_source_references(p_type_metadata->'source_references');
            wrapper_source := normalize_source_reference(p_source_attribution);
            IF wrapper_source = '{}'::jsonb AND jsonb_typeof(semantic_refs) = 'array' AND jsonb_array_length(semantic_refs) > 0 THEN
                wrapper_source := normalize_source_reference(semantic_refs->0);
            END IF;
            IF wrapper_source = '{}'::jsonb THEN
                wrapper_source := jsonb_build_object('kind', 'unattributed', 'observed_at', CURRENT_TIMESTAMP);
            END IF;
            wrapper_trust := COALESCE(p_trust_level,
                compute_semantic_trust(LEAST(1.0, GREATEST(0.0, COALESCE(semantic_confidence, 0.5))), semantic_refs, 0.0));
        ELSE
            -- create_episodic_memory, create_procedural_memory, create_strategic_memory.
            wrapper_source := normalize_source_reference(p_source_attribution);
            IF wrapper_source = '{}'::jsonb THEN
                wrapper_source := jsonb_build_object('kind', 'internal', 'observed_at', CURRENT_TIMESTAMP);
            END IF;
            wrapper_trust := COALESCE(p_trust_level, CASE p_type
                WHEN 'episodic' THEN 0.95
                WHEN 'procedural' THEN 0.70
                WHEN 'strategic' THEN 0.70
            END);
        END IF;
    END IF;
    normalized_source := normalize_source_reference(wrapper_source);
    IF normalized_source = '{}'::jsonb THEN
        normalized_source := jsonb_build_object(
            'kind',
            CASE
                WHEN p_type = 'semantic' THEN 'unattributed'
                ELSE 'internal'
            END,
            'observed_at', CURRENT_TIMESTAMP
        );
    END IF;
    effective_trust := wrapper_trust;
    IF effective_trust IS NULL THEN
        effective_trust := CASE
            WHEN p_type = 'episodic' THEN 0.95
            WHEN p_type = 'semantic' THEN 0.20
            WHEN p_type = 'procedural' THEN 0.70
            WHEN p_type = 'strategic' THEN 0.70
            ELSE 0.50
        END;
    END IF;
    effective_trust := LEAST(1.0, GREATEST(0.0, effective_trust));

    -- 4. Another mouth, same words: a quotation of the earliest live row with them.
    SELECT m.id INTO restates_row
      FROM memories m
     WHERE md5(m.content) = md5(p_content) AND m.content = p_content
       AND m.archived_at IS NULL
       AND coalesce(m.speaker, '') <> coalesce(p_speaker, '')
     ORDER BY m.created_at, m.id
     LIMIT 1;

    -- 5. Generate the document vector only when none was supplied.
    embedding_vec := CASE WHEN p_supplied_embedding THEN p_embedding ELSE public.get_embedding(p_content) END;

    -- 6. The whole act in one insert. The production guard fires BEFORE INSERT and,
    --    with supersedes_id NULL, stores production verbatim; the table CHECKs
    --    validate force, minted_in and production.
    INSERT INTO memories (type, content, embedding, importance, source_attribution, trust_level,
                          trust_updated_at, speaker, force, production, minted_in, restates_id)
    VALUES (p_type, p_content, embedding_vec, p_importance, normalized_source, effective_trust,
            CURRENT_TIMESTAMP, p_speaker, p_force, p_production, p_minted_in, restates_row)
    ON CONFLICT ((coalesce(speaker, '')), md5(content)) WHERE archived_at IS NULL DO NOTHING
    RETURNING id INTO new_id;
    IF new_id IS NULL THEN
        -- Another transaction committed the same mouth and words first: its row stands.
        SELECT m.id, m.content INTO existing_id, existing_content
          FROM memories m
         WHERE coalesce(m.speaker, '') = coalesce(p_speaker, '')
           AND md5(m.content) = md5(p_content) AND m.content = p_content
           AND m.archived_at IS NULL
         ORDER BY m.created_at, m.id
         LIMIT 1;
        IF existing_id IS NULL THEN
            RAISE EXCEPTION 'content hash collision: idx_memories_live_mouth_content refused the insert but full text differs';
        END IF;
        RETURN QUERY SELECT existing_id, 'reused'::text, NULL::uuid;
        RETURN;
    END IF;

    -- 7. The subtype row, only with metadata, as each typed wrapper writes it (keys and
    --    defaults as batch_create_memories reads them). For semantic rows the AFTER
    --    INSERT trigger trg_semantic_trust_sync then runs as it does for the wrapper.
    IF p_type_metadata IS NOT NULL THEN
        IF p_type = 'episodic' THEN
            INSERT INTO episodic_memories (memory_id, action_taken, context, result,
                                           emotional_valence, event_time)
            VALUES (new_id, NULLIF(p_type_metadata->'action_taken','null'::jsonb), NULLIF(p_type_metadata->'context','null'::jsonb),
                    NULLIF(p_type_metadata->'result','null'::jsonb),
                    COALESCE(NULLIF(p_type_metadata->>'emotional_valence', '')::float, 0.0),
                    COALESCE(NULLIF(p_type_metadata->>'event_time', '')::timestamptz, CURRENT_TIMESTAMP));
        ELSIF p_type = 'semantic' THEN
            INSERT INTO semantic_memories (memory_id, confidence, category, related_concepts,
                                           source_references, last_validated)
            VALUES (new_id, semantic_confidence,
                    CASE WHEN jsonb_typeof(p_type_metadata->'category') = 'array'
                         THEN ARRAY(SELECT jsonb_array_elements_text(p_type_metadata->'category')) ELSE NULL END,
                    CASE WHEN jsonb_typeof(p_type_metadata->'related_concepts') = 'array'
                         THEN ARRAY(SELECT jsonb_array_elements_text(p_type_metadata->'related_concepts')) ELSE NULL END,
                    semantic_refs, CURRENT_TIMESTAMP);
        ELSIF p_type = 'procedural' THEN
            INSERT INTO procedural_memories (memory_id, steps, prerequisites)
            VALUES (new_id, COALESCE(NULLIF(p_type_metadata->'steps','null'::jsonb), jsonb_build_object('steps', '[]'::jsonb)),
                    NULLIF(p_type_metadata->'prerequisites','null'::jsonb));
        ELSIF p_type = 'strategic' THEN
            INSERT INTO strategic_memories (memory_id, pattern_description, confidence_score,
                                            supporting_evidence, context_applicability)
            VALUES (new_id, COALESCE(NULLIF(p_type_metadata->>'pattern_description', ''), p_content),
                    COALESCE(NULLIF(p_type_metadata->>'confidence_score', '')::float, 0.8),
                    NULLIF(p_type_metadata->'supporting_evidence','null'::jsonb), NULLIF(p_type_metadata->'context_applicability','null'::jsonb));
        END IF;
    END IF;

    -- 8. The graph node, as create_memory creates it.
    EXECUTE format(
        'SELECT * FROM cypher(''memory_graph'', $q$
            CREATE (n:MemoryNode {memory_id: %L, type: %L, created_at: %L})
            RETURN n
        $q$) as (result agtype)',
        new_id,
        p_type,
        CURRENT_TIMESTAMP
    );

    -- 9. Concepts, as the routes link them, only for an inserted row.
    IF p_concepts IS NOT NULL THEN
        FOREACH concept_name IN ARRAY p_concepts LOOP
            PERFORM link_memory_to_concept(new_id, concept_name, 1.0);
        END LOOP;
    END IF;

    -- 10. Inserted.
    RETURN QUERY SELECT new_id, 'inserted'::text, restates_row;
END $$;

CREATE OR REPLACE FUNCTION public.mint_memory(p_type memory_type, p_content text,
    p_importance double precision DEFAULT 0.5, p_source_attribution jsonb DEFAULT NULL,
    p_speaker text DEFAULT NULL, p_force text DEFAULT NULL,
    p_minted_in jsonb DEFAULT NULL, p_production jsonb DEFAULT NULL,
    p_type_metadata jsonb DEFAULT NULL, p_concepts text[] DEFAULT NULL,
    p_trust_level double precision DEFAULT NULL)
RETURNS TABLE(memory_id uuid,outcome text,restates uuid)
LANGUAGE sql SET search_path = public, ag_catalog AS $$
 SELECT * FROM public._mint_memory(p_type,p_content,p_importance,p_source_attribution,p_speaker,p_force,p_minted_in,p_production,p_type_metadata,p_concepts,p_trust_level) $$;
CREATE OR REPLACE FUNCTION public.mint_memory_with_embedding(p_type memory_type, p_content text, p_embedding vector,
    p_importance double precision DEFAULT 0.5, p_source_attribution jsonb DEFAULT NULL,
    p_speaker text DEFAULT NULL, p_force text DEFAULT NULL,
    p_minted_in jsonb DEFAULT NULL, p_production jsonb DEFAULT NULL,
    p_type_metadata jsonb DEFAULT NULL, p_concepts text[] DEFAULT NULL,
    p_trust_level double precision DEFAULT NULL, p_embedding_profile text DEFAULT NULL)
RETURNS TABLE(memory_id uuid,outcome text,restates uuid)
LANGUAGE sql SET search_path = public, ag_catalog AS $$
 SELECT * FROM public._mint_memory(p_type,p_content,p_importance,p_source_attribution,p_speaker,p_force,p_minted_in,p_production,p_type_metadata,p_concepts,p_trust_level,p_embedding,p_embedding_profile,true) $$;
CREATE OR REPLACE FUNCTION public.mint_memories(p_items jsonb)
RETURNS TABLE (idx int, memory_id uuid, outcome text, restates uuid)
LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE
    item jsonb;
    item_type memory_type;
    item_content text;
    minted record;
BEGIN
    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(k) FROM (
        SELECT DISTINCT hashtextextended(md5(value->>'content'),10) k
        FROM jsonb_array_elements(p_items)
    ) locks ORDER BY k;
    idx := 0;
    FOR item IN SELECT e.value FROM jsonb_array_elements(p_items) WITH ORDINALITY AS e(value, ord) ORDER BY e.ord
    LOOP
        idx := idx + 1;
        item_type := NULLIF(item->>'type', '')::memory_type;
        item_content := NULLIF(item->>'content', '');
        IF item_type IS NULL OR item_content IS NULL THEN
            RAISE EXCEPTION 'mint_memories: item % missing required fields', idx;
        END IF;
        -- Item keys are mint_memory's parameter names without p_; a missing key or a
        -- JSON null takes the default.
        SELECT * INTO minted FROM public.mint_memory(
            item_type,
            item_content,
            COALESCE(NULLIF(item->>'importance', '')::float, 0.5),
            NULLIF(item->'source_attribution', 'null'::jsonb),
            item->>'speaker',
            item->>'force',
            NULLIF(item->'minted_in', 'null'::jsonb),
            NULLIF(item->'production', 'null'::jsonb),
            NULLIF(item->'type_metadata', 'null'::jsonb),
            CASE WHEN jsonb_typeof(item->'concepts') = 'array'
                 THEN ARRAY(SELECT jsonb_array_elements_text(item->'concepts')) END,
            NULLIF(item->>'trust_level', '')::float);
        memory_id := minted.memory_id;
        outcome := minted.outcome;
        restates := minted.restates;
        RETURN NEXT;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.supersede_memory(
    p_old_id UUID, p_new_content TEXT, p_reason TEXT, p_metadata JSONB
) RETURNS UUID LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE
    old_mem memories%ROWTYPE;
    mint_outcome text;
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
    PERFORM pg_advisory_xact_lock(hashtextextended(md5(p_new_content),10));
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

    -- Archive in this transaction before minting identical words. Any failure
    -- (including an already standing successor) restores the predecessor.
    UPDATE memories SET status='archived',archived_at=now(),archive_reason='supersession pending' WHERE id=p_old_id;
    SELECT memory_id,outcome INTO new_id,mint_outcome FROM public.mint_memory(
        old_mem.type,p_new_content,old_mem.importance,current_source,
        p_metadata->>'speaker',p_metadata->>'force',
        COALESCE(p_metadata->'minted_in',jsonb_build_object('via','supersede_memory()','when',now())),
        new_production,NULL,NULL,0.5);
    IF mint_outcome <> 'inserted' THEN
        RAISE EXCEPTION 'successor already exists as live memory %; predecessor unchanged',new_id USING ERRCODE='23505';
    END IF;
    UPDATE memories SET decay_rate=old_mem.decay_rate,source_attribution=current_source,
        supersedes_id=p_old_id,
        derived_from=jsonb_build_object('premises',jsonb_build_array(p_old_id::text),'reason',COALESCE(p_reason,'supersession'))
        WHERE id=new_id;

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

-- Legacy generic calls mint a NULL-speaker bare row. They cannot adopt/retag
-- an attributed row. The AGE quality guard is intentionally namespace-specific.
CREATE OR REPLACE FUNCTION public.create_memory(p_type memory_type,p_content text,p_importance double precision DEFAULT 0.5,p_source_attribution jsonb DEFAULT NULL,p_trust_level double precision DEFAULT NULL) RETURNS uuid
LANGUAGE sql SET search_path = public, ag_catalog AS $$
SELECT memory_id FROM public.mint_memory(p_type,p_content,p_importance,p_source_attribution,p_trust_level=>p_trust_level) $$;

CREATE OR REPLACE FUNCTION ag_catalog.create_memory(p_type memory_type,p_content text,p_importance double precision DEFAULT 0.5,p_source_attribution jsonb DEFAULT NULL,p_trust_level double precision DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE content_trimmed text;
BEGIN
    content_trimmed := trim(p_content);

    -- Validate content early with context about memory type
    IF p_content IS NULL OR content_trimmed = '' THEN
        RAISE EXCEPTION 'create_memory(%) called with NULL or empty content', p_type;
    END IF;

    -- Quality gate for semantic memories
    IF p_type = 'semantic' THEN
        -- Reject very short content (less than 30 chars)
        IF LENGTH(content_trimmed) < 30 THEN
            RAISE EXCEPTION 'create_memory(semantic) rejected: content too short (% chars)', LENGTH(content_trimmed);
        END IF;

        -- Reject junk patterns
        IF content_trimmed ~* '^(unknown|none|null|\[\]|unanswerable|inquire shallow|insufficient information|no information|not available|not applicable)' THEN
            RAISE EXCEPTION 'create_memory(semantic) rejected: junk pattern detected';
        END IF;
    END IF;


RETURN public.create_memory(p_type,p_content,p_importance,p_source_attribution,p_trust_level);
END $$;

CREATE OR REPLACE FUNCTION public.create_episodic_memory(
    p_content TEXT,
    p_action_taken JSONB DEFAULT NULL,
    p_context JSONB DEFAULT NULL,
    p_result JSONB DEFAULT NULL,
    p_emotional_valence FLOAT DEFAULT 0.0,
    p_event_time TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    p_importance FLOAT DEFAULT 0.5,
    p_source_attribution JSONB DEFAULT NULL,
    p_trust_level FLOAT DEFAULT NULL
) RETURNS UUID
LANGUAGE sql SET search_path = public, ag_catalog AS $$
SELECT memory_id FROM public.mint_memory('episodic',p_content,p_importance,p_source_attribution,
 p_type_metadata=>jsonb_build_object('action_taken',p_action_taken,'context',p_context,'result',p_result,'emotional_valence',p_emotional_valence,'event_time',p_event_time),p_trust_level=>p_trust_level) $$;

CREATE OR REPLACE FUNCTION public.create_semantic_memory(
    p_content TEXT,
    p_confidence FLOAT,
    p_category TEXT[] DEFAULT NULL,
    p_related_concepts TEXT[] DEFAULT NULL,
    p_source_references JSONB DEFAULT NULL,
    p_importance FLOAT DEFAULT 0.5,
    p_source_attribution JSONB DEFAULT NULL,
    p_trust_level FLOAT DEFAULT NULL
) RETURNS UUID
LANGUAGE sql SET search_path = public, ag_catalog AS $$
SELECT memory_id FROM public.mint_memory('semantic',p_content,p_importance,p_source_attribution,
 p_type_metadata=>jsonb_build_object('confidence',p_confidence,'category',p_category,'related_concepts',p_related_concepts,'source_references',p_source_references),p_trust_level=>p_trust_level) $$;

CREATE OR REPLACE FUNCTION public.create_procedural_memory(
    p_content TEXT,
    p_steps JSONB,
    p_prerequisites JSONB DEFAULT NULL,
    p_importance FLOAT DEFAULT 0.5,
    p_source_attribution JSONB DEFAULT NULL,
    p_trust_level FLOAT DEFAULT NULL
) RETURNS UUID
LANGUAGE sql SET search_path = public, ag_catalog AS $$
SELECT memory_id FROM public.mint_memory('procedural',p_content,p_importance,p_source_attribution,
 p_type_metadata=>jsonb_build_object('steps',p_steps,'prerequisites',p_prerequisites),p_trust_level=>p_trust_level) $$;

CREATE OR REPLACE FUNCTION public.create_strategic_memory(
    p_content TEXT,
    p_pattern_description TEXT,
    p_confidence_score FLOAT,
    p_supporting_evidence JSONB DEFAULT NULL,
    p_context_applicability JSONB DEFAULT NULL,
    p_importance FLOAT DEFAULT 0.5,
    p_source_attribution JSONB DEFAULT NULL,
    p_trust_level FLOAT DEFAULT NULL
) RETURNS UUID
LANGUAGE sql SET search_path = public, ag_catalog AS $$
SELECT memory_id FROM public.mint_memory('strategic',p_content,p_importance,p_source_attribution,
 p_type_metadata=>jsonb_build_object('pattern_description',p_pattern_description,'confidence_score',p_confidence_score,'supporting_evidence',p_supporting_evidence,'context_applicability',p_context_applicability),p_trust_level=>p_trust_level) $$;

CREATE OR REPLACE FUNCTION public.batch_create_memories(p_items JSONB)
RETURNS UUID[] AS $$
DECLARE
    ids UUID[] := ARRAY[]::UUID[];
    item JSONB;
    mtype memory_type;
    content TEXT;
    importance FLOAT;
    new_id UUID;
    idx INT := 0;
BEGIN
    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' THEN
        RETURN ids;
    END IF;

    PERFORM pg_advisory_xact_lock(k) FROM (
        SELECT DISTINCT hashtextextended(md5(value->>'content'),10) k FROM jsonb_array_elements(p_items)
    ) locks ORDER BY k;
    FOR item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        idx := idx + 1;
        mtype := NULLIF(item->>'type', '')::memory_type;
        content := NULLIF(item->>'content', '');
        IF content IS NULL OR mtype IS NULL THEN
            RAISE EXCEPTION 'batch_create_memories: item % missing required fields', idx;
        END IF;
        importance := COALESCE(NULLIF(item->>'importance', '')::float, 0.5);

        IF mtype = 'episodic' THEN
            new_id := create_episodic_memory(
                content,
                item->'action_taken',
                item->'context',
                item->'result',
                COALESCE(NULLIF(item->>'emotional_valence', '')::float, 0.0),
                COALESCE(NULLIF(item->>'event_time', '')::timestamptz, CURRENT_TIMESTAMP),
                importance,
                item->'source_attribution',
                NULLIF(item->>'trust_level', '')::float
            );
        ELSIF mtype = 'semantic' THEN
            new_id := create_semantic_memory(
                content,
                COALESCE(NULLIF(item->>'confidence', '')::float, 0.8),
                CASE WHEN item ? 'category' THEN ARRAY(SELECT jsonb_array_elements_text(item->'category')) ELSE NULL END,
                CASE WHEN item ? 'related_concepts' THEN ARRAY(SELECT jsonb_array_elements_text(item->'related_concepts')) ELSE NULL END,
                item->'source_references',
                importance,
                item->'source_attribution',
                NULLIF(item->>'trust_level', '')::float
            );
        ELSIF mtype = 'procedural' THEN
            new_id := create_procedural_memory(
                content,
                COALESCE(item->'steps', jsonb_build_object('steps', '[]'::jsonb)),
                item->'prerequisites',
                importance,
                item->'source_attribution',
                NULLIF(item->>'trust_level', '')::float
            );
        ELSIF mtype = 'strategic' THEN
            new_id := create_strategic_memory(
                content,
                COALESCE(NULLIF(item->>'pattern_description', ''), content),
                COALESCE(NULLIF(item->>'confidence_score', '')::float, 0.8),
                item->'supporting_evidence',
                item->'context_applicability',
                importance,
                item->'source_attribution',
                NULLIF(item->>'trust_level', '')::float
            );
        ELSE
            RAISE EXCEPTION 'batch_create_memories: item % invalid type %', idx, mtype::text;
        END IF;

        IF new_id IS NULL THEN
            RAISE EXCEPTION 'batch_create_memories: item % failed to create memory', idx;
        END IF;
        ids := array_append(ids, new_id);
    END LOOP;

    RETURN ids;
END;
$$ LANGUAGE plpgsql SET search_path = public, ag_catalog;


-- Replace, rather than overload, the vector routes: existing arities resolve
-- through trailing defaults; there is exactly one callable definition per name.
DROP FUNCTION public.create_memory_with_embedding(memory_type,text,vector,double precision,jsonb,double precision);
CREATE FUNCTION public.create_memory_with_embedding(
 p_type memory_type,p_content text,p_embedding vector,p_importance double precision DEFAULT 0.5,
 p_source_attribution jsonb DEFAULT NULL,p_trust_level double precision DEFAULT NULL,
 p_embedding_profile text DEFAULT NULL
) RETURNS uuid LANGUAGE sql SET search_path = public, ag_catalog AS $$
 SELECT memory_id FROM public.mint_memory_with_embedding(p_type,p_content,p_embedding,p_importance,p_source_attribution,
 p_trust_level=>p_trust_level,p_embedding_profile=>p_embedding_profile) $$;

DROP FUNCTION public.batch_create_memories_with_embeddings(memory_type,text[],jsonb,double precision);
CREATE FUNCTION public.batch_create_memories_with_embeddings(
 p_type memory_type,p_contents text[],p_embeddings jsonb,p_importance double precision DEFAULT 0.5,
 p_embedding_profile text DEFAULT NULL
) RETURNS uuid[] LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE ids uuid[] := '{}'; vectors vector[] := '{}'; i int; n int; mid uuid; v vector;
BEGIN
 IF p_embedding_profile IS NOT NULL THEN
   RAISE EXCEPTION 'explicit embedding profiles require embedding regime migration/activation support' USING ERRCODE='22023';
 END IF;
 n:=COALESCE(cardinality(p_contents),0);
 IF p_embeddings IS NULL OR jsonb_typeof(p_embeddings)<>'array' THEN
   RAISE EXCEPTION 'embeddings must be a JSON array' USING ERRCODE='22023';
 END IF;
 IF jsonb_array_length(p_embeddings)<>n THEN
   RAISE EXCEPTION 'contents and embeddings length mismatch' USING ERRCODE='22023';
 END IF;
 -- Validate every vector before any base/subtype/graph/concept mutation.
 FOR i IN 1..n LOOP
   IF jsonb_typeof(p_embeddings->(i-1))<>'array' THEN
     RAISE EXCEPTION 'embedding % must be an array',i USING ERRCODE='22023';
   END IF;
   v:=(p_embeddings->(i-1))::text::vector;
   IF vector_dims(v)<>public.embedding_dimension() THEN
     RAISE EXCEPTION 'embedding dimension mismatch at item %',i USING ERRCODE='22023';
   END IF;
   vectors:=array_append(vectors,v);
 END LOOP;
 PERFORM pg_advisory_xact_lock(k) FROM (
   SELECT DISTINCT hashtextextended(md5(c),10) k FROM unnest(p_contents) c
 ) locks ORDER BY k;
 FOR i IN 1..n LOOP
   -- The established raw batch creates safe-default subtypes. Supply metadata
   -- at mint time so only newly inserted rows receive these defaults.
   SELECT memory_id INTO mid FROM public.mint_memory_with_embedding(p_type,p_contents[i],vectors[i],p_importance,
       p_type_metadata=>CASE p_type
           WHEN 'episodic' THEN jsonb_build_object('context',jsonb_build_object('type','raw_batch'),'emotional_valence',0.0)
           WHEN 'semantic' THEN jsonb_build_object('confidence',0.8,'source_references','[]'::jsonb)
           WHEN 'procedural' THEN jsonb_build_object('steps',jsonb_build_object('steps','[]'::jsonb))
           WHEN 'strategic' THEN jsonb_build_object('pattern_description',p_contents[i],'confidence_score',0.8)
       END,
       p_embedding_profile=>p_embedding_profile);
   ids:=array_append(ids,mid);
 END LOOP;
 RETURN ids;
END $$;

COMMIT;
