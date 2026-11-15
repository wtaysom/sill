"""Goal state and commitment ownership on an explicitly disposable store.

These tests catch mismatched terminal dates, release of unrelated records,
revival of manual/superseded archives, and partial notice writes.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
import time
from uuid import uuid4

import psycopg2
import pytest

from bootstrap_fixture import temporary_fixture
from conftest import BACKEND
from test_atomic_mint import mint, query, snapshot


def goal(db, priority='active'):
    return query(db, 'INSERT INTO goals(title,priority) VALUES (%s,%s) RETURNING id',
                 ('Synthetic goal ' + str(uuid4()), priority))[0][0]


def link(db, mid, gid, kind='home'):
    query(db, 'INSERT INTO goal_memory_links(memory_id,goal_id,link_type) VALUES (%s,%s,%s)', (mid, gid, kind))


def state(db, gid):
    return query(db, 'SELECT priority,completed_at,abandoned_at,abandonment_reason FROM goals WHERE id=%s', (gid,))[0]


def active(db, mid):
    return query(db, "SELECT status='active' AND archived_at IS NULL FROM memories WHERE id=%s", (mid,))[0][0]


def test_goal_state_normalized_for_api_and_raw_sql(db):
    gid = goal(db)
    query(db, "UPDATE goals SET completed_at='2026-01-02T00:00:00Z' WHERE id=%s", (gid,))
    assert state(db, gid)[0] == 'completed'
    query(db, "SELECT public.change_goal_priority(%s,'abandoned','no longer needed')", (gid,))
    assert state(db, gid)[0] == 'abandoned' and state(db, gid)[1] is None
    assert state(db, gid)[2] is not None and state(db, gid)[3] == 'no longer needed'
    query(db, "SELECT public.change_goal_priority(%s,'active')", (gid,))
    assert state(db, gid) == ('active', None, None, None)
    query(db, 'SAVEPOINT conflict')
    with pytest.raises(psycopg2.errors.CheckViolation):
        query(db, "UPDATE goals SET completed_at=now(),abandoned_at=now() WHERE id=%s", (gid,))
    query(db, 'ROLLBACK TO SAVEPOINT conflict')
    # Simulate historical unknown completion without normalizing other history.
    query(db, 'ALTER TABLE goals DISABLE TRIGGER USER')
    query(db, "UPDATE goals SET priority='completed',completed_at=NULL WHERE id=%s", (gid,))
    query(db, 'ALTER TABLE goals ENABLE TRIGGER USER')
    query(db, "SELECT public.change_goal_priority(%s,'completed')", (gid,))
    assert state(db, gid) == ('completed', None, None, None)
    query(db, "UPDATE goals SET priority='queued' WHERE id=%s", (gid,))
    assert state(db, gid) == ('queued', None, None, None)


@pytest.mark.parametrize('terminal,date', [('completed','completed_at'), ('abandoned','abandoned_at')])
def test_close_releases_only_home_links(db, terminal, date):
    gid = goal(db)
    home, support, unrelated = [mint(db, str(uuid4()) + ' commitment')[0] for _ in range(3)]
    link(db, home, gid)
    link(db, support, gid, 'support')
    query(db, f'UPDATE goals SET {date}=now() WHERE id=%s', (gid,))
    assert state(db, gid)[0] == terminal
    assert not active(db, home)
    assert active(db, support) and active(db, unrelated)


def test_reopen_restores_only_its_release(db):
    first, second = goal(db), goal(db)
    live, manual, superseded, other, overwritten = [mint(db, str(uuid4()) + ' commitment')[0] for _ in range(5)]
    for mid in (live, manual, superseded, overwritten):
        link(db, mid, first)
    link(db, other, second)
    query(db, "UPDATE memories SET archived_at=now(),archive_reason='manual' WHERE id=%s", (manual,))
    query(db, "SELECT public.supersede_memory(%s,'A successor to the old synthetic commitment')", (superseded,))
    for _ in range(2):
        query(db, "SELECT public.change_goal_priority(%s,'completed')", (first,))
        query(db, "SELECT public.change_goal_priority(%s,'abandoned')", (second,))
        # A manual archival AFTER release owns its own reason and must survive.
        query(db, "UPDATE memories SET archived_at=now(),archive_reason='manual after release' WHERE id=%s", (overwritten,))
        query(db, "SELECT public.change_goal_priority(%s,'active')", (first,))
        assert active(db, live)
        assert all(not active(db, mid) for mid in (manual, superseded, other, overwritten))
    query(db, 'SAVEPOINT second_home')
    with pytest.raises(psycopg2.errors.UniqueViolation):
        link(db, live, goal(db))
    query(db, 'ROLLBACK TO SAVEPOINT second_home')


def test_notice_goal_and_mint_are_transactional(db, monkeypatch, capsys):
    import sill
    import sill_cli
    monkeypatch.setattr(sill, '_query_db', lambda sql, timeout=60: query(db, sql))
    monkeypatch.setattr(sill, 'mint_context', lambda via: {'session': None, 'as': None, 'when': None, 'via': via, 'model': None})
    gid = goal(db)
    words = 'A notice commitment must be stored and linked as one transaction.'
    count = query(db, 'SELECT count(*) FROM memories')
    query(db, 'SAVEPOINT missing_goal')
    with pytest.raises(psycopg2.Error):
        sill.notice(words, speaker='test', concepts=['rollback-concept'], goal=str(uuid4()))
    query(db, 'ROLLBACK TO SAVEPOINT missing_goal')
    assert query(db, 'SELECT count(*) FROM memories') == count
    assert not query(db, "SELECT 1 FROM concepts WHERE name='rollback-concept'")
    mid = sill.notice(words, speaker='test', source='original', concepts=['original'])
    before = snapshot(db, mid)
    assert sill.notice(words, speaker='test', source='changed', concepts=['changed'], goal=gid) == mid
    assert snapshot(db, mid) == before
    assert not query(db, 'SELECT 1 FROM goal_memory_links WHERE memory_id=%s', (mid,))
    assert 'link_commitment' in capsys.readouterr().err
    # Drive the PUBLIC CLI delegation, including combined goal/supersedes flags.
    assert sill_cli.main(['notice', 'A new declared commitment supersedes the first.', '--speaker','test',
                          '--supersedes', mid, '--goal', gid, '--concepts', 'successor',
                          '--force', 'commissive', '--source', "https://example.test/O'Brien"]) == 0
    successor = query(db, 'SELECT id FROM memories WHERE supersedes_id=%s', (mid,))[0][0]
    assert query(db, "SELECT goal_id FROM goal_memory_links WHERE memory_id=%s AND link_type='home'", (successor,)) == [(gid,)]
    assert not active(db, mid) and active(db, successor)
    assert query(db, "SELECT speaker,force,source_attribution->>'ref' FROM memories WHERE id=%s", (successor,)) == [
        ('test', 'commissive', "https://example.test/O'Brien")]
    before = snapshot(db, successor)
    query(db, 'SAVEPOINT missing_successor_home')
    with pytest.raises(psycopg2.Error):
        sill.notice('This successor must roll back.', speaker='test', supersedes=successor, goal=str(uuid4()))
    query(db, 'ROLLBACK TO SAVEPOINT missing_successor_home')
    assert snapshot(db, successor) == before


@pytest.mark.asyncio
async def test_api_mcp_link_and_priority(migrated_dsn):
    from cognitive_memory_api import CognitiveMemory, GoalPriority
    import sill_mcp_server as mcp
    schemas = {t.name: t.inputSchema for t in mcp._tools()}
    assert 'link_commitment' in schemas and 'change_goal_priority' in schemas
    async with CognitiveMemory.connect(migrated_dsn, min_size=1, max_size=2) as api:
        async with api._pool.acquire() as conn:
            gid = await conn.fetchval("INSERT INTO goals(title,priority) VALUES($1,'active') RETURNING id",
                                      'Synthetic MCP lifecycle ' + str(uuid4()))
        mid = await api.remember('Synthetic commitment ' + str(uuid4()), speaker='test')
        await mcp._dispatch_tool(api, 'link_commitment', {'memory_id': str(mid), 'goal_id': str(gid)})
        await mcp._dispatch_tool(api, 'change_goal_priority', {'goal_id': str(gid), 'priority': 'abandoned', 'reason': 'test closure'})
        await api.link_commitment(mid, gid)  # Idempotent retry even after closure.
        async with api._pool.acquire() as conn:
            assert await conn.fetchval('SELECT archived_at IS NOT NULL FROM memories WHERE id=$1', mid)
            assert await conn.fetchval('SELECT abandonment_reason FROM goals WHERE id=$1', gid) == 'test closure'
        await api.change_goal_priority(gid, GoalPriority.ACTIVE)
        async with api._pool.acquire() as conn:
            assert await conn.fetchval("SELECT status='active' AND archived_at IS NULL FROM memories WHERE id=$1", mid)


def test_closed_home_rejected_and_same_home_retry_is_immutable(db):
    gid = goal(db)
    mid, late = [mint(db, str(uuid4()) + ' commitment')[0] for _ in range(2)]
    query(db, 'SELECT public.link_commitment(%s,%s)', (mid,gid))
    query(db, "SELECT public.change_goal_priority(%s,'completed')", (gid,))
    before = snapshot(db, mid)
    query(db, 'SELECT public.link_commitment(%s,%s)', (mid,gid))
    assert snapshot(db, mid) == before
    for sql in ('SELECT public.link_commitment(%s,%s)',
                "INSERT INTO goal_memory_links(memory_id,goal_id,link_type) VALUES(%s,%s,'home')"):
        query(db, 'SAVEPOINT closed_link')
        with pytest.raises(psycopg2.Error, match='reopen'):
            query(db, sql, (late,gid))
        query(db, 'ROLLBACK TO SAVEPOINT closed_link')
    assert active(db, late)


def test_multiple_home_preflight_preserves_legacy_data(fixture_dsn, fixture_container):
    path = BACKEND / 'migrations/013_commitment_home_release.sql'
    assert path.exists(), 'commitment migration is missing'
    with temporary_fixture(fixture_dsn, fixture_container, through='012') as dsn:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            mid = mint(conn, 'A legacy commitment with two explicit homes.')[0]
            a, b = goal(conn), goal(conn)
            link(conn, mid, a); link(conn, mid, b)
            before = snapshot(conn, mid)
            with pytest.raises(psycopg2.Error) as err:
                query(conn, path.read_text())
            assert err.value.pgcode == '55000'
            assert all(value in str(err.value) for value in (mid,a,b))
            query(conn, 'ROLLBACK')
            assert snapshot(conn, mid) == before
            assert len(query(conn, "SELECT * FROM goal_memory_links WHERE memory_id=%s", (mid,))) == 2
            assert query(conn, "SELECT to_regprocedure('public.link_commitment(uuid,uuid)')") == [(None,)]
        finally:
            conn.close()


@pytest.mark.parametrize('first', ['link', 'close'])
def test_link_and_close_serialize(migrated_dsn, first):
    with closing(psycopg2.connect(migrated_dsn)) as setup, setup:
        gid = goal(setup)
        mid = mint(setup, str(uuid4()) + ' concurrent commitment')[0]
    owner = psycopg2.connect(migrated_dsn)
    started = Event()
    contender_pid = []
    def operation(conn, kind):
        if kind == 'link':
            link(conn, mid, gid)
        else:
            query(conn, "UPDATE goals SET priority='completed' WHERE id=%s", (gid,))
    def contender():
        with closing(psycopg2.connect(migrated_dsn)) as conn, conn:
            query(conn, "SET LOCAL statement_timeout='5s'")
            contender_pid.append(query(conn, 'SELECT pg_backend_pid()')[0][0])
            started.set()
            try:
                operation(conn, 'close' if first == 'link' else 'link')
                return 'ok'
            except psycopg2.Error as exc:
                assert 'reopen' in str(exc)
                return 'closed'
    try:
        operation(owner, first)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(contender)
            assert started.wait(5)
            # Prove overlap: the second transaction must actually wait for the
            # first home/closure lock, rather than merely run after its commit.
            deadline = time.monotonic() + 3
            while not query(owner, 'SELECT pg_backend_pid()=ANY(pg_blocking_pids(%s))', (contender_pid[0],))[0][0]:
                assert time.monotonic() < deadline, 'link/closure did not serialize on the goal lock'
                time.sleep(.01)
            owner.commit()
            assert pending.result(timeout=10) == ('ok' if first == 'link' else 'closed')
        assert active(owner, mid) == (first == 'close')
        assert len(query(owner, "SELECT * FROM goal_memory_links WHERE memory_id=%s", (mid,))) == (1 if first == 'link' else 0)
    finally:
        owner.rollback()
        owner.close()


@pytest.mark.asyncio
async def test_create_goal_and_deduplicated_priority_use_enum(migrated_dsn):
    """The baseline CASE inferred text and prevented even first goal creation."""
    from cognitive_memory_api import CognitiveMemory, GoalPriority
    async with CognitiveMemory.connect(migrated_dsn, min_size=1, max_size=2) as api:
        title = 'Synthetic first goal ' + str(uuid4())
        gid = await api.create_goal(title, priority=GoalPriority.BACKBURNER)
        assert await api.create_goal(title, priority=GoalPriority.QUEUED) == gid
        async with api._pool.acquire() as conn:
            assert await conn.fetchval('SELECT priority FROM goals WHERE id=$1', gid) == 'queued'
            for path in ('public,ag_catalog', 'ag_catalog,public'):
                async with conn.transaction():
                    await conn.execute('SET LOCAL search_path=' + path)
                    assert await conn.fetchval("SELECT create_goal($1,p_priority=>'queued')", title) == gid
        await api.change_goal_priority(gid, GoalPriority.COMPLETED)
        async with api._pool.acquire() as conn:
            assert await conn.fetchval('SELECT completed_at IS NOT NULL FROM goals WHERE id=$1', gid)


def test_migrations_leave_historical_goals_unchanged(fixture_dsn, fixture_container):
    from bootstrap_fixture import apply_migrations
    with temporary_fixture(fixture_dsn, fixture_container, through='011') as dsn:
        with closing(psycopg2.connect(dsn)) as conn, conn:
            gid = goal(conn, 'completed')
            assert state(conn, gid) == ('completed', None, None, None)
            before = query(conn, 'SELECT to_jsonb(g) FROM goals g ORDER BY id')
        apply_migrations(dsn, after='011', through='013')
        with closing(psycopg2.connect(dsn)) as conn, conn:
            assert query(conn, 'SELECT to_jsonb(g) FROM goals g ORDER BY id') == before
            # AGE-first legacy route also must not invent a completion date.
            query(conn, "SELECT change_goal_priority(%s,'completed')", (gid,))
            assert state(conn, gid) == ('completed', None, None, None)


@pytest.mark.parametrize('first', ['notice', 'close'])
def test_home_supersession_and_close_use_goal_first_lock_order(
        fixture_dsn, fixture_container, monkeypatch, first):
    """Catch memory→goal inversion using the real notice SQL, not manual prelocks.

    Pause the embedding boundary only after supersede_memory has locked its
    predecessor, then start closing. Both transactions must serialize without
    deadlock; a close-first notice must reject without writing a successor.
    """
    import sill
    with temporary_fixture(fixture_dsn, fixture_container, through='013') as dsn:
        with closing(psycopg2.connect(dsn)) as monitor, monitor:
            gid = goal(monitor)
            old = mint(monitor, 'Synthetic lock-order predecessor ' + str(uuid4()))[0]
            link(monitor, old, gid)
            monitor.commit()
            pause_key = uuid4().int % (2**63)
            query(monitor, 'SELECT pg_advisory_lock(%s)', (pause_key,))
            monitor_pid = query(monitor, 'SELECT pg_backend_pid()')[0][0]
            notice_conn, close_conn = psycopg2.connect(dsn), psycopg2.connect(dsn)
            try:
                for conn in (notice_conn, close_conn):
                    query(conn, "SET LOCAL statement_timeout='8s'")
                    query(conn, "SET LOCAL deadlock_timeout='100ms'")
                notice_pid = query(notice_conn, 'SELECT pg_backend_pid()')[0][0]
                close_pid = query(close_conn, 'SELECT pg_backend_pid()')[0][0]
                # The network boundary is a deterministic barrier. All
                # supersession, home-link, closure and archive SQL stays real.
                query(notice_conn, f"""CREATE OR REPLACE FUNCTION public.get_embedding(text_content text)
                    RETURNS vector LANGUAGE plpgsql AS $$ BEGIN
                        PERFORM pg_advisory_xact_lock({pause_key});
                        RETURN array_fill(0.01::real, ARRAY[public.embedding_dimension()])::vector;
                    END $$""")
                monkeypatch.setattr(sill, '_query_db', lambda sql, timeout=60: query(notice_conn, sql))
                def wait_blocked(waiter, blocker):
                    deadline = time.monotonic() + 3
                    while not query(monitor, 'SELECT %s=ANY(pg_blocking_pids(%s))', (blocker, waiter))[0][0]:
                        assert time.monotonic() < deadline, 'expected concurrent lock wait did not occur'
                        time.sleep(.01)
                def notice():
                    try:
                        mid = sill.notice('Synthetic lock-order successor ' + str(uuid4()),
                                          speaker='test', supersedes=old, goal=gid,
                                          minted_in={'via': 'synthetic-lock-order'})
                        notice_conn.commit()
                        return ('ok', mid)
                    except psycopg2.Error as exc:
                        notice_conn.rollback()
                        return (exc.pgcode, str(exc))
                def close():
                    try:
                        query(close_conn, "SELECT public.change_goal_priority(%s,'completed')", (gid,))
                        close_conn.commit()
                        return ('ok', None)
                    except psycopg2.Error as exc:
                        close_conn.rollback()
                        return (exc.pgcode, str(exc))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    if first == 'notice':
                        pending_notice = pool.submit(notice)
                        wait_blocked(notice_pid, monitor_pid)
                        pending_close = pool.submit(close)
                        wait_blocked(close_pid, notice_pid)
                        query(monitor, 'SELECT pg_advisory_unlock(%s)', (pause_key,))
                        noticed, closed = pending_notice.result(timeout=10), pending_close.result(timeout=10)
                        assert noticed[0] == closed[0] == 'ok', (noticed, closed)
                        assert not active(monitor, noticed[1])
                        assert query(monitor, 'SELECT goal_release_owner FROM memories WHERE id=%s', (noticed[1],)) == [(gid,)]
                    else:
                        query(close_conn, "SELECT public.change_goal_priority(%s,'completed')", (gid,))
                        pending_notice = pool.submit(notice)
                        wait_blocked(notice_pid, close_pid)
                        close_conn.commit()
                        noticed = pending_notice.result(timeout=10)
                        assert noticed[0] == '23514' and 'reopen' in noticed[1], noticed
                        assert not query(monitor, 'SELECT id FROM memories WHERE supersedes_id=%s', (old,))
                assert not active(monitor, old)
            finally:
                notice_conn.rollback(); notice_conn.close()
                close_conn.rollback(); close_conn.close()
                query(monitor, 'SELECT pg_advisory_unlock_all()')
