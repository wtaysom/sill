"""Exposure is telemetry; only explicit use refreshes recency."""
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cognitive_memory_api import MemoryType
from test_perspective_recall import client_in_transaction

OLD = datetime(2001,1,1,tzinfo=timezone.utc)

@pytest.mark.asyncio
async def test_show_is_not_use(migrated_dsn):
    async with client_in_transaction(migrated_dsn) as (client, conn):
        mid = await client.remember('telemetry '+uuid4().hex, type=MemoryType.SEMANTIC)
        content = await conn.fetchval('SELECT content FROM memories WHERE id=$1',mid)
        await conn.execute('UPDATE memories SET last_accessed=$2,access_count=0 WHERE id=$1',mid,OLD)
        async def state():
            return tuple(await conn.fetchrow('SELECT last_accessed,access_count,importance FROM memories WHERE id=$1',mid))
        importance = (await state())[2]
        for action in (lambda:client.recall(content, include_partial=False),
                       lambda:client.hydrate(content, include_partial=False),
                       lambda:client.recall_preview(content),
                       lambda:client.recall_recent(limit=100)):
            before = await state()
            await action()
            after = await state()
            assert after == (OLD,before[1]+1,importance)
        for action in (lambda:client.recall_by_id(mid),lambda:client.recall_batch([mid,mid]),
                       lambda:client.touch_memories([mid])):
            await conn.execute('UPDATE memories SET last_accessed=$2 WHERE id=$1',mid,OLD)
            await action()
            assert (await state())[0] > OLD


def test_citation_but_not_phrase_refreshes(db):
    with db.cursor() as cur:
        cur.execute("SELECT memory_id FROM public.mint_memory('semantic',%s)",('reuse '+uuid4().hex,))
        mid = cur.fetchone()[0]
        cur.execute('UPDATE memories SET last_accessed=%s WHERE id=%s',(OLD,mid))
        cur.execute("SELECT record_memory_reuse(%s,'test','body_phrase','phrase')",(mid,))
        cur.execute('SELECT last_accessed FROM memories WHERE id=%s',(mid,))
        assert cur.fetchone()[0] == OLD
        cur.execute("SELECT record_memory_reuse(%s,'test','memory_id',%s)",(mid,str(mid)))
        cur.execute('SELECT last_accessed FROM memories WHERE id=%s',(mid,))
        assert cur.fetchone()[0] > OLD


def test_working_memory_exposure_preserves_recency(db):
    with db.cursor() as cur:
        cur.execute("SELECT add_to_working_memory(%s)",('working '+uuid4().hex,))
        mid = cur.fetchone()[0]
        cur.execute('UPDATE working_memory SET last_accessed=%s,access_count=0 WHERE id=%s',(OLD,mid))
        cur.execute("SELECT * FROM search_working_memory('working',100)")
        assert str(mid) in [str(r[0]) for r in cur.fetchall()]
        cur.execute('SELECT last_accessed,access_count FROM working_memory WHERE id=%s',(mid,))
        assert cur.fetchone() == (OLD,1)


