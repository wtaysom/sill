"""Real SQL/HTTP regime transitions, only on explicitly guarded disposable stores."""
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import psycopg2
import pytest

from conftest import BACKEND
from bootstrap_fixture import temporary_fixture
from embedding_profiles import resolve_profile

MIGRATION = BACKEND / 'migrations/014_embedding_profiles.sql'


@pytest.fixture
def regime_db(fixture_dsn, fixture_container):
    assert MIGRATION.exists(), '014 profile migration is missing'
    with temporary_fixture(fixture_dsn, fixture_container, through='013') as dsn:
        with closing(psycopg2.connect(dsn)) as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(MIGRATION.read_text())
        yield dsn


@pytest.fixture
def http_service():
    state = {'inputs': [], 'status': 200, 'payload': None, 'failures': 0, 'dimension':768}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'model_id': 'google/embeddinggemma-300m', 'model_sha': 'fixture-rev'}).encode())
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['inputs'].append(body['inputs'])
            status = state['status']
            if state['failures']:
                state['failures'] -= 1
                status = state.get('transient_status',503)
            self.send_response(status)
            self.end_headers()
            digest = hashlib.sha256(body['inputs'].encode()).digest()
            vector = [(digest[i % 32] + 1) / 256 for i in range(state['dimension'])]
            payload = state['payload'] if state['payload'] is not None else [vector]
            self.wfile.write(json.dumps(payload).encode())
    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state['url'] = f'http://host.docker.internal:{server.server_port}/embed'
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configure(conn, service):
    with conn.cursor() as cur:
        cur.execute("UPDATE public.embedding_config SET value=%s WHERE key='service_url'", (service['url'],))
        cur.execute("INSERT INTO public.embedding_config(key,value) VALUES('retry_seconds','1'),('retry_interval_seconds','0.01') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    conn.commit()


def scalar(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def test_upgrade_starts_in_legacy_compatibility(regime_db, http_service):
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn, http_service)
        assert scalar(conn, 'SELECT profile FROM public.embedding_regime') is None
        before = scalar(conn, 'SELECT jsonb_object_agg(id,trigger_embedding::text) FROM boundaries')
        scalar(conn, "SELECT public.get_embedding('legacy query','query')")
        scalar(conn, "SELECT public.get_embedding('legacy document')")
        assert http_service['inputs'] == ['legacy query', 'legacy document']
        assert scalar(conn, 'SELECT jsonb_object_agg(id,trigger_embedding::text) FROM boundaries') == before


@pytest.mark.parametrize('status,payload', [(400,None),(401,None),(403,None),(404,None),(200,[[1,2]]),(200,{'bad':'shape'})])
def test_permanent_embedding_errors_fail_once(regime_db, http_service, status, payload):
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn, http_service)
        http_service.update(status=status, payload=payload)
        with pytest.raises(psycopg2.Error):
            scalar(conn, "SELECT public.safe_get_embedding('invalid payload')")
        assert len(http_service['inputs']) == 1


@pytest.mark.parametrize('transient_status',[429,503])
def test_transient_embedding_errors_retry(regime_db, http_service, transient_status):
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn, http_service)
        http_service['failures'] = 2
        http_service['transient_status'] = transient_status
        assert scalar(conn, "SELECT vector_dims(public.get_embedding('retry'))") == 768
        assert len(http_service['inputs']) == 3


def migration(dsn):
    from scripts import reembed_documents
    assert hasattr(reembed_documents, 'ProfileMigration'), 'resumable operator is missing'
    return reembed_documents.ProfileMigration(dsn)


def seed_sources(conn):
    mid = scalar(conn, "SELECT public.create_memory('semantic','original memory for regime migration')")
    archived = scalar(conn, "SELECT public.create_memory('semantic','archived memory for regime migration')")
    with conn.cursor() as cur:
        cur.execute("UPDATE memories SET archived_at=now(),status='archived' WHERE id=%s", (archived,))
        cur.execute("INSERT INTO working_memory(content,embedding) VALUES('working memory source',public.get_embedding('working memory source'))")
        cur.execute("INSERT INTO episodes(started_at,summary,summary_embedding) VALUES(now(),'episode source',public.get_embedding('episode source'))")
        cur.execute("INSERT INTO memory_clusters(cluster_type,name) VALUES('theme','fixture cluster') RETURNING id")
        cid = cur.fetchone()[0]
        cur.execute("INSERT INTO memory_cluster_members(cluster_id,memory_id,membership_strength) VALUES(%s,%s,1)", (cid,mid))
        cur.execute('SELECT public.recalculate_cluster_centroid(%s)', (cid,))
        cur.execute('SELECT public.recompute_neighborhood(%s)', (mid,))
    conn.commit()
    return mid, archived, cid


