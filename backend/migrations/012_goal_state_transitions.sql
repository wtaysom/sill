-- 012_goal_state_transitions.sql
-- Ported from Sili migration 029 (2026-11-13 public adaptation).
-- Enforce terminal transitions at the shared table boundary, including raw SQL.
-- This migration does not backfill historical dates or normalize historical rows.
-- Existing completed/abandoned rows with unknown dates remain unknown on touches.
BEGIN;
SET LOCAL search_path = public, ag_catalog;
CREATE OR REPLACE FUNCTION sync_goal_state() RETURNS trigger LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE priority_changed boolean; completion_changed boolean; abandonment_changed boolean;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.completed_at IS NOT NULL AND NEW.abandoned_at IS NOT NULL THEN
            RAISE EXCEPTION 'a goal cannot be completed and abandoned' USING ERRCODE='23514';
        ELSIF NEW.completed_at IS NOT NULL THEN
            NEW.priority:='completed';
        ELSIF NEW.abandoned_at IS NOT NULL THEN
            NEW.priority:='abandoned';
        ELSIF NEW.priority='completed' THEN
            NEW.completed_at:=CURRENT_TIMESTAMP;
        ELSIF NEW.priority='abandoned' THEN
            NEW.abandoned_at:=CURRENT_TIMESTAMP;
        END IF;
        RETURN NEW;
    END IF;
    priority_changed:=NEW.priority IS DISTINCT FROM OLD.priority;
    completion_changed:=NEW.completed_at IS DISTINCT FROM OLD.completed_at;
    abandonment_changed:=NEW.abandoned_at IS DISTINCT FROM OLD.abandoned_at;
    IF completion_changed AND abandonment_changed AND NEW.completed_at IS NOT NULL AND NEW.abandoned_at IS NOT NULL THEN
        RAISE EXCEPTION 'a goal cannot be completed and abandoned' USING ERRCODE='23514';
    END IF;
    IF priority_changed THEN
        IF NEW.priority='completed' THEN
            IF abandonment_changed AND NEW.abandoned_at IS NOT NULL THEN
                RAISE EXCEPTION 'completed priority conflicts with abandonment timestamp' USING ERRCODE='23514';
            END IF;
            NEW.completed_at:=COALESCE(NEW.completed_at,CURRENT_TIMESTAMP);
            NEW.abandoned_at:=NULL;
            NEW.abandonment_reason:=NULL;
        ELSIF NEW.priority='abandoned' THEN
            IF completion_changed AND NEW.completed_at IS NOT NULL THEN
                RAISE EXCEPTION 'abandoned priority conflicts with completion timestamp' USING ERRCODE='23514';
            END IF;
            NEW.abandoned_at:=COALESCE(NEW.abandoned_at,CURRENT_TIMESTAMP);
            NEW.completed_at:=NULL;
        ELSE
            IF (completion_changed AND NEW.completed_at IS NOT NULL) OR (abandonment_changed AND NEW.abandoned_at IS NOT NULL) THEN
                RAISE EXCEPTION 'open priority conflicts with terminal timestamp' USING ERRCODE='23514';
            END IF;
            NEW.completed_at:=NULL;
            NEW.abandoned_at:=NULL;
            NEW.abandonment_reason:=NULL;
        END IF;
    ELSIF completion_changed OR abandonment_changed THEN
        -- Inspect the resulting pair, not which column changed first: a caller
        -- can clear one terminal date and set the other in the same UPDATE.
        IF completion_changed AND NEW.completed_at IS NOT NULL THEN
            NEW.priority:='completed'; NEW.abandoned_at:=NULL; NEW.abandonment_reason:=NULL;
        ELSIF abandonment_changed AND NEW.abandoned_at IS NOT NULL THEN
            NEW.priority:='abandoned'; NEW.completed_at:=NULL;
        ELSIF NEW.completed_at IS NOT NULL THEN
            NEW.priority:='completed'; NEW.abandonment_reason:=NULL;
        ELSIF NEW.abandoned_at IS NOT NULL THEN
            NEW.priority:='abandoned';
        ELSIF NEW.priority IN ('completed','abandoned') THEN
            NEW.priority:='queued'; NEW.abandonment_reason:=NULL;
        END IF;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_goal_state_sync ON goals;
CREATE TRIGGER trg_goal_state_sync BEFORE INSERT OR UPDATE OF priority,completed_at,abandoned_at ON goals
FOR EACH ROW EXECUTE FUNCTION sync_goal_state();

