-- 016_fix_get_embedding_backslash.sql
-- Fix: get_embedding hashed the cache key with text_content::bytea, which
-- parses backslash escapes and rejects any content containing a backslash
-- ('invalid input syntax for type bytea'). Use convert_to(...,'UTF8') instead.
-- Safe for the cache: non-backslash content hashes identically.

CREATE OR REPLACE FUNCTION public.get_embedding(text_content TEXT)
RETURNS vector AS $$
	DECLARE
	    service_url TEXT;
	    response http_response;
	    request_body TEXT;
	    embedding_array FLOAT[];
	    embedding_json JSONB;
	    v_content_hash TEXT;
	    cached_embedding vector;
	    expected_dim INT;
	    start_ts TIMESTAMPTZ;
	    retry_seconds INT;
	    retry_interval_seconds FLOAT;
	    last_error TEXT;
	BEGIN
	    -- Validate input early to give clear error message
	    IF text_content IS NULL OR trim(text_content) = '' THEN
	        RAISE EXCEPTION 'Cannot generate embedding for NULL or empty content';
	    END IF;

	    PERFORM sync_embedding_dimension_config();
	    expected_dim := embedding_dimension();

	    -- Generate hash for caching.
	    -- Use convert_to(...,'UTF8'), NOT ::bytea: casting text to bytea parses
	    -- backslash escape sequences, so any content with a '\' that isn't a valid
	    -- bytea escape throws 'invalid input syntax for type bytea' (e.g. code,
	    -- regexes, LaTeX, Windows paths). convert_to takes the literal UTF-8 bytes.
	    v_content_hash := encode(sha256(convert_to(text_content, 'UTF8')), 'hex');

    -- Check cache first
    SELECT ec.embedding INTO cached_embedding
    FROM embedding_cache ec
    WHERE ec.content_hash = v_content_hash;

    IF FOUND THEN
        RETURN cached_embedding;
    END IF;

    -- Get service URL
	    SELECT value INTO service_url FROM embedding_config WHERE key = 'service_url';

	    -- Prepare request body
	    request_body := json_build_object('inputs', text_content)::TEXT;

	    -- Make HTTP request (with retries to tolerate a slow-starting embedding service).
	    retry_seconds := COALESCE(NULLIF((SELECT value FROM embedding_config WHERE key = 'retry_seconds'), '')::int, 30);
	    retry_interval_seconds := COALESCE(NULLIF((SELECT value FROM embedding_config WHERE key = 'retry_interval_seconds'), '')::float, 1.0);
	    start_ts := clock_timestamp();

	    -- Lift curl's default ~5s timeout. Long content (>5 KB) embedded under CPU load
	    -- can take 6-10s; the default makes http_post fail before the embedding returns,
	    -- breaking every retry in the loop below. 60s is generous; actual requests are fast.
	    -- Session-scoped; re-set every call to survive connection pool recycling.
	    PERFORM http_set_curlopt('CURLOPT_TIMEOUT_MS', '60000');

	    LOOP
	        BEGIN
	            SELECT * INTO response FROM http_post(
	                service_url,
	                request_body,
	                'application/json'
	            );

	            IF response.status = 200 THEN
	                EXIT;
	            END IF;

	            -- Non-retriable statuses (bad request, auth, etc).
	            IF response.status IN (400, 401, 403, 404, 422) THEN
	                RAISE EXCEPTION 'Embedding service error: % - %', response.status, response.content;
	            END IF;

	            last_error := format('status %s: %s', response.status, left(COALESCE(response.content, ''), 500));
	        EXCEPTION
	            WHEN OTHERS THEN
	                last_error := SQLERRM;
	        END;

	        IF retry_seconds <= 0 OR clock_timestamp() - start_ts >= (retry_seconds || ' seconds')::interval THEN
	            RAISE EXCEPTION 'Embedding service not available after % seconds: %', retry_seconds, COALESCE(last_error, '<unknown>');
	        END IF;

	        PERFORM pg_sleep(GREATEST(0.0, retry_interval_seconds));
	    END LOOP;

	    -- Parse response
	    embedding_json := response.content::JSONB;

    -- Extract embedding array (handle different response formats)
    IF embedding_json ? 'embeddings' THEN
        -- Format: {"embeddings": [[...]]}
        embedding_array := ARRAY(
            SELECT jsonb_array_elements_text((embedding_json->'embeddings')->0)::FLOAT
        );
    ELSIF embedding_json ? 'embedding' THEN
        -- Format: {"embedding": [...]}
        embedding_array := ARRAY(
            SELECT jsonb_array_elements_text(embedding_json->'embedding')::FLOAT
        );
    ELSIF embedding_json ? 'data' THEN
        -- OpenAI format: {"data": [{"embedding": [...]}]}
        embedding_array := ARRAY(
            SELECT jsonb_array_elements_text((embedding_json->'data')->0->'embedding')::FLOAT
        );
    ELSIF jsonb_typeof(embedding_json->0) = 'array' THEN
        -- HuggingFace TEI format: [[...]] (array of arrays)
        embedding_array := ARRAY(
            SELECT jsonb_array_elements_text(embedding_json->0)::FLOAT
        );
    ELSE
        -- Flat array format: [...]
        embedding_array := ARRAY(
            SELECT jsonb_array_elements_text(embedding_json)::FLOAT
        );
	    END IF;
	
	    -- Validate embedding size
	    IF array_length(embedding_array, 1) IS NULL OR array_length(embedding_array, 1) != expected_dim THEN
	        RAISE EXCEPTION 'Invalid embedding dimension: expected %, got %', expected_dim, array_length(embedding_array, 1);
	    END IF;
	
	    -- Cache the result
	    INSERT INTO embedding_cache (content_hash, embedding)
	    VALUES (v_content_hash, embedding_array::vector)
	    ON CONFLICT DO NOTHING;
	
	    RETURN embedding_array::vector;
	EXCEPTION
	    WHEN OTHERS THEN
	        RAISE EXCEPTION 'Failed to get embedding: %', SQLERRM;
	END;
$$ LANGUAGE plpgsql;