def profiles():
    target = resolve_profile('google/embeddinggemma-300m',768,revision='fixture-rev')
    source = resolve_profile(target.model_id,768,revision=target.revision,query_prefix='',document_prefix='')
    return source, target


def prepare(operator, tmp_path, container):
    source, target = profiles()
    return operator.prepare(target, source_attestation=source, backup_dir=tmp_path, pg_tools_container=container)


def snapshot(conn):
    return scalar(conn, "SELECT jsonb_object_agg(relation_name||':'||row_id,jsonb_build_array(source_text,embedding::text)) FROM public.embedding_sources")


def test_prepare_resume_does_not_mix_regimes(regime_db, http_service, tmp_path, fixture_container):
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        mid, archived, cid = seed_sources(conn)
        before = snapshot(conn)
        conn.commit()
        prepare(op,tmp_path,fixture_container)
        first = op.resume(batch_size=2, max_batches=1)
        assert first['generated'] == 2 and first['remaining'] > 0
        assert snapshot(conn) == before
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("UPDATE memories SET content='changed during prepare',embedding=public.get_embedding('changed during prepare') WHERE id=%s", (mid,))
            cur.execute("DELETE FROM working_memory")
        new = scalar(conn, "SELECT public.create_memory('semantic','new during prepare')")
        conn.commit()
        active_before = snapshot(conn)
        conn.commit()
        with pytest.raises(ValueError,match='resume required'):
            op.activate()
        final = op.resume(batch_size=2)
        assert final['remaining'] == 0
        assert op.resume()['generated'] == 0
        assert snapshot(conn) == active_before
        conn.commit()
        op.activate()
        assert scalar(conn,'SELECT profile FROM public.embedding_regime') == profiles()[1].to_dict()
        assert scalar(conn,"SELECT count(*) FROM public.embedding_sources s LEFT JOIN public.embedding_stage t ON t.operation_id=%s AND t.relation_name=s.relation_name AND t.row_id=s.row_id AND t.profile_fingerprint=%s WHERE t.embedding IS DISTINCT FROM s.embedding",(op.status()['operation']['id'],profiles()[1].fingerprint)) == 0
        assert scalar(conn,'SELECT centroid_embedding= (SELECT embedding FROM memories WHERE id=%s) FROM memory_clusters WHERE id=%s',(mid,cid))
        assert scalar(conn,'SELECT count(*) FROM memory_neighborhoods WHERE NOT is_stale') == 0
        assert scalar(conn,"SELECT count(*) FROM public.hybrid_recall('changed during prepare',10,60)") > 0
        assert scalar(conn,'SELECT archived_at IS NOT NULL FROM memories WHERE id=%s',(archived,))


def test_rollback_preserves_records_created_during_operation(regime_db,http_service,tmp_path,fixture_container):
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        mid, archived, cid = seed_sources(conn)
        original = snapshot(conn)
        conn.commit()
        prepare(op,tmp_path,fixture_container)
        op.resume()
        op.activate()
        new = scalar(conn,"SELECT public.create_memory('semantic','new after activation')")
        with conn.cursor() as cur:
            cur.execute("UPDATE memories SET content='changed after activation',embedding=public.get_embedding('changed after activation') WHERE id=%s",(mid,))
            cur.execute("INSERT INTO working_memory(content,embedding) VALUES('working after activation',public.get_embedding('working after activation'))")
            cur.execute("INSERT INTO episodes(started_at,summary,summary_embedding) VALUES(now(),'episode after activation',public.get_embedding('episode after activation'))")
            cur.execute("UPDATE episodes SET summary='changed episode after activation',summary_embedding=public.get_embedding('changed episode after activation') WHERE summary='episode source'")
            cur.execute("INSERT INTO boundaries(name,embedding_text,trigger_embedding,boundary_type,response_type) VALUES('boundary after activation','boundary after activation',public.get_embedding('boundary after activation'),'ethical','flag')")
        conn.commit()
        http_service['status'] = 400
        with pytest.raises(psycopg2.Error):
            op.rollback()
        assert scalar(conn,'SELECT profile FROM public.embedding_regime') == profiles()[1].to_dict()
        conn.commit()
        http_service['status'] = 200
        op.rollback()
        assert scalar(conn,'SELECT profile FROM public.embedding_regime') == profiles()[0].to_dict()
        assert scalar(conn,'SELECT content FROM memories WHERE id=%s',(new,)) == 'new after activation'
        assert scalar(conn,'SELECT content FROM memories WHERE id=%s',(mid,)) == 'changed after activation'
        for text in ('working after activation','episode after activation','changed episode after activation','boundary after activation'):
            assert scalar(conn,"SELECT embedding=public.get_embedding(source_text) FROM embedding_sources WHERE source_text=%s",(text,))
        after = snapshot(conn)
        assert after['memories:'+str(archived)] == original['memories:'+str(archived)]
        assert scalar(conn,"SELECT count(*) FROM public.hybrid_recall('new after activation',10,60)") > 0
        assert op.status()['operation']['state'] == 'rolled_back'


