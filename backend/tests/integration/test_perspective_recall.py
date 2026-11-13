"""Synthetic attribution round trips against an explicitly disposable store."""
from contextlib import asynccontextmanager, closing
import json
from uuid import uuid4

import asyncpg
import psycopg2
import pytest

from bootstrap_fixture import temporary_fixture, apply_migrations
from cognitive_memory_api import CognitiveMemory, MemoryInput, MemoryType, _init_connection
import sill
import sill_mcp_server as mcp

PRODUCTION = {'made':[{'kind':'person','who':'Inez','work':'experiment','at':[],
                     'declared':{'by':None,'when':'W','basis':'stated'}}]}
STAMP = {'session':'synthetic-host','as':'Field Guide','when':'W','via':'test','model':None}

@asynccontextmanager
async def client_in_transaction(dsn):
    conn = await asyncpg.connect(dsn)
    await _init_connection(conn)
    tx = conn.transaction(); await tx.start()
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn
    try:
        yield CognitiveMemory(Pool()), conn
    finally:
        await tx.rollback(); await conn.close()

@pytest.mark.asyncio
async def test_production_is_optional_end_to_end(migrated_dsn, monkeypatch):
    async with client_in_transaction(migrated_dsn) as (client, conn):
        for declaration in (None, PRODUCTION):
            word = 'perspectiveroundtrip' + uuid4().hex
            mid = await client.remember(word, type=MemoryType.SEMANTIC, production=declaration,
                      minted_in=STAMP, speaker='Inez', force='assertive', concepts=[word, 'survey'])
            byid = await client.recall_by_id(mid)
            assert byid.production == declaration and byid.minted_in == STAMP
            assert byid.speaker == 'Inez' and byid.force == 'assertive'
            assert byid.restates_id is None
            recalled = await client.recall(word, include_partial=False, speaker='Inez')
            hydrated = await client.hydrate(word, include_partial=False)
            previews = await client.recall_preview(word)
            for rows in (recalled.memories, hydrated.memories, previews.previews,
                         await client.recall_recent(limit=100), await client.recall_batch([mid]),
                         await client.find_by_concept(word)):
                record = next(row for row in rows if row.id == mid)
                assert record.production == declaration and record.minted_in == STAMP
                assert record.speaker == 'Inez' and record.force == 'assertive'
            for rows in (await client.trace_trajectory(word), await client.position_on(word),
                         await client.gather_pattern(word), await client.relate_concepts(word, 'survey'),
                         await client.follow_supersedes(mid)):
                record = next(row for row in rows if row['id'] == str(mid))
                assert record['production'] == declaration and record['minted_in'] == STAMP
            rendered = mcp._jsonable(await mcp._dispatch_tool(client, 'recall_by_id', {'memory_id':str(mid)}))
            assert rendered['production'] == declaration and 'score' in rendered and 'similarity' not in rendered
            made = await mcp._dispatch_tool(client,'remember',{'content':word+' mcp','speaker':'Inez',
                          'production':declaration,'minted_in':STAMP})
            assert (await client.recall_by_id(made)).production == declaration
            batch = await mcp._dispatch_tool(client,'remember_batch',{'memories':[{'content':word+' batch',
                          'speaker':'Inez','production':declaration,'minted_in':STAMP}]})
            assert (await client.recall_by_id(batch[0])).production == declaration

@pytest.mark.asyncio
async def test_perspective_filter_before_limit(migrated_dsn):
    async with client_in_transaction(migrated_dsn) as (client, conn):
        # Other integration tests may commit synthetic rows; isolate the ranking universe.
        await conn.execute('UPDATE memories SET archived_at=now()')
        keyword = 'perspectivefilter' + uuid4().hex
        # Exact top FTS competitors crowd the other speaker out before LIMIT 4.
        for i in range(20):
            await client.remember((keyword+' ')*20+str(i), speaker='crowd', minted_in=STAMP)
        target = await client.remember(keyword+' background '*50, speaker='Inez', minted_in=STAMP)
        dimension = await conn.fetchval('SELECT embedding_dimension()')
        distant = [0.01] * dimension; distant[0] = 0.99
        await conn.execute('UPDATE memories SET embedding=$1::text::vector WHERE id=$2', str(distant), target)
        global_rows = await conn.fetch('SELECT m.speaker FROM hybrid_recall($1,1) r JOIN memories m ON m.id=r.memory_id', keyword)
        assert [row['speaker'] for row in global_rows] == ['crowd']
        nobody = await client.remember(keyword+' anonymous', speaker=None, minted_in=STAMP)
        await client.remember(keyword+' reserved', speaker='#Nobody', minted_in=STAMP)
        archived = await client.remember(keyword+' archived', speaker='archived-only', minted_in=STAMP)
        await conn.execute('UPDATE memories SET archived_at=now() WHERE id=$1', archived)
        for namespace in ('public', 'ag_catalog'):
            for speaker, expected in [('Inez', target), ('#Nobody', nobody)]:
                rows = await conn.fetch(f'SELECT * FROM {namespace}.hybrid_recall_perspective($1,1,$2)',keyword,speaker)
                assert [row['memory_id'] for row in rows] == [expected]
                assert rows[0]['score'] == pytest.approx(2/61)
        result = await client.recall(keyword, limit=1, speaker='Inez', include_partial=False)
        assert [r.id for r in result.memories] == [target]
        speakers = dict(await client.speakers())
        assert speakers['Inez'] == 1 and speakers['crowd'] == 20
        assert '#Nobody' not in speakers and 'archived-only' not in speakers
        assert await client.speakers(min_count=20, limit=1) == [('crowd',20)]


def test_cli_production_optional_and_declared(db, monkeypatch):
    monkeypatch.setattr(sill, 'mint_context', lambda via: STAMP)
    def query(sql, timeout=60):
        with db.cursor() as cur:
            cur.execute(sql)
            return [tuple(str(x) if x is not None else '' for x in row) for row in cur.fetchall()]
    monkeypatch.setattr(sill, '_query_db', query)
    for decl in (None, PRODUCTION):
        mid = sill.notice('CLI synthetic '+uuid4().hex, speaker='Inez', production=decl)
        with db.cursor() as cur:
            cur.execute('SELECT production,minted_in FROM memories WHERE id=%s',(mid,))
            assert cur.fetchone() == (decl,STAMP)
    with pytest.raises(ValueError):
        sill.notice('invalid '+uuid4().hex, production={'made':'bad'})

@pytest.mark.parametrize('upgrade', [False, True])
def test_perspective_fresh_and_upgrade(fixture_dsn, fixture_container, upgrade):
    with temporary_fixture(fixture_dsn, fixture_container, through='010' if upgrade else '011') as dsn:
        if upgrade:
            apply_migrations(dsn, through='011', after='010')
        with closing(psycopg2.connect(dsn)) as conn:
            with conn.cursor() as cur:
                for namespace in ('public','ag_catalog'):
                    cur.execute(f'SELECT * FROM {namespace}.hybrid_recall_perspective(%s,1,%s)',('synthetic','absent-speaker'))
                    assert cur.fetchall() == []
