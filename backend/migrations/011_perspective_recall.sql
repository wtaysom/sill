-- sill original: dedicated perspective variant of the baseline RRF function.
-- 011: Filter perspective before vector/FTS candidates; preserve rank-fusion k=60.
BEGIN;
CREATE OR REPLACE FUNCTION public.hybrid_recall_perspective(
    p_query_text TEXT,
    p_limit INT,
    p_speaker TEXT,
    p_k INT DEFAULT 60
)
RETURNS TABLE(
    memory_id UUID,
    content TEXT,
    memory_type memory_type,
    score DOUBLE PRECISION,
    source TEXT
)
LANGUAGE plpgsql SET search_path = public, ag_catalog
AS $$
DECLARE
    query_embedding vector;
    zero_vec vector;
    ts_query tsquery;
BEGIN
    query_embedding := public.get_embedding(p_query_text);
    zero_vec := array_fill(0.0::float, ARRAY[embedding_dimension()])::vector;
    ts_query := plainto_tsquery('english', p_query_text);

    RETURN QUERY
    WITH
    vector_ranked AS (
        SELECT
            m.id,
            m.content,
            m.type,
            ROW_NUMBER() OVER (ORDER BY m.embedding <=> query_embedding) AS rank
        FROM memories m
        WHERE m.status = 'active' AND m.archived_at IS NULL
          AND (p_speaker IS NULL OR
               (p_speaker = '#Nobody' AND m.speaker IS NULL) OR
               (p_speaker <> '#Nobody' AND m.speaker = p_speaker))
          AND m.embedding IS NOT NULL
          AND m.embedding <> zero_vec
        ORDER BY m.embedding <=> query_embedding
        LIMIT p_limit * 4
    ),
    fts_ranked AS (
        SELECT
            m.id,
            m.content,
            m.type,
            ROW_NUMBER() OVER (ORDER BY ts_rank(m.content_tsv, ts_query) DESC) AS rank
        FROM memories m
        WHERE m.status = 'active' AND m.archived_at IS NULL
          AND (p_speaker IS NULL OR
               (p_speaker = '#Nobody' AND m.speaker IS NULL) OR
               (p_speaker <> '#Nobody' AND m.speaker = p_speaker))
          AND m.content_tsv @@ ts_query
        ORDER BY ts_rank(m.content_tsv, ts_query) DESC
        LIMIT p_limit * 4
    ),
    combined AS (
        SELECT
            COALESCE(v.id, f.id) AS id,
            COALESCE(v.content, f.content) AS content,
            COALESCE(v.type, f.type) AS type,
            (COALESCE(1.0/(p_k + v.rank), 0) + COALESCE(1.0/(p_k + f.rank), 0))::double precision AS rrf_score,
            CASE
                WHEN v.id IS NOT NULL AND f.id IS NOT NULL THEN 'hybrid'
                WHEN v.id IS NOT NULL THEN 'vector'
                ELSE 'fts'
            END AS match_source
        FROM vector_ranked v
        FULL OUTER JOIN fts_ranked f ON v.id = f.id
    )
    SELECT
        c.id,
        c.content,
        c.type,
        c.rrf_score,
        c.match_source
    FROM combined c
    ORDER BY c.rrf_score DESC
    LIMIT p_limit;
END;
$$;

CREATE OR REPLACE FUNCTION ag_catalog.hybrid_recall_perspective(
    p_query_text text, p_limit int, p_speaker text, p_k int DEFAULT 60
) RETURNS TABLE(memory_id uuid, content text, memory_type public.memory_type,
                score double precision, source text)
LANGUAGE sql SET search_path = public, ag_catalog AS $$
    SELECT * FROM public.hybrid_recall_perspective(p_query_text, p_limit, p_speaker, p_k)
$$;
COMMIT;