CREATE OR REPLACE FUNCTION public.change_goal_priority(p_goal_id UUID,p_new_priority goal_priority,p_reason TEXT DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE old_priority goal_priority;
BEGIN
    SELECT priority INTO STRICT old_priority FROM goals WHERE id=p_goal_id FOR UPDATE;
    -- The trigger handles dates only when a state actually changes. In particular,
    -- repeating 'completed' must not invent a date for a legacy unknown completion.
    UPDATE goals SET priority=p_new_priority,last_touched=CURRENT_TIMESTAMP,
        abandonment_reason=CASE WHEN p_new_priority='abandoned' THEN p_reason ELSE abandonment_reason END
    WHERE id=p_goal_id;
    PERFORM add_goal_progress(p_goal_id,format('Priority changed from %s to %s%s',old_priority,p_new_priority,
        CASE WHEN p_reason IS NOT NULL THEN ': '||p_reason ELSE '' END));
END $$;
-- The frozen baseline CASE resolves to text, so goal creation fails even on
-- first insert. Preserve creation/deduplication policy and cast its result.
CREATE OR REPLACE FUNCTION public.create_goal(
    p_title TEXT,
    p_description TEXT DEFAULT NULL,
    p_source goal_source DEFAULT 'curiosity',
    p_priority goal_priority DEFAULT 'queued',
    p_parent_id UUID DEFAULT NULL,
    p_due_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS UUID AS $$
DECLARE
    new_goal_id UUID;
    active_count INT;
    max_active INT;
    queued_count INT;
    max_queued INT;
BEGIN
    -- Check active goal limit if trying to create as active
    IF p_priority = 'active' THEN
        SELECT COUNT(*) INTO active_count FROM goals WHERE priority = 'active';
        SELECT value INTO max_active FROM heartbeat_config WHERE key = 'max_active_goals';

        IF active_count >= max_active THEN
            p_priority := 'queued';  -- Demote to queued if at limit
        END IF;
    END IF;

    -- Check queued goal limit if trying to create as queued
    IF p_priority = 'queued' THEN
        SELECT COUNT(*) INTO queued_count FROM goals WHERE priority = 'queued';
        SELECT value INTO max_queued FROM heartbeat_config WHERE key = 'max_queued_goals';
        max_queued := COALESCE(max_queued, 50);

        IF queued_count >= max_queued THEN
            p_priority := 'backburner';
        END IF;
    END IF;

    -- Idempotent insert: avoid duplicated open goals (active/queued/backburner) by normalized title.
    INSERT INTO goals (title, description, source, priority, parent_goal_id, due_at)
    VALUES (p_title, p_description, p_source, p_priority, p_parent_id, p_due_at)
    ON CONFLICT (dedupe_key) WHERE priority IN ('active', 'queued', 'backburner')
    DO UPDATE SET
        description = COALESCE(NULLIF(btrim(goals.description), ''), EXCLUDED.description),
        due_at = COALESCE(goals.due_at, EXCLUDED.due_at),
        parent_goal_id = COALESCE(goals.parent_goal_id, EXCLUDED.parent_goal_id),
        last_touched = CURRENT_TIMESTAMP,
        priority = CASE
            WHEN goals.priority = 'active' OR EXCLUDED.priority = 'active' THEN 'active'
            WHEN goals.priority = 'queued' OR EXCLUDED.priority = 'queued' THEN 'queued'
            ELSE 'backburner'
        END::goal_priority
    RETURNING id INTO new_goal_id;

    RETURN new_goal_id;
END;
$$ LANGUAGE plpgsql SET search_path = public, ag_catalog;

-- Preserve the legacy AGE-first callable when the baseline installed it there.
DO $migration$
BEGIN
    IF to_regprocedure('ag_catalog.change_goal_priority(uuid,goal_priority,text)') IS NOT NULL THEN
        EXECUTE $wrapper$CREATE OR REPLACE FUNCTION ag_catalog.change_goal_priority(
            p_goal_id UUID,p_new_priority goal_priority,p_reason TEXT DEFAULT NULL)
        RETURNS VOID LANGUAGE sql SET search_path = public, ag_catalog AS
        'SELECT public.change_goal_priority(p_goal_id,p_new_priority,p_reason)'$wrapper$;
    END IF;
    IF to_regprocedure('ag_catalog.create_goal(text,text,goal_source,goal_priority,uuid,timestamp with time zone)') IS NOT NULL THEN
        EXECUTE $wrapper$CREATE OR REPLACE FUNCTION ag_catalog.create_goal(
            p_title TEXT,p_description TEXT DEFAULT NULL,p_source goal_source DEFAULT 'curiosity',
            p_priority goal_priority DEFAULT 'queued',p_parent_id UUID DEFAULT NULL,p_due_at TIMESTAMPTZ DEFAULT NULL)
        RETURNS UUID LANGUAGE sql SET search_path = public, ag_catalog AS
        'SELECT public.create_goal(p_title,p_description,p_source,p_priority,p_parent_id,p_due_at)'$wrapper$;
    END IF;
END $migration$;
COMMIT;
