-- 008_supersession_metadata.sql
-- Ported from Sili migration 028 (2026-11-13 public adaptation).
-- Historical attribution stays historical; a successor declares its own voice,
-- current type facts and validation. Concepts inherit through gaps, until an
-- explicit clearing. Public is the measured legacy namespace: the three-argument
-- overload forwards to the one four-argument implementation.

BEGIN;
SET LOCAL search_path = public, ag_catalog;

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
BEGIN
    p_metadata:=COALESCE(NULLIF(p_metadata,'null'::jsonb),'{}'::jsonb);
    IF p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
       OR EXISTS (SELECT 1 FROM jsonb_object_keys(p_metadata) k
                  WHERE k NOT IN ('speaker','force','source_attribution','concepts','type_metadata')) THEN
        RAISE EXCEPTION 'supersession metadata must be an object with supported keys'
            USING ERRCODE = '22023';
    END IF;
    IF p_new_content IS NULL OR btrim(p_new_content) = '' THEN
        RAISE EXCEPTION 'replacement content must not be empty' USING ERRCODE = '22023';
    END IF;
    -- Serialize contenders: after waiting, the losing caller sees archived state.
    SELECT * INTO old_mem FROM memories WHERE id=p_old_id FOR UPDATE;
    IF NOT FOUND OR old_mem.status <> 'active' OR old_mem.archived_at IS NOT NULL THEN
        RAISE EXCEPTION 'Memory % not found or already archived',p_old_id;
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
        'trust_updated_at',old_mem.trust_updated_at));
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
                                to_jsonb(pm)-'memory_id',to_jsonb(stm)-'memory_id')) ORDER BY a.depth),'[]'::jsonb)
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
                         supersedes_id,derived_from,embedding)
    VALUES(p_new_content,old_mem.type,old_mem.importance,old_mem.decay_rate,
           p_metadata->>'speaker',p_metadata->>'force',current_source,p_old_id,
           jsonb_build_object('premises',jsonb_build_array(p_old_id::text),'reason',COALESCE(p_reason,'supersession')),
           public.get_embedding(p_new_content)) RETURNING id INTO new_id;

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
COMMIT;