def test_precomputed_ingest_regime_is_explicit(regime_db,http_service,tmp_path,fixture_container):
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        scalar(conn,"SELECT public.batch_create_memories_with_embeddings('semantic',ARRAY['legacy supplied'],jsonb_build_array(to_jsonb(array_fill(0.1::float,ARRAY[768]))))")
        conn.commit()
        prepare(op,tmp_path,fixture_container)
        op.resume()
        op.activate()
        for supplied in (None,'wrong'):
            with pytest.raises(psycopg2.Error,match='embedding_profile'):
                scalar(conn,"SELECT public.batch_create_memories_with_embeddings('semantic',ARRAY['refused supplied'],jsonb_build_array(to_jsonb(array_fill(0.1::float,ARRAY[768]))),0.5,%s)",(supplied,))
            conn.rollback()
            assert scalar(conn,"SELECT count(*) FROM memories WHERE content='refused supplied'") == 0
        assert scalar(conn,"SELECT cardinality(public.batch_create_memories_with_embeddings('semantic',ARRAY['matching supplied'],jsonb_build_array(to_jsonb(array_fill(0.1::float,ARRAY[768]))),0.5,%s))",(profiles()[1].fingerprint,)) == 1


def test_activation_requires_complete_compatible_vector_set(regime_db,http_service,tmp_path,fixture_container):
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        mid, _, _ = seed_sources(conn)
        prepare(op,tmp_path,fixture_container)
        op.resume()
        with conn.cursor() as cur:
            cur.execute("UPDATE memories SET content='changed after staging',embedding=public.get_embedding('changed after staging') WHERE id=%s",(mid,))
        conn.commit()
        before = snapshot(conn)
        conn.commit()
        with pytest.raises(ValueError,match='resume required'):
            op.activate()
        assert snapshot(conn)==before
        assert scalar(conn,'SELECT profile FROM public.embedding_regime') is None
        conn.commit()
        op.resume()
        op.activate()
        assert scalar(conn,"SELECT content FROM memories WHERE id=%s",(mid,))=='changed after staging'


def test_source_attestation_and_live_identity_fail_closed(regime_db,http_service,tmp_path,fixture_container):
    from dataclasses import replace
    op = migration(regime_db)
    source,target = profiles()
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        for changes in ({'model_id':'other'},{'revision':'other'},{'dimension':384},{'normalization':'none'}):
            with pytest.raises(ValueError,match='same-model prompt migration only'):
                op.prepare(replace(target,**changes),source_attestation=source,backup_dir=tmp_path,pg_tools_container=fixture_container)
        with pytest.raises(ValueError,match='unknown'):
            op.prepare(target,backup_dir=tmp_path,pg_tools_container=fixture_container)
        with pytest.raises(ValueError,match='live service'):
            op.prepare(replace(target,revision='wrong'),source_attestation=replace(source,revision='wrong'),backup_dir=tmp_path,pg_tools_container=fixture_container)
        assert scalar(conn,'SELECT count(*) FROM public.embedding_operations')==0
        assert scalar(conn,'SELECT profile FROM public.embedding_regime') is None
        assert list(tmp_path.iterdir())==[]


def test_custom_boundary_source_must_be_explicit(regime_db,http_service,tmp_path,fixture_container):
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO boundaries(name,description,boundary_type,response_type,trigger_embedding) VALUES('custom','not the original text','ethical','flag',public.get_embedding('original private trigger'))")
        conn.commit()
        with pytest.raises(ValueError,match='Missing embedding source text'):
            prepare(op,tmp_path,fixture_container)
        assert scalar(conn,'SELECT count(*) FROM public.embedding_operations')==0


