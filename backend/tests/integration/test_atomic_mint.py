"""Atomic speech-act creation against an explicitly disposable real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json
import pytest

from bootstrap_fixture import temporary_fixture
from conftest import BACKEND


def query(db, sql, args=()):
    with db.cursor() as cur:
        cur.execute(sql, args if args else None)
        return cur.fetchall() if cur.description else []


def mint(db, content, speaker=None, **kw):
    args = [kw.pop('type', 'semantic'), content, speaker]
    names = ['p_type', 'p_content', 'p_speaker']
    for key, value in kw.items():
        names.append('p_' + key)
        args.append(Json(value) if isinstance(value, dict) else value)
    return query(db, 'SELECT * FROM public.mint_memory(' + ','.join(n + ' => %s' for n in names) + ')', args)[0]


def snapshot(db, mid):
    return query(db, """SELECT to_jsonb(m),
        (SELECT to_jsonb(s) FROM semantic_memories s WHERE memory_id=m.id),
        (SELECT jsonb_agg(to_jsonb(c) ORDER BY concept_id) FROM memory_concepts c WHERE memory_id=m.id),
        (SELECT jsonb_agg(properties::text::jsonb) FROM memory_graph."MemoryNode"
         WHERE properties::text::jsonb->>'memory_id'=m.id::text)
        FROM memories m WHERE id=%s""", (mid,))[0]


def test_same_speaker_retry_is_immutable(db):
    text = 'An atomic retry preserves the entire original declaration.'
    first = mint(db, text, 'reader', type_metadata={}, concepts=['original'],
                 production={'made': []}, minted_in={'via': 'original'}, force='assertive')
    before = snapshot(db, first[0])
    second = mint(db, text, 'reader', type='episodic', type_metadata={'context': {'changed': True}},
                  concepts=['changed'], production={'made': [{'work': 'changed'}]},
                  source_attribution={'kind': 'changed'}, minted_in={'via': 'changed'}, force='directive')
    assert second == (first[0], 'reused', None)
    assert snapshot(db, first[0]) == before


def test_distinct_speaker_restates_not_supersedes(db):
    first = mint(db, 'The same words from different speakers remain separate.', 'one')
    second = mint(db, 'The same words from different speakers remain separate.', 'two')
    assert second[0] != first[0] and second[2] == first[0]
    # Both rows have transaction-start created_at; UUID breaks that tie.
    third = mint(db, 'The same words from different speakers remain separate.', 'three')
    assert third[2] == min(first[0], second[0])
    assert query(db, 'SELECT archived_at,supersedes_id FROM memories WHERE id=ANY(%s::uuid[])', ([first[0], second[0]],)) == [(None, None), (None, None)]


@pytest.mark.parametrize('speakers', [(None, ''), ('one', 'two')])
def test_concurrent_mints_one_complete_act_or_one_restatement(migrated_dsn, speakers):
    barrier = Barrier(2)
    content = str(uuid4()) + ' concurrent content ' * 1200
    def run(i):
        with psycopg2.connect(migrated_dsn) as conn:
            barrier.wait(timeout=10)
            return mint(conn, content, speakers[i], type_metadata={}, concepts=['concurrent'],
                        production={'made': [{'work': str(i)}]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(run, (0, 1)))
    with psycopg2.connect(migrated_dsn) as conn:
        ids = {a[0], b[0]}
        if speakers[0] is None:
            assert len(ids) == 1 and {a[1], b[1]} == {'inserted', 'reused'}
        else:
            assert len(ids) == 2 and sum(r[2] is not None for r in (a, b)) == 1
            assert next(r[2] for r in (a, b) if r[2]) in ids
        for mid in ids:
            row, subtype, concepts, nodes = snapshot(conn, mid)
            assert subtype and len(concepts) == len(nodes) == 1
            winner = next(i for i, r in enumerate((a, b)) if r[0] == mid and r[1] == 'inserted')
            assert row['production']['made'][0]['work'] == str(winner)
            assert row['speaker'] == speakers[winner]


@pytest.mark.parametrize('kind', ['semantic', 'episodic', 'procedural', 'strategic'])
@pytest.mark.parametrize('metadata', [None, Json(None), {}])
def test_optional_type_metadata_preserves_bare_vs_typed(db, kind, metadata):
    mid = mint(db, str(uuid4()) + ' optional subtype record', type=kind, type_metadata=metadata)[0]
    rows = query(db, f'SELECT * FROM {kind}_memories WHERE memory_id=%s', (mid,))
    assert bool(rows) == (metadata == {})
    if kind == 'semantic' and metadata == {}:
        assert query(db, 'SELECT confidence FROM semantic_memories WHERE memory_id=%s', (mid,))[0][0] == .8
    if kind == 'procedural' and metadata == {}:
        assert query(db, 'SELECT steps FROM procedural_memories WHERE memory_id=%s', (mid,))[0][0] == {'steps': []}


def test_batch_order_and_transaction_rollback(db):
    content = 'Batch repeats must not acquire concepts from later entries.'
    items = [{'type': 'semantic', 'content': content, 'type_metadata': {}, 'concepts': ['first']},
             {'type': 'episodic', 'content': content, 'concepts': ['later']}]
    rows = query(db, 'SELECT * FROM public.mint_memories(%s)', (Json(items),))
    assert [r[0] for r in rows] == [1, 2] and rows[0][1] == rows[1][1]
    assert [r[2] for r in rows] == ['inserted', 'reused']
    assert len(snapshot(db, rows[0][1])[2]) == 1
    before = query(db, 'SELECT count(*) FROM memories')[0]
    nodes = query(db, 'SELECT count(*) FROM memory_graph."MemoryNode"')[0]
    query(db, 'SAVEPOINT invalid_batch')
    with pytest.raises(psycopg2.Error):
        query(db, 'SELECT * FROM public.mint_memories(%s)', (Json([
            {'type': 'semantic', 'content': 'Rollback all parts of this newly minted row.', 'type_metadata': {}, 'concepts': ['rollback-only']},
            {'type': 'semantic', 'content': 'later bad metadata', 'production': []}]),))
    query(db, 'ROLLBACK TO SAVEPOINT invalid_batch')
    assert query(db, 'SELECT count(*) FROM memories')[0] == before
    assert query(db, 'SELECT count(*) FROM memory_graph."MemoryNode"')[0] == nodes
    assert not query(db, "SELECT 1 FROM concepts WHERE name='rollback-only'")


def test_supersede_unchanged_content_archives_before_insert(db):
    content = 'Unchanged words can have a new explicitly declared successor.'
    old = mint(db, content, 'same', type_metadata={}, production={'made': []})[0]
    new = query(db, 'SELECT public.supersede_memory(%s,%s,NULL,%s)', (old, content, Json({'speaker': 'same'})))[0][0]
    assert new != old
    assert query(db, 'SELECT archived_at IS NOT NULL FROM memories WHERE id=%s', (old,)) == [(True,)]
    assert len(snapshot(db, new)[3]) == 1
    query(db, 'SAVEPOINT invalid_successor')
    with pytest.raises(psycopg2.Error):
        query(db, 'SELECT public.supersede_memory(%s,%s,NULL,%s)', (new, content, Json({'speaker': 'same', 'force': 'bad'})))
    query(db, 'ROLLBACK TO SAVEPOINT invalid_successor')
    assert query(db, 'SELECT archived_at FROM memories WHERE id=%s', (new,)) == [(None,)]


def test_duplicate_live_mouth_preflight_changes_nothing(fixture_dsn, fixture_container):
    migration = BACKEND / 'migrations/010_atomic_mint.sql'
    assert migration.exists(), 'atomic mint migration is missing'
    with temporary_fixture(fixture_dsn, fixture_container) as dsn:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        try:
            ids = query(conn, "INSERT INTO memories(type,content,speaker,embedding) VALUES ('semantic','duplicate','x',public.get_embedding('duplicate')),('semantic','duplicate','x',public.get_embedding('duplicate')) RETURNING id")
            before = query(conn, 'SELECT to_jsonb(m) FROM memories m ORDER BY id')
            with pytest.raises(psycopg2.Error) as error:
                query(conn, migration.read_text())
            assert error.value.pgcode == '55000'
            assert all(str(row[0]) in str(error.value) for row in ids)
            query(conn, 'ROLLBACK')
            assert query(conn, 'SELECT to_jsonb(m) FROM memories m ORDER BY id') == before
            assert query(conn, "SELECT to_regprocedure('public.mint_memory(memory_type,text,double precision,jsonb,text,text,jsonb,jsonb,jsonb,text[],double precision)')") == [(None,)]
        finally:
            conn.close()


def test_legacy_namespace_guard_and_speaker_scope(db):
    text = 'A legacy caller cannot adopt another speaker utterance.'
    spoken = mint(db, text, 'speaker')[0]
    legacy = query(db, "SELECT public.create_memory('semantic',%s)", (text,))[0][0]
    assert legacy != spoken
    assert query(db, "SELECT ag_catalog.create_memory('semantic',%s)", (text,))[0][0] == legacy
    assert query(db, "SELECT public.create_memory('semantic','short')")[0][0]
    query(db, 'SAVEPOINT short_guard')
    with pytest.raises(psycopg2.Error, match='too short'):
        query(db, "SELECT ag_catalog.create_memory('semantic','short')")
    query(db, 'ROLLBACK TO SAVEPOINT short_guard')


@pytest.mark.parametrize('kind,args', [
    ('semantic', ',0.8'), ('episodic', ''), ('procedural', ",'{\"steps\":[]}'::jsonb"),
    ('strategic', ",'pattern',0.8")])
def test_legacy_typed_wrappers_reuse_without_duplicate_subtype(db, kind, args):
    text = 'Legacy typed wrapper remains complete and immutable: ' + kind
    sql = f'SELECT public.create_{kind}_memory(%s{args})'
    first = query(db, sql, (text,))[0][0]
    assert query(db, sql, (text,))[0][0] == first
    assert len(query(db, f'SELECT * FROM {kind}_memories WHERE memory_id=%s', (first,))) == 1
    assert len(snapshot(db, first)[3]) == 1


def test_precomputed_batch_atomic_and_dimension_checked(db):
    dim = query(db, 'SELECT public.embedding_dimension()')[0][0]
    vec = [0.02] * dim
    # A supplied-vector route must never call the embedding service.
    query(db, "CREATE OR REPLACE FUNCTION public.get_embedding(text_content text) RETURNS vector LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'network forbidden'; END $$")
    text = 'A supplied vector batch preserves legacy typed defaults.'
    sql = "SELECT public.batch_create_memories_with_embeddings('semantic',%s,%s)::text[]"
    ids = query(db, sql, ([text, text], Json([vec, vec])))[0][0]
    assert len(ids) == 2 and ids[0] == ids[1]
    assert query(db, 'SELECT confidence FROM semantic_memories WHERE memory_id=%s', (ids[0],)) == [(0.8,)]
    assert query(db, 'SELECT embedding::text FROM memories WHERE id=%s', (ids[0],))[0][0].startswith('[0.02,')
    count = query(db, 'SELECT count(*) FROM memories')
    for invalid in ([vec, vec[:-1]], [vec, [float('inf')] * dim], [vec]):
        query(db, 'SAVEPOINT invalid_vector')
        with pytest.raises(psycopg2.Error):
            query(db, sql, (['First raw row must roll back on a later bad vector.', 'bad vector'], Json(invalid)))
        query(db, 'ROLLBACK TO SAVEPOINT invalid_vector')
        assert query(db, 'SELECT count(*) FROM memories') == count
    query(db, 'SAVEPOINT explicit_profile')
    with pytest.raises(psycopg2.Error, match='profile'):
        query(db, "SELECT * FROM public.mint_memory_with_embedding('semantic',%s,%s::vector,p_embedding_profile=>'unsupported')", (text, str(vec)))
    query(db, 'ROLLBACK TO SAVEPOINT explicit_profile')


def test_concurrent_supersession_has_one_winner(migrated_dsn):
    content = str(uuid4()) + ' concurrent supersession predecessor'
    with psycopg2.connect(migrated_dsn) as conn:
        old = mint(conn, content, 'one', type_metadata={})[0]
    barrier = Barrier(2)
    def run(i):
        with psycopg2.connect(migrated_dsn) as conn:
            barrier.wait(timeout=10)
            try:
                return query(conn, 'SELECT public.supersede_memory(%s,%s,NULL,%s)',
                             (old, content, Json({'speaker': 'one'})))[0][0]
            except psycopg2.Error as e:
                assert 'already archived' in str(e)
                conn.rollback()
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    assert sum(r is not None for r in results) == 1


def test_hash_collision_is_explicit_and_does_not_reuse_different_text(db):
    # PostgreSQL dispatches built-in md5 by OID even after a body replacement.
    # Inject a digest seam only into this transactional fixture's core and index,
    # preserving the full mint/uniqueness code and all non-fixture content hashes.
    query(db, """CREATE FUNCTION public.fixture_digest(text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT CASE WHEN $1 IN
        ('First words in the collision fixture.','Different words with the same synthetic digest.')
        THEN 'synthetic-collision' ELSE md5($1) END $$""")
    definition = query(db, "SELECT pg_get_functiondef('public._mint_memory(memory_type,text,double precision,jsonb,text,text,jsonb,jsonb,jsonb,text[],double precision,vector,text,boolean)'::regprocedure)")[0][0]
    query(db, definition.replace('md5(', 'public.fixture_digest('))
    query(db, "DROP INDEX idx_memories_live_mouth_content")
    query(db, "CREATE UNIQUE INDEX idx_memories_live_mouth_content ON memories((coalesce(speaker,'')),public.fixture_digest(content)) WHERE archived_at IS NULL")
    first = mint(db, 'First words in the collision fixture.', 'collision')[0]
    before = snapshot(db, first)
    query(db, 'SAVEPOINT collision')
    with pytest.raises(psycopg2.Error, match='hash collision'):
        mint(db, 'Different words with the same synthetic digest.', 'collision')
    query(db, 'ROLLBACK TO SAVEPOINT collision')
    assert snapshot(db, first) == before


def test_semantic_source_normalization_and_trust_match_legacy(db):
    source = {'kind': 'document', 'uri': 'https://example.test/source', 'trust': .9}
    mid = mint(db, 'A sourced semantic record normalizes references and trust.',
               type_metadata={'source_references': [source, source]})[0]
    rows = query(db, '''SELECT source_attribution,source_references,trust_level,
        public.compute_semantic_trust(s.confidence,s.source_references,0.0)
        FROM memories m JOIN semantic_memories s ON m.id=s.memory_id WHERE m.id=%s''', (mid,))
    attribution, refs, trust, expected = rows[0]
    assert len(refs) == 1 and refs[0]['ref'] == source['uri']
    assert attribution['ref'] == source['uri'] and trust == expected


@pytest.mark.asyncio
async def test_python_typed_batch_and_raw_routes_are_atomic(migrated_dsn):
    from cognitive_memory_api import CognitiveMemory, MemoryInput, MemoryType
    from uuid import UUID
    async with CognitiveMemory.connect(migrated_dsn, min_size=1, max_size=2) as api:
        prefix = str(uuid4())
        first = await api.remember(prefix + ' typed memory', type=MemoryType.SEMANTIC, concepts=['first'])
        assert isinstance(first, UUID)
        assert await api.remember(prefix + ' typed memory', type=MemoryType.EPISODIC, concepts=['later']) == first
        batch = await api.remember_batch([
            MemoryInput(content=prefix + ' batch memory', type=MemoryType.SEMANTIC, concepts=['batch']),
            MemoryInput(content=prefix + ' batch memory', type=MemoryType.SEMANTIC, concepts=['later'])])
        assert batch[0] == batch[1] and isinstance(batch[0], UUID)
        async with api._pool.acquire() as conn:
            assert await conn.fetchval('SELECT count(*) FROM memory_concepts WHERE memory_id=$1', first) == 1
            assert await conn.fetchval('SELECT count(*) FROM memory_concepts WHERE memory_id=$1', batch[0]) == 1
            assert await conn.fetchval('SELECT confidence FROM semantic_memories WHERE memory_id=$1', first) == .8
            dim = await conn.fetchval('SELECT embedding_dimension()')
            before = await conn.fetchval('SELECT count(*) FROM memories')
        for vectors in ([[0.01] * dim, [0.01] * (dim-1)], [[0.01] * dim, [float('nan')] * dim]):
            with pytest.raises(ValueError):
                await api.remember_batch_raw([prefix+' raw first', prefix+' raw second'], vectors)
        with pytest.raises(Exception):
            await api.remember_batch([MemoryInput(content=prefix+' rollback', concepts=['rollback-api']), MemoryInput(content='')])
        async with api._pool.acquire() as conn:
            assert await conn.fetchval('SELECT count(*) FROM memories') == before
        raw = await api.remember_batch_raw([prefix+' raw valid',prefix+' raw valid'], [[0.01]*dim]*2)
        assert raw[0] == raw[1] and isinstance(raw[0], UUID)
        async with api._pool.acquire() as conn:
            assert json.loads(await conn.fetchval('SELECT context FROM episodic_memories WHERE memory_id=$1', raw[0])) == {'type': 'raw_batch'}


def test_notice_and_decompose_routes_do_not_retag_or_extend_reused_rows(db, monkeypatch):
    import sill
    from scripts import decompose_event
    def real_query(sql, timeout=60):
        return query(db, sql)
    monkeypatch.setattr(sill, '_query_db', real_query)
    words = 'The CLI emits the entire act in its single SQL statement.'
    one = sill.notice(words, speaker='one', concepts=['one'])
    two = sill.notice(words, speaker='two', concepts=['two'])
    assert one != two
    before = snapshot(db, one)
    assert sill.notice(words, speaker='one', concepts=['later'], force='directive') == one
    assert snapshot(db, one) == before
    sql = decompose_event.build_sql({'speaker': 'one', 'acts': [{'content': words, 'force': 'commissive'}]})
    # Keep the test rollback scope; production SQL includes BEGIN/COMMIT.
    query(db, sql.removeprefix('BEGIN;').removesuffix('COMMIT;'))
    assert snapshot(db, one) == before


@pytest.mark.parametrize('fake', [False, True])
def test_seed_import_uses_atomic_mint_and_never_adds_subtypes_on_reuse(db, fake):
    from scripts import seed_import
    content = 'A seed retry must preserve a preexisting bare record.'
    mid = mint(db, content)[0]
    before = snapshot(db, mid)
    creator = seed_import._insert_with_fake_embedding if fake else seed_import._insert_via_create_memory
    with db.cursor() as cur:
        reused = creator(cur, mem_type='semantic', content=content, importance=.9, source_attribution={'kind': 'seed'})
    assert reused == mid and snapshot(db, mid) == before


def test_atomic_callable_namespaces_fresh_and_upgrade(fixture_dsn, fixture_container):
    from bootstrap_fixture import apply_migrations
    from test_function_contracts import catalog, contract_only, MANIFEST
    manifest = json.loads(MANIFEST.read_text())
    names = manifest.get('atomic_function_names')
    assert names, 'pin the M2 callable namespace/overload contract'
    snapshots = []
    for through in ('007', '010'):
        with temporary_fixture(fixture_dsn, fixture_container, through=through) as dsn:
            if through == '007':
                apply_migrations(dsn, after='007', through='010')
            conn = psycopg2.connect(dsn)
            try:
                snapshots.append(catalog(conn, names))
                assert contract_only(snapshots[-1]) == manifest['v03_m2']
                # An explicit public-first or AGE-first path must find the same
                # atomic creators, with no stale overload bypassing the contract.
                for path in ('public,ag_catalog', 'ag_catalog,public'):
                    query(conn, 'SET LOCAL search_path = ' + path)
                    words = 'Callable legacy batch with immutable ordered retries: ' + path
                    items = [{'type': 'semantic', 'content': words}]*2
                    ids = query(conn, 'SELECT batch_create_memories(%s)::text[]', (Json(items),))[0][0]
                    assert ids[0] == ids[1]
                    dim = query(conn, 'SELECT embedding_dimension()')[0][0]
                    vector_id = query(conn, "SELECT create_memory_with_embedding('semantic',%s,%s::vector)", (words, str([0.01]*dim)))[0][0]
                    assert vector_id == ids[0]
            finally:
                conn.rollback()
                conn.close()
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize('kind', ['episodic', 'semantic', 'procedural', 'strategic'])
def test_raw_batch_legacy_subtype_defaults_only_on_insert(db, kind):
    dim = query(db, 'SELECT embedding_dimension()')[0][0]
    vec = [0.01]*dim
    bare_words = 'Existing bare row stays bare when a raw batch repeats it: ' + kind
    bare = mint(db, bare_words, type=kind)[0]
    before = snapshot(db, bare)
    new_words = 'New raw batch row gets the legacy subtype defaults: ' + kind
    ids = query(db, 'SELECT public.batch_create_memories_with_embeddings(%s,%s,%s)::text[]',
                (kind, [bare_words,new_words], Json([vec,vec])))[0][0]
    assert ids[0] == bare and snapshot(db, bare) == before
    row = query(db, f'SELECT to_jsonb(s) FROM {kind}_memories s WHERE memory_id=%s', (ids[1],))[0][0]
    if kind == 'episodic':
        assert row['context'] == {'type':'raw_batch'} and row['emotional_valence'] == 0.0 and row['event_time']
    elif kind == 'semantic':
        assert row['confidence'] == .8 and row['source_references'] == [] and row['last_validated']
    elif kind == 'procedural':
        assert row['steps'] == {'steps':[]}
    else:
        assert row['pattern_description'] == new_words and row['confidence_score'] == .8
    # The NEW generic vector mint, unlike the established batch API, is bare.
    direct = query(db, "SELECT memory_id FROM public.mint_memory_with_embedding(%s,%s,%s::vector,p_type_metadata=>'null'::jsonb)",
                   (kind, 'Direct vector bare ' + kind, str(vec)))[0][0]
    assert not query(db, f'SELECT 1 FROM {kind}_memories WHERE memory_id=%s', (direct,))


@pytest.mark.parametrize('kind,args,fields', [
    ('episodic', '', ['action_taken','context','result']),
    ('procedural', ",'{\"steps\":[]}'::jsonb", ['prerequisites']),
    ('strategic', ",'pattern',0.8", ['supporting_evidence','context_applicability']),
])
def test_typed_wrapper_optional_json_fields_remain_sql_null(db, kind, args, fields):
    mid = query(db, f'SELECT public.create_{kind}_memory(%s{args})',
                ('Optional SQL-null defaults in typed wrapper: ' + kind,))[0][0]
    values = query(db, 'SELECT ' + ','.join(f'{field} IS NULL' for field in fields)
                   + f' FROM {kind}_memories WHERE memory_id=%s', (mid,))[0]
    assert all(values)


@pytest.mark.parametrize('author', [None, 'fixture-pack'])
def test_seed_precheck_is_scoped_to_its_null_speaker(db, author):
    from scripts import seed_import
    words = 'Seed precheck must not suppress another speaker: ' + str(author)
    source = {'kind': 'seed'} if author is None else {'kind': 'seed', 'author': author}
    mint(db, words, 'another speaker', source_attribution=source)
    with db.cursor() as cur:
        assert not seed_import._already_present(cur, words, author)
    mint(db, words, source_attribution=source)
    with db.cursor() as cur:
        assert seed_import._already_present(cur, words, author)
