"""Actual initdb + data-bearing tagged upgrades, without rewriting SQL bodies."""
from contextlib import closing
import json
import os
import re
from pathlib import Path

import psycopg2
import pytest

from schema_fixture import MANIFEST, ROOT, dump, routes, sql, upgrade
from test_function_contracts import catalog, contract_only


def query(conn, statement, args=()):
    with conn.cursor() as cur:
        cur.execute(statement, args)
        return cur.fetchall() if cur.description else []


def ledger(conn):
    return query(conn, "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                 "WHERE c.relname='schema_migrations' AND c.relkind IN ('r','p') ORDER BY 1")


def seed_history(conn):
    mid = query(conn, "INSERT INTO public.memories(type,content,embedding,importance,source_attribution,created_at) "
        "VALUES('semantic','Synthetic historical charter observation',array_fill(0.01::real,ARRAY[768])::vector,"
        "0.42,'{\"ref\":\"fixture:charter\",\"speaker\":\"Synthetic historian\"}', '2001-02-03T04:05:06Z') RETURNING id")[0][0]
    query(conn, 'INSERT INTO public.semantic_memories(memory_id,confidence) VALUES(%s,0.73)', (mid,))
    iid = query(conn, "INSERT INTO public.identity_aspects(aspect_type,content,stability,created_at,updated_at) "
        "VALUES('purpose','{\"name\":\"Synthetic identity\",\"charter_path\":\"fixture:charter.md\"}',"
        "0.64,'2001-02-03T04:05:06Z','2001-02-03T04:05:06Z') RETURNING id")[0][0]
    gid = query(conn, "INSERT INTO public.goals(title,priority,created_at,last_touched) "
        "VALUES('Synthetic historical goal','active','2001-02-03T04:05:06Z','2001-02-03T04:05:06Z') RETURNING id")[0][0]
    ids = {'memories': mid, 'identity_aspects': iid, 'goals': gid}
    before = {table: query(conn, f'SELECT to_jsonb(t) FROM public.{table} t WHERE id=%s', (value,))[0][0]
              for table, value in ids.items()}
    before['semantic_memories'] = query(conn, 'SELECT to_jsonb(t) FROM public.semantic_memories t WHERE memory_id=%s', (mid,))[0][0]
    before['rrf'] = query(conn, "SELECT * FROM hybrid_recall('Synthetic historical charter',5)")
    conn.commit()
    return ids, before


@pytest.fixture(scope='module')
def schema_routes(tmp_path_factory):
    if not all(os.environ.get(k) for k in ('SILL_SCHEMA_IMAGE', 'SILL_SCHEMA_HTTP_IMAGE')):
        if os.environ.get('SILL_SCHEMA_REQUIRED') == '1':
            pytest.fail('required schema gate needs SILL_SCHEMA_IMAGE and SILL_SCHEMA_HTTP_IMAGE')
        pytest.skip('explicit Docker schema fixture images not configured')
    directory = tmp_path_factory.mktemp('schema-contract')
    # pytest parents can be 0700; Docker postgres must traverse this fixture path.
    directory.parent.chmod(0o755)
    with routes(directory) as stores:
        for lane, store in stores.items():
            store['init_log'] = (directory / (lane + '-init.log')).read_text()
            with closing(psycopg2.connect(store['dsn'])) as conn:
                store['ledger_before'] = ledger(conn)
                if lane != 'fresh':
                    store['history'] = seed_history(conn)
            if lane != 'fresh':
                (directory / (lane + '-upgrade.log')).write_text(upgrade(store['container'], directory))
            store['dump'] = dump(store['container'], directory / (lane + '-raw-schema.sql'))
            (directory / (lane + '-schema.sql')).write_text(store['dump'])
        manifest = json.loads(MANIFEST.read_text())
        for lane, store in stores.items():
            with closing(psycopg2.connect(store['dsn'])) as conn:
                measured = catalog(conn, manifest['release_function_names'])
                (directory / (lane + '-functions.json')).write_text(json.dumps(measured, indent=2))
        yield stores


def test_fresh_and_data_bearing_upgrade_match_functions(schema_routes):
    manifest = json.loads(MANIFEST.read_text())
    snapshots = {}
    for lane, store in schema_routes.items():
        with closing(psycopg2.connect(store['dsn'])) as conn:
            assert query(conn, 'SHOW search_path') == [('ag_catalog,public',)]
            assert ledger(conn) == [('ag_catalog',)], (lane, ledger(conn))
            assert query(conn, 'SELECT migration_id,source FROM schema_migrations ORDER BY 1') == [
                (f'{n:03}', 'initdb' if lane == 'fresh' or lane == 'from02' and n <= 7 else 'upgrade.sh')
                for n in range(1,16)]
            assert store['ledger_before'] == ([] if lane == 'from01' else [('ag_catalog',)])
            snapshots[lane] = catalog(conn, manifest['release_function_names'])
    for lane in ('from01','from02'):
        assert schema_routes[lane]['dump'] == schema_routes['fresh']['dump'], lane
        assert snapshots[lane] == snapshots['fresh'], lane
    assert contract_only(snapshots['fresh']) == manifest['v03_release']


def test_upgrade_preserves_unrelated_user_data(schema_routes):
    for lane in ('from01','from02'):
        store = schema_routes[lane]
        ids, before = store['history']
        with closing(psycopg2.connect(store['dsn'])) as conn:
            for table, value in ids.items():
                after = query(conn, f'SELECT to_jsonb(t) FROM public.{table} t WHERE id=%s', (value,))[0][0]
                assert {key: after[key] for key in before[table]} == before[table], (lane, table)
            assert query(conn, 'SELECT to_jsonb(t) FROM public.semantic_memories t WHERE memory_id=%s', (ids['memories'],))[0][0] == before['semantic_memories']
            assert query(conn, "SELECT * FROM hybrid_recall('Synthetic historical charter',5)") == before['rrf']
            assert query(conn, 'SELECT production,minted_in,importance FROM public.memories WHERE id=%s', (ids['memories'],)) == [(None,None,0.42)]


@pytest.mark.parametrize('lane', ['fresh','from01','from02'])
def test_legacy_and_current_overloads_under_production_path(schema_routes, lane):
    with closing(psycopg2.connect(schema_routes[lane]['dsn'])) as conn:
        anchors = []
        for namespace in ('', 'public.', 'ag_catalog.'):
            mid = query(conn, f"SELECT {namespace}create_memory('semantic',%s)",
                        ('Synthetic release callable ' + namespace + lane,))[0][0]
            anchors.append(mid)
            assert query(conn, 'SELECT production FROM memories WHERE id=%s', (mid,)) == [(None,)]
            assert query(conn, f"SELECT * FROM {namespace}hybrid_recall_perspective(%s,5,NULL)", ('Synthetic',))
        for function in ('get_embedding', 'safe_get_embedding'):
            assert query(conn, f"SELECT vector_dims({function}('Synthetic one argument'))") == [(768,)]
            assert query(conn, f"SELECT vector_dims({function}('Synthetic two arguments','query'))") == [(768,)]
        # Existing 3/4-default ambiguity: named query_text selects the legacy
        # three-argument signature; explicit fourth argument selects current.
        query(conn, "SELECT * FROM search_similar_memories(query_text=>'Synthetic',result_limit=>5,type_filter=>NULL)")
        query(conn, "SELECT * FROM search_similar_memories('Synthetic',5,NULL,0.0)")
        for namespace in ('','public.'):
            for extended in (False,True):
                old = query(conn, "SELECT create_memory('semantic',%s)",
                            ('Synthetic historical predecessor for release contract ' + namespace + str(extended),))[0][0]
                args = (old, 'Synthetic replacement successor for release contract ' + namespace + str(extended), 'fixture reason')
                placeholders = '%s,%s,%s'
                if extended:
                    placeholders += ',%s::jsonb'
                    args += ('{"speaker":"Synthetic successor"}',)
                successor = query(conn, f'SELECT {namespace}supersede_memory({placeholders})', args)[0][0]
                assert query(conn, 'SELECT supersedes_id FROM memories WHERE id=%s', (successor,)) == [(old,)]
        mid = query(conn, "SELECT create_memory_with_embedding('semantic','Synthetic supplied vector',array_fill(0.01::real,ARRAY[768])::vector)")[0][0]
        prior = query(conn, 'SELECT access_count,importance FROM memories WHERE id=%s', (mid,))[0]
        query(conn, 'SELECT touch_memory_access(%s)', (mid,))
        assert query(conn, 'SELECT access_count,importance FROM memories WHERE id=%s', (mid,)) == [(prior[0]+1,prior[1])]
        query(conn, "SELECT record_memory_reuse(%s,'fixture','memory_id',NULL,'fixture-session','{}')", (mid,))
        assert query(conn, 'SELECT count(*) FROM memory_reuse_events WHERE memory_id=%s', (mid,)) == [(1,)]
        gid = query(conn, "SELECT create_goal('Synthetic new goal')")[0][0]
        query(conn, 'SELECT link_commitment(%s,%s)', (mid,gid))
        query(conn, "SELECT change_goal_priority(%s,'completed')", (gid,))
        assert query(conn, 'SELECT archived_at IS NOT NULL FROM memories WHERE id=%s', (mid,)) == [(True,)]
        # Exercise query call sites changed in-place by 014/015, plus the
        # public touch overload hidden by AGE on the production search_path.
        query(conn, 'SELECT public.touch_memory_access(%s)', (mid,))
        for expression in (
            "fast_recall('Synthetic',5)", "fast_recall_hybrid('Synthetic',5)",
            "hybrid_recall('Synthetic',5)", "search_working_memory('Synthetic',5)",
            "find_partial_activations('Synthetic')", "check_boundaries('Synthetic')",
            "embedding_profile_fingerprint(NULL)", "embedding_source_hash('Synthetic','query')",
            "check_supplied_embedding_profile(NULL)",
            "mint_memory('semantic','Synthetic explicit mint memory call')",
            "mint_memory_with_embedding('semantic','Synthetic explicit vector mint call',array_fill(0.01::real,ARRAY[768])::vector)",
            "mint_memories('[{\"type\":\"semantic\",\"content\":\"Synthetic batch mint memory call\"}]'::jsonb)",
            "batch_create_memories('[{\"type\":\"semantic\",\"content\":\"Synthetic legacy batch memory call\"}]'::jsonb)",
            "batch_create_memories_with_embeddings('semantic',ARRAY['Synthetic precomputed batch memory call'],jsonb_build_array(to_jsonb(array_fill(0.01::real,ARRAY[768]))))",
            "create_semantic_memory('Synthetic semantic subtype constructor call',0.73)",
            "create_episodic_memory('Synthetic episodic subtype constructor call')",
            "create_procedural_memory('Synthetic procedural subtype constructor call','[]'::jsonb)",
            "create_strategic_memory('Synthetic strategic subtype constructor call','Synthetic pattern',0.73)",
        ):
            query(conn, 'SELECT * FROM ' + expression)
        query(conn, "SELECT discover_relationship(%s,%s,'ASSOCIATED')", (anchors[0],anchors[1]))
        query(conn, 'SELECT recalculate_cluster_centroid(%s)', (mid,))
        query(conn, 'SELECT assign_memory_to_clusters(%s)', (mid,))
        query(conn, 'SELECT recompute_neighborhood(%s)', (mid,))
        query(conn, 'SELECT batch_recompute_neighborhoods(5)')
        # Retain the previous CI job's real session-activity upsert smoke.
        upsert = """INSERT INTO session_activity(project,session_id,last_prompt_at,session_started_at,prompt_count)
            VALUES('m7-fixture','m7-session',now(),now(),1)
            ON CONFLICT(project) DO UPDATE SET
              session_id=EXCLUDED.session_id,last_prompt_at=EXCLUDED.last_prompt_at,
              session_started_at=CASE WHEN session_activity.session_id=EXCLUDED.session_id
                THEN session_activity.session_started_at ELSE EXCLUDED.session_started_at END,
              prompt_count=CASE WHEN session_activity.session_id=EXCLUDED.session_id
                THEN session_activity.prompt_count+1 ELSE 1 END
            RETURNING prompt_count,session_started_at"""
        first = query(conn, upsert)[0]
        assert first[0] == 1 and query(conn, upsert) == [(2,first[1])]
        conn.rollback()


def test_ambiguous_duplicate_ledger_is_rejected(schema_routes):
    from scripts.migrate import inspect_state
    store = schema_routes['fresh']
    class ActualDatabase:
        def query(self, statement):
            return sql(store['container'], statement).strip()
    with closing(psycopg2.connect(store['dsn'])) as conn:
        query(conn, 'CREATE TABLE public.schema_migrations (migration_id text PRIMARY KEY)')
        conn.commit()
        try:
            with pytest.raises(ValueError, match='Multiple schema_migrations'):
                inspect_state(ActualDatabase(), ROOT / 'backend/migrations')
        finally:
            query(conn, 'DROP TABLE public.schema_migrations')
            conn.commit()
        assert ledger(conn) == [('ag_catalog',)]
        assert query(conn, 'SELECT count(*) FROM schema_migrations') == [(15,)]


def test_initdb_logs_retain_only_known_frozen_baseline_error(schema_routes):
    for lane, store in schema_routes.items():
        errors = re.findall(r'(?:ERROR|FATAL|PANIC):\s*(.*)', store['init_log'])
        # Frozen baseline performs an illegal UPDATE of a generated column.
        # Both PostgreSQL and psql report it; ON_ERROR_STOP is off there.
        # Do not describe these logs as clean or whitelist any other failure.
        assert errors == ['column "dedupe_key" can only be updated to DEFAULT'] * 2, (lane, errors)