def test_selected_transport_full_text_and_exposure(migrated_dsn, monkeypatch, tmp_path):
    import psycopg2
    from scripts import recall_core as core
    selected = tmp_path/'selected.env'
    selected.write_text('SILL_DB_DSN='+migrated_dsn+'\n')
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE',str(selected))
    for key in ('SILL_DB_DSN','SILL_DB_CONTAINER','PGHOST','PGDATABASE','PGUSER','PGPASSWORD','POSTGRES_HOST'):
        monkeypatch.setenv(key,'stale-never-use')
    text = 'wire '+uuid4().hex+'\n'+('臺灣\t||| "quote" and apostrophe \'\n'*40)
    with psycopg2.connect(migrated_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT memory_id FROM public.mint_memory('semantic',%s,p_force=>'assertive',p_speaker=>'Inez',p_minted_in=>'{\"session\":\"test\"}')",(text,))
            mid = str(cur.fetchone()[0])
            cur.execute('UPDATE memories SET last_accessed=%s,access_count=0 WHERE id=%s',(OLD,mid))
    try:
        for lane in ('fast','hybrid'):
            rows = core.recall_rows(lane,text[:37],100,fast_min_similarity=.1)
            row = next(r for r in rows if r['id']==mid)
            assert row['content'] == text[:500] and row['truncated'] is True
            assert row['speaker']=='Inez' and row['minted_in']=={'session':'test'}
            assert row['production'] is None and row['source_store'] is None
            if lane=='fast': assert row['query_similarity'] == pytest.approx(1)
        assert core.full_contents([mid]) == {mid:text}
        core.record_exposure([mid,mid])
        with conn.cursor() as cur:
            cur.execute('SELECT last_accessed,access_count FROM memories WHERE id=%s',(mid,))
            assert cur.fetchone() == (OLD,1)
    finally:
        with conn:
            with conn.cursor() as cur: cur.execute('DELETE FROM memories WHERE id=%s',(mid,))
        conn.close()


def test_cli_show_selected_instance(migrated_dsn, monkeypatch, tmp_path, capsys):
    import psycopg2
    import sill_cli
    selected = tmp_path/'selected.env'
    selected.write_text('SILL_DB_DSN='+migrated_dsn+'\n')
    monkeypatch.setenv('SILL_INSTANCE_ENV_FILE',str(selected))
    monkeypatch.setenv('SILL_DB_DSN','postgresql://stale:secret@localhost:1/wrong')
    content = 'CLI full body\n'+uuid4().hex+'x'*600
    with psycopg2.connect(migrated_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT memory_id FROM public.mint_memory('semantic',%s)",(content,))
            mid = str(cur.fetchone()[0])
            cur.execute('UPDATE memories SET last_accessed=%s WHERE id=%s',(OLD,mid))
    try:
        assert sill_cli.main(['show',mid]) == 0
        assert capsys.readouterr().out == content+'\n'
        with conn.cursor() as cur:
            cur.execute('SELECT last_accessed FROM memories WHERE id=%s',(mid,))
            assert cur.fetchone()[0] > OLD
    finally:
        with conn:
            with conn.cursor() as cur: cur.execute('DELETE FROM memories WHERE id=%s',(mid,))
        conn.close()


@pytest.mark.asyncio
async def test_empty_selected_batch_does_not_touch(migrated_dsn):
    async with client_in_transaction(migrated_dsn) as (client, conn):
        assert await client.recall_batch([]) == []


@pytest.mark.asyncio
async def test_exposure_failure_does_not_abort_read_transaction(migrated_dsn, caplog):
    async with client_in_transaction(migrated_dsn) as (client, conn):
        mid = await client.remember('telemetryfailure '+uuid4().hex)
        await conn.execute('''CREATE FUNCTION pg_temp.fail_access() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic access failure'; END $$''')
        await conn.execute('''CREATE TRIGGER test_fail_access BEFORE UPDATE OF access_count ON memories
            FOR EACH ROW EXECUTE FUNCTION pg_temp.fail_access()''')
        rows = await client.recall_recent(limit=100)
        assert mid in [row.id for row in rows]
        assert await conn.fetchval('SELECT 1') == 1
        assert 'exposure telemetry failed' in caplog.text


def test_upgrade_preserves_dates_and_working_query_profile(fixture_dsn, fixture_container):
    from bootstrap_fixture import temporary_fixture, apply_migrations
    import psycopg2
    with temporary_fixture(fixture_dsn,fixture_container,through='014') as target:
        with closing(psycopg2.connect(target)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute("SELECT memory_id FROM public.mint_memory('semantic','historic synthetic')")
                mid = cur.fetchone()[0]
                cur.execute('UPDATE memories SET last_accessed=%s,access_count=7 WHERE id=%s',(OLD,mid))
                cur.execute("SELECT pg_get_functiondef('public.search_working_memory(text,integer)'::regprocedure)")
                assert "get_embedding(p_query_text, 'query')" in cur.fetchone()[0]
        apply_migrations(target,after='014',through='015')
        with closing(psycopg2.connect(target)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute('SELECT last_accessed,access_count FROM memories WHERE id=%s',(mid,))
                assert cur.fetchone() == (OLD,7)
                cur.execute("SELECT pg_get_functiondef('public.search_working_memory(text,integer)'::regprocedure)")
                assert "get_embedding(p_query_text, 'query')" in cur.fetchone()[0]


def test_legacy_readers_expose_without_use(db):
    from memory_tools import MemoryToolHandler
    handler = MemoryToolHandler({})
    handler.conn = db
    with db.cursor() as cur:
        cur.execute("SELECT memory_id FROM public.mint_memory('semantic',%s)",('legacy '+uuid4().hex,))
        mid = cur.fetchone()[0]
        cur.execute('UPDATE memories SET last_accessed=%s,access_count=0 WHERE id=%s',(OLD,mid))
    for read in (lambda:handler._handle_recall({'query':'legacy','limit':100}),
                 lambda:handler._handle_recall_recent({'limit':100,'by_access':False})):
        assert str(mid) in [str(row['memory_id']) for row in read()['memories']]
        with db.cursor() as cur:
            cur.execute('SELECT last_accessed,access_count FROM memories WHERE id=%s',(mid,))
            when,count = cur.fetchone()
            assert when == OLD and count > 0


def test_docker_transport_uses_configured_fixture(migrated_dsn,fixture_container,monkeypatch):
    from scripts import recall_core as core
    from psycopg2.extensions import parse_dsn
    monkeypatch.delenv('SILL_INSTANCE_ENV_FILE',raising=False)
    monkeypatch.setenv('SILL_DB_CONTAINER',fixture_container)
    monkeypatch.setenv('SILL_DB_USER','sill_test')
    monkeypatch.setenv('SILL_DB_NAME',parse_dsn(migrated_dsn)['dbname'])
    monkeypatch.setenv('DOCKER_CONTEXT','orbstack')
    assert isinstance(core.recall_rows('hybrid','synthetic fixture',1),list)


def test_legacy_exposure_failure_keeps_successful_read(db, caplog):
    from memory_tools import MemoryToolHandler
    handler = MemoryToolHandler({})
    handler.conn = db
    with db.cursor() as cur:
        cur.execute("SELECT memory_id FROM public.mint_memory('semantic',%s)",('failure '+uuid4().hex,))
        mid = cur.fetchone()[0]
        cur.execute('''CREATE FUNCTION pg_temp.fail_access() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic access failure'; END $$''')
        cur.execute('''CREATE TRIGGER test_fail_access BEFORE UPDATE OF access_count ON memories
            FOR EACH ROW EXECUTE FUNCTION pg_temp.fail_access()''')
    rows = handler._handle_recall_recent({'limit':20,'by_access':False})['memories']
    assert str(mid) in [str(row['memory_id']) for row in rows]
    with db.cursor() as cur:
        cur.execute('SELECT 1')
        assert cur.fetchone()[0] == 1
    assert 'exposure telemetry failed' in caplog.text