def test_python_query_consumers_use_active_query_profile(regime_db,http_service,tmp_path,fixture_container):
    from memory_tools import MemoryToolHandler
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        seed_sources(conn)
        scalar(conn,"SELECT memory_id FROM public.mint_memory('procedural','procedure document',p_type_metadata=>'{}')")
        scalar(conn,"SELECT memory_id FROM public.mint_memory('strategic','strategy document',p_type_metadata=>'{}')")
        conn.commit()
        prepare(op,tmp_path,fixture_container)
        op.resume()
        op.activate()
        handler = MemoryToolHandler({})
        handler.conn = conn
        http_service['inputs'].clear()
        handler.execute_tool('explore_cluster',{'query':'cluster needle'})
        handler.execute_tool('get_procedures',{'task':'procedure needle'})
        handler.execute_tool('get_strategies',{'situation':'strategy needle'})
        assert conn.get_transaction_status() == 0
        assert http_service['inputs'] == [profiles()[1].query_prefix+t for t in ('cluster needle','procedure needle','strategy needle')]


def test_writer_barrier_rechecks_committed_new_rows(regime_db,http_service,tmp_path,fixture_container):
    import concurrent.futures
    import time
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as writer, closing(psycopg2.connect(regime_db)) as observer:
        configure(writer,http_service)
        seed_sources(writer)
        prepare(op,tmp_path,fixture_container)
        op.resume()
        # This insert holds the shared barrier until its transaction commits.
        mid = scalar(writer,"SELECT public.create_memory('semantic','concurrent writer new record')")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(op.activate)
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                blocked = scalar(observer,"SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=714014 AND NOT granted")
                observer.commit()
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, 'activation must wait for the existing writer transaction'
            assert not future.done()
            writer.commit()
            with pytest.raises(ValueError,match='resume required'):
                future.result(timeout=10)
        assert scalar(observer,'SELECT content FROM memories WHERE id=%s',(mid,)) == 'concurrent writer new record'
        assert scalar(observer,'SELECT profile FROM public.embedding_regime') is None
        observer.commit()
        op.resume()
        op.activate()
        assert scalar(observer,'SELECT profile FROM public.embedding_regime') == profiles()[1].to_dict()


def test_sql_query_consumers_and_cache_keys(regime_db,http_service,tmp_path,fixture_container):
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        seed_sources(conn)
        prepare(op,tmp_path,fixture_container)
        op.resume()
        op.activate()
        target = profiles()[1]
        assert scalar(conn,'SELECT public.embedding_profile_fingerprint(%s::jsonb)',(json.dumps(target.to_dict()),)) == target.fingerprint
        http_service['inputs'].clear()
        for function,args in [('fast_recall',',10'),('fast_recall_hybrid',',10'),('hybrid_recall',',10,60'),
                              ('hybrid_recall_perspective',',10,NULL,60'),('find_partial_activations',',0.7,0.5'),
                              ('check_boundaries',''),('search_working_memory',',10'),
                              ('search_similar_memories',',10,NULL::memory_type[],0.0'),
                              ('search_similar_memories',',10,NULL::memory_type[]')]:
            query = function+args
            statement = f'SELECT count(*) FROM public.{function}(%s{args})'
            if function=='search_similar_memories' and args==',10,NULL::memory_type[]':
                statement = 'SELECT count(*) FROM public.search_similar_memories(query_text=>%s,result_limit=>10,type_filter=>NULL::memory_type[])'
            scalar(conn,statement,(query,))
            assert http_service['inputs'][-1] == target.query_prefix+query
        scalar(conn,"SELECT public.get_embedding('same input','query')")
        scalar(conn,"SELECT public.get_embedding('same input','document')")
        assert scalar(conn,'SELECT count(*) FROM embedding_cache WHERE content_hash=ANY(%s)',
                      ([target.cache_key(k,'same input') for k in ('query','document')],)) == 2


def test_backup_is_private_verified_and_retained(regime_db,http_service,tmp_path,fixture_container,monkeypatch):
    import stat
    # sill-memory supports Python 3.10, which has no hashlib.file_digest.
    monkeypatch.delattr(hashlib,'file_digest',raising=False)
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        result = prepare(op,tmp_path,fixture_container)
        backup = result['backup']
        path = Path(backup['path'])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == backup['sha256']
        assert stat.S_IMODE(path.stat().st_mode)==0o600
        assert stat.S_IMODE(path.parent.stat().st_mode)==0o700
        assert 'pg_restore --list' in backup['verification']
        metadata=json.loads((path.parent/'profile-and-functions.json').read_text())
        assert metadata['provenance']['historical_identity']=='declared-unverified'
        assert metadata['source_profile']==profiles()[0].to_dict()
        assert metadata['function_snapshot']
        status = op.status()
        assert status['provenance']['historical_identity']=='unknown'
        assert status['operation']['provenance']['historical_identity']=='declared-unverified'
        assert scalar(conn,'SELECT count(*) FROM embedding_operations,LATERAL jsonb_object_keys(function_snapshot)') > 0
        conn.commit()
        op.rollback()
        assert path.exists()


