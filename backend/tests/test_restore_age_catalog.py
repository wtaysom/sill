"""Opt-in real AGE restore regression: explicit disposable Docker container only.

SILL_RESTORE_TEST_CONTAINER=<immutable ID> pytest tests/test_restore_age_catalog.py
Creates uniquely named databases and retains them for inspection. Never uses Sill
instance configuration or an existing database as a write target.
"""
import os
from pathlib import Path
import subprocess
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = os.environ.get('SILL_RESTORE_TEST_CONTAINER')
pytestmark = pytest.mark.skipif(not CONTAINER, reason='requires explicit isolated AGE test container')


def psql(db, sql, *args):
    return subprocess.run(['docker', 'exec', '-i', CONTAINER, 'psql', '-X', '-U',
                           'sill', '-d', db, '-v', 'ON_ERROR_STOP=1', '-qAt', *args],
                          input=sql, text=True, capture_output=True)


def good(db, sql, *args):
    result = psql(db, sql, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def new_db():
    db = 'sill_test_restore_' + uuid.uuid4().hex[:14]
    good('postgres', f'CREATE DATABASE {db} TEMPLATE template0;')
    return db


@pytest.fixture(scope='module')
def dump():
    source = new_db()
    good(source, """CREATE EXTENSION age; LOAD 'age';
SET search_path=ag_catalog,public;
SELECT create_graph('memory_graph');
SELECT create_graph('extra_graph');
SELECT * FROM cypher('memory_graph', $$CREATE (a:MemoryNode {memory_id:'one'}),
(b:MemoryNode {memory_id:'two'}), (a)-[:SUPPORTS {confidence:0.91}]->(b)$$) AS (v agtype);
CREATE TABLE public.preserved (id integer PRIMARY KEY, provenance text, payload jsonb);
INSERT INTO public.preserved VALUES (7, NULL, '{"historical":true}');""")
    result = subprocess.run(['docker', 'exec', CONTAINER, 'pg_dump', '-U', 'sill', '-d', source],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def restored(dump):
    db = new_db()
    good(db, dump, '--single-transaction')
    return db


def snapshot(db):
    return good(db, """SET search_path=ag_catalog,public;
SELECT jsonb_build_object('graphs',(SELECT jsonb_agg(to_jsonb(g) ORDER BY name) FROM ag_graph g),
'labels',(SELECT jsonb_agg(to_jsonb(l) ORDER BY graph,id) FROM ag_label l),
'constraints',(SELECT jsonb_agg(to_jsonb(c) ORDER BY oid) FROM pg_constraint c
 WHERE conrelid IN ('ag_graph'::regclass,'ag_label'::regclass)));""")


def payload(db):
    return good(db, """SET search_path=ag_catalog,public;
SELECT jsonb_build_object('nodes',(SELECT jsonb_agg(to_jsonb(n) ORDER BY id) FROM memory_graph.\"MemoryNode\" n),
'edges',(SELECT jsonb_agg(to_jsonb(e) ORDER BY id) FROM memory_graph.\"SUPPORTS\" e),
'ordinary',(SELECT jsonb_agg(to_jsonb(p)) FROM public.preserved p));""")


def repair(db, expected=None, ack='yes'):
    return psql(db, (ROOT/'scripts/restore_age_catalog.sql').read_text(),
                '-v', 'restore_database=' + (expected or db), '-v', 'isolated_restore=' + ack)


def test_dump_restore_repairs_real_cypher_and_preserves_data(restored):
    """Missing either OID update breaks Cypher; data reconstruction loses rows/IDs."""
    before = payload(restored)
    result = repair(restored)
    assert result.returncode == 0, result.stderr
    got = good(restored, """LOAD 'age'; SET search_path=ag_catalog,public;
SELECT * FROM cypher('memory_graph', $$MATCH (a:MemoryNode)-[e:SUPPORTS]->(b:MemoryNode)
RETURN a.memory_id, b.memory_id, e.confidence$$) AS (a agtype,b agtype,c agtype);""")
    assert got == '"one"|"two"|0.91'
    assert payload(restored) == before
    first = snapshot(restored)
    assert repair(restored).returncode == 0
    assert snapshot(restored) == first


@pytest.mark.parametrize('expected,ack', [('wrong_database','yes'), (None,'no')])
def test_wrong_target_or_ack_refuses_without_changes(restored, expected, ack):
    before = snapshot(restored)
    result = repair(restored, expected, ack)
    assert result.returncode != 0
    assert snapshot(restored) == before


def test_unknown_catalog_layout_refuses_before_changes(restored):
    good(restored, 'ALTER TABLE ag_catalog.ag_graph ADD COLUMN unexpected integer;')
    before = snapshot(restored)
    result = repair(restored)
    assert result.returncode != 0
    assert 'layout' in result.stderr
    assert snapshot(restored) == before


def test_relation_outside_graph_refuses_without_changes(restored):
    good(restored, 'ALTER TABLE memory_graph."MemoryNode" SET SCHEMA public;')
    before = snapshot(restored)
    result = repair(restored)
    assert result.returncode != 0
    assert 'relation' in result.stderr
    assert snapshot(restored) == before


def test_transaction_failure_rolls_back_oids_and_constraint_flags(restored):
    good(restored, """CREATE FUNCTION public.stop_repair() RETURNS trigger LANGUAGE plpgsql AS
$$BEGIN RAISE EXCEPTION 'synthetic repair failure'; END$$;
CREATE TRIGGER stop_repair AFTER UPDATE ON ag_catalog.ag_graph FOR EACH ROW EXECUTE FUNCTION public.stop_repair();""")
    before = snapshot(restored)
    result = repair(restored)
    assert result.returncode != 0
    assert snapshot(restored) == before


def test_oid_collision_refuses_before_mutation(restored):
    # Put extra_graph's old ID on memory_graph's target OID. The FK remains valid.
    good(restored, """BEGIN;
ALTER TABLE ag_catalog.ag_label ALTER CONSTRAINT fk_graph_oid DEFERRABLE;
SET CONSTRAINTS ALL DEFERRED;
UPDATE ag_catalog.ag_label SET graph=(SELECT namespace::oid FROM ag_catalog.ag_graph WHERE name='memory_graph')
 WHERE graph=(SELECT graphid FROM ag_catalog.ag_graph WHERE name='extra_graph');
UPDATE ag_catalog.ag_graph SET graphid=(SELECT namespace::oid FROM ag_catalog.ag_graph WHERE name='memory_graph') WHERE name='extra_graph';
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE ag_catalog.ag_label ALTER CONSTRAINT fk_graph_oid NOT DEFERRABLE;
COMMIT;""")
    before = snapshot(restored)
    result = repair(restored)
    assert result.returncode != 0
    assert 'collision' in result.stderr
    assert snapshot(restored) == before


def test_alternate_fk_semantics_refuse_unchanged(restored):
    good(restored, 'ALTER TABLE ag_catalog.ag_label ALTER CONSTRAINT fk_graph_oid DEFERRABLE INITIALLY DEFERRED;')
    before = snapshot(restored)
    result = repair(restored)
    assert result.returncode != 0
    assert 'foreign key' in result.stderr
    assert snapshot(restored) == before


def test_fresh_session_verification_matches_all_graph_rows(restored):
    assert repair(restored).returncode == 0
    result = psql(restored, (ROOT/'scripts/verify_age_restore.sql').read_text())
    assert result.returncode == 0, result.stderr
    assert 'Graph rows verified' in result.stdout


def test_fresh_session_verification_rejects_broken_edge(restored):
    assert repair(restored).returncode == 0
    # Direct SQL can create a dangling edge which Cypher traversal cannot recover.
    good(restored, """UPDATE memory_graph."SUPPORTS" SET end_id='844424930139999'::ag_catalog.graphid;""")
    result = psql(restored, (ROOT/'scripts/verify_age_restore.sql').read_text())
    assert result.returncode != 0


def test_detached_label_refuses_before_changes(restored):
    good(restored, 'ALTER TABLE memory_graph."MemoryNode" NO INHERIT memory_graph._ag_label_vertex;')
    before = snapshot(restored)
    result = repair(restored)
    assert result.returncode != 0
    assert 'inheritance' in result.stderr
    assert snapshot(restored) == before
