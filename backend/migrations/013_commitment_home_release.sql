-- 013_commitment_home_release.sql
-- Ported from Sili migration 030 (2026-11-13 public adaptation).
-- Release ownership and single-home enforcement are sill original.
-- A commitment has one explicit home. Closing releases only that home's live
-- records; reopening restores only archives still owned by that release.
BEGIN;
SET LOCAL search_path = public, ag_catalog;

-- Abort before schema/data changes. The lock prevents a second home being
-- inserted between this diagnostic and installation of the unique index.
LOCK TABLE goal_memory_links IN ACCESS EXCLUSIVE MODE;
DO $$
DECLARE conflicts jsonb;
BEGIN
    SELECT jsonb_agg(to_jsonb(c)) INTO conflicts FROM (
        SELECT memory_id, array_agg(goal_id ORDER BY goal_id) AS goal_ids
        FROM goal_memory_links WHERE link_type='home'
        GROUP BY memory_id HAVING count(*)>1
    ) c;
    IF conflicts IS NOT NULL THEN
        RAISE EXCEPTION 'multiple commitment homes; resolve explicitly before upgrade: %', conflicts
            USING ERRCODE='55000';
    END IF;
END $$;

CREATE UNIQUE INDEX idx_commitment_one_home ON goal_memory_links(memory_id) WHERE link_type='home';
ALTER TABLE memories ADD COLUMN goal_release_owner uuid, ADD COLUMN goal_release_token uuid,
    ADD CONSTRAINT goal_release_pair CHECK ((goal_release_owner IS NULL)=(goal_release_token IS NULL));
COMMENT ON COLUMN memories.goal_release_token IS
    'Unique release receipt; an explicit later archive write revokes restoration ownership.';

CREATE FUNCTION public.guard_commitment_home() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE home_priority goal_priority;
BEGIN
    IF NEW.link_type <> 'home' THEN RETURN NEW; END IF;
    -- Lock the home before looking at its state, serializing with closure.
    SELECT priority INTO STRICT home_priority FROM goals WHERE id=NEW.goal_id FOR UPDATE;
    IF EXISTS (SELECT 1 FROM goal_memory_links
               WHERE goal_id=NEW.goal_id AND memory_id=NEW.memory_id AND link_type='home') THEN
        RETURN NEW;
    END IF;
    IF home_priority IN ('completed','abandoned') THEN
        RAISE EXCEPTION 'home goal % is closed; reopen it before linking a new commitment', NEW.goal_id
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_guard_commitment_home BEFORE INSERT OR UPDATE ON goal_memory_links
FOR EACH ROW EXECUTE FUNCTION public.guard_commitment_home();

CREATE FUNCTION public.link_commitment(p_memory_id uuid,p_goal_id uuid) RETURNS void
LANGUAGE sql SET search_path = public, ag_catalog AS $$
    INSERT INTO goal_memory_links(memory_id,goal_id,link_type) VALUES(p_memory_id,p_goal_id,'home')
    ON CONFLICT (goal_id,memory_id,link_type) DO NOTHING
$$;

CREATE FUNCTION public.revoke_goal_release() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
BEGIN
    -- A later manual archive operation owns the decision even if now() or the
    -- text happens to equal the release's values. Release itself supplies a new
    -- token, while restoration explicitly clears it. Never infer from prose.
    IF OLD.goal_release_token IS NOT NULL
       AND NEW.goal_release_token IS NOT DISTINCT FROM OLD.goal_release_token THEN
        NEW.goal_release_owner:=NULL;
        NEW.goal_release_token:=NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_revoke_goal_release
BEFORE UPDATE OF archived_at,archive_reason,status,supersedes_id ON memories
FOR EACH ROW EXECUTE FUNCTION public.revoke_goal_release();

CREATE FUNCTION public.release_commitments_at_home_close() RETURNS trigger
LANGUAGE plpgsql SET search_path = public, ag_catalog AS $$
DECLARE
    was_closed boolean := COALESCE(OLD.priority IN ('completed','abandoned'),false);
    is_closed boolean := COALESCE(NEW.priority IN ('completed','abandoned'),false);
    release_token uuid := gen_random_uuid();
BEGIN
    IF is_closed AND NOT was_closed THEN
        UPDATE memories m SET archived_at=CURRENT_TIMESTAMP,
            archive_reason=format('released: home goal %s %s',NEW.id,NEW.priority)
                || COALESCE(' — '||NEW.abandonment_reason,''),
            goal_release_owner=NEW.id,goal_release_token=release_token
        FROM goal_memory_links l
        WHERE l.goal_id=NEW.id AND l.link_type='home' AND l.memory_id=m.id
            AND m.archived_at IS NULL AND m.status='active';
    ELSIF was_closed AND NOT is_closed THEN
        UPDATE memories m SET archived_at=NULL,archive_reason=NULL,
            goal_release_owner=NULL,goal_release_token=NULL
        FROM goal_memory_links l
        WHERE l.goal_id=NEW.id AND l.link_type='home' AND l.memory_id=m.id
            AND m.goal_release_owner=NEW.id AND m.goal_release_token IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM memories successor WHERE successor.supersedes_id=m.id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_release_commitments_at_home_close
AFTER UPDATE OF priority,completed_at,abandoned_at ON goals
FOR EACH ROW EXECUTE FUNCTION public.release_commitments_at_home_close();
COMMIT;