def test_custom_dimension_upgrade_keeps_raw_vectors(fixture_dsn,fixture_container,http_service,monkeypatch):
    import bootstrap_fixture
    baseline = bootstrap_fixture.baseline_sql
    monkeypatch.setattr(bootstrap_fixture,'baseline_sql',lambda: "SET app.embedding_dimension='384';\n"+baseline())
    with temporary_fixture(fixture_dsn,fixture_container,through='013') as dsn:
        with closing(psycopg2.connect(dsn)) as conn:
            configure(conn,http_service)
            before = scalar(conn,'SELECT jsonb_object_agg(id,trigger_embedding::text) FROM boundaries')
            with conn.cursor() as cur:
                cur.execute(MIGRATION.read_text())
            conn.commit()
            http_service['dimension'] = 384
            assert scalar(conn,"SELECT vector_dims(public.get_embedding('custom raw document'))") == 384
            assert http_service['inputs'] == ['custom raw document']
            assert scalar(conn,'SELECT jsonb_object_agg(id,trigger_embedding::text) FROM boundaries') == before
            assert scalar(conn,'SELECT profile FROM public.embedding_regime') is None


def test_summary_source_preserves_original_whitespace(regime_db,http_service,tmp_path,fixture_container):
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO episodes(started_at,summary,summary_embedding) VALUES(now(),'  exact summary  ',public.get_embedding('  exact summary  '))")
        conn.commit()
        prepare(op,tmp_path,fixture_container)
        http_service['inputs'].clear()
        op.resume()
        assert profiles()[1].document_prefix+'  exact summary  ' in http_service['inputs']


def test_old_repeatable_read_writer_cannot_mint_old_regime_after_cutover(regime_db,http_service,tmp_path,fixture_container):
    op = migration(regime_db)
    with closing(psycopg2.connect(regime_db)) as conn:
        configure(conn,http_service)
        prepare(op,tmp_path,fixture_container)
        op.resume()
        conn.set_session(isolation_level='REPEATABLE READ')
        assert scalar(conn,'SELECT profile FROM public.embedding_regime') is None
        op.activate()
        with pytest.raises(psycopg2.errors.SerializationFailure):
            scalar(conn,"SELECT public.create_memory('semantic','old transaction attempting a late insert')")
        conn.rollback()


def test_existing_age_query_variants_are_patched(fixture_dsn,fixture_container,http_service):
    with temporary_fixture(fixture_dsn,fixture_container,through='013') as dsn:
        with closing(psycopg2.connect(dsn)) as conn:
            configure(conn,http_service)
            with conn.cursor() as cur:
                cur.execute("SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname IN ('hybrid_recall','hybrid_recall_perspective','check_boundaries','search_working_memory','search_similar_memories','fast_recall','fast_recall_hybrid','find_partial_activations')")
                for (definition,) in cur.fetchall():
                    cur.execute(definition.replace('FUNCTION public.','FUNCTION ag_catalog.',1))
                cur.execute(MIGRATION.read_text())
                cur.execute("SELECT n.nspname,p.proname,p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('public','ag_catalog') AND p.proname IN ('hybrid_recall','hybrid_recall_perspective','check_boundaries','search_working_memory','search_similar_memories','fast_recall','fast_recall_hybrid','find_partial_activations')")
                rows = cur.fetchall()
                assert {r[0] for r in rows}=={'public','ag_catalog'}
                assert all(", 'query')" in r[2] for r in rows)
            scalar(conn,"SELECT count(*) FROM ag_catalog.hybrid_recall('AGE query',10,60)")
            assert http_service['inputs'] == ['AGE query']


def test_ordinary_integration_fixture_stubs_both_network_overloads(db):
    with db.cursor() as cur:
        cur.execute("UPDATE embedding_config SET value='http://127.0.0.1:1/embed' WHERE key='service_url'")
        cur.execute("INSERT INTO embedding_config(key,value) VALUES('retry_seconds','0') ON CONFLICT(key) DO UPDATE SET value='0'")
    assert scalar(db,"SELECT vector_dims(public.get_embedding('fixture query boundary','query'))")==768
