-- 016_update_importance_target.sql — sill original.
-- The baseline's update_memory_importance(uuid, double precision, text)
-- writes its audit row to ag_catalog.importance_updates. On a Sill install
-- the baseline creates that table in public, so every call failed with
-- "relation ag_catalog.importance_updates does not exist" and the MCP
-- update_importance tool never worked. Resolve the table through the
-- function's own search path, which finds it in either schema.
--
-- ROLLBACK: re-create the function from backend/schema.sql.
BEGIN;

CREATE OR REPLACE FUNCTION public.update_memory_importance(
  p_memory_id UUID,
  p_new_importance DOUBLE PRECISION,
  p_reason TEXT DEFAULT ''
) RETURNS BOOLEAN AS $$
DECLARE
  v_old_importance DOUBLE PRECISION;
BEGIN
  SELECT importance INTO v_old_importance
  FROM memories WHERE id = p_memory_id;

  IF v_old_importance IS NULL THEN
    RETURN FALSE;
  END IF;

  UPDATE memories SET importance = p_new_importance WHERE id = p_memory_id;

  INSERT INTO importance_updates (memory_id, old_importance, new_importance, reason)
  VALUES (p_memory_id, v_old_importance, p_new_importance, p_reason);

  RETURN TRUE;
END;
$$ LANGUAGE plpgsql SET search_path = public, ag_catalog;

COMMIT;
