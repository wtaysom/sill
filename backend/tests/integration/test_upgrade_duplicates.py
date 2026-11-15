"""Repeated live memories on a real v0.2-level store, and the upgrade's answer.

Migration 010 refuses a store in which one speaker holds the same content
twice. It runs after 008 and 009 have committed, so a refusal there leaves
the schema between releases. `migrate.py` asks the same question in its
read-only preflight; these tests hold the two answers together on a real
database, and check that the explicit archive lets the lane finish.
"""
import importlib.util
import os
from contextlib import closing
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn

from bootstrap_fixture import BACKEND, apply_migrations, temporary_fixture

LAST = sorted(p.name[:3] for p in (BACKEND / "migrations").glob("[0-9][0-9][0-9]_*.sql"))[-1]


def runner():
    spec = importlib.util.spec_from_file_location("migration_runner", BACKEND / "scripts/migrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def repeat(dsn, content, speaker):
    """Store one content twice the way a v0.2 client did.

    Under the store's own search path (ag_catalog, public) the unqualified
    create_memory that v0.2's `sill notice` and MCP wrappers called resolves
    to the copy with no duplicate check, so saying a thing twice kept two
    live rows. Each call commits on its own, so the first is the older.
    """
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            ids = []
            for _ in range(2):
                cur.execute("SELECT create_memory('semantic'::memory_type, %s)", (content,))
                ids.append(str(cur.fetchone()[0]))
            assert ids[0] != ids[1], "this store deduplicated; the premise of these tests changed"
            cur.execute("UPDATE memories SET speaker=%s WHERE id = ANY(%s::uuid[])", (speaker, ids))
            return ids[0], ids[1]


def live(dsn, memory_id):
    with closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT archived_at IS NULL, status::text, archive_reason FROM memories WHERE id=%s", (memory_id,))
        return cur.fetchone()


@pytest.fixture
def v02_store(fixture_dsn, fixture_container):
    with temporary_fixture(fixture_dsn, fixture_container, through="007") as dsn:
        yield dsn, fixture_container


def database(mod, dsn, container):
    return mod.Database(container, parse_dsn(dsn)["user"], parse_dsn(dsn)["dbname"])


def test_preflight_finds_what_migration_010_refuses(v02_store):
    dsn, container = v02_store
    mod = runner()
    keep, newer = repeat(dsn, "repeated " + uuid4().hex, "Inez")
    db = database(mod, dsn, container)
    state = mod.inspect_state(db, BACKEND / "migrations")
    groups = mod.duplicate_groups(db, state)
    assert [[row["id"] for row in group] for group in groups] == [[keep, newer]]
    with pytest.raises(ValueError, match="--archive-duplicates"):
        mod.check_duplicates(groups, False)
    # Left alone, the lane stops inside 010 with 008 and 009 already applied.
    with pytest.raises(psycopg2.Error, match="010 preflight duplicate"):
        apply_migrations(dsn, after="007", through=LAST)
    with closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(migration_id) FROM public.schema_migrations")
        assert cur.fetchone()[0] == "009"


def test_explicit_archive_lets_the_whole_lane_apply(v02_store):
    dsn, container = v02_store
    mod = runner()
    keep, newer = repeat(dsn, "repeated " + uuid4().hex, "Inez")
    other_speaker, _ = repeat(dsn, "another " + uuid4().hex, "")
    db = database(mod, dsn, container)
    state = mod.inspect_state(db, BACKEND / "migrations")
    groups = mod.duplicate_groups(db, state)
    assert len(groups) == 2
    db.apply(mod.archive_duplicates_sql(groups))
    assert mod.duplicate_groups(db, state) == []
    assert live(dsn, keep) == (True, "active", None)
    assert live(dsn, newer) == (False, "archived", f"duplicate of {keep}")
    assert live(dsn, other_speaker)[0] is True
    apply_migrations(dsn, after="007", through=LAST)
    after = mod.inspect_state(db, BACKEND / "migrations")
    assert after["pending"] == [] and mod.duplicate_groups(db, after) == []


def test_same_words_from_two_speakers_are_not_a_duplicate(v02_store):
    dsn, container = v02_store
    mod = runner()
    content = "shared " + uuid4().hex
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for speaker in ("Inez", "Rafael"):
                cur.execute("SELECT create_memory('semantic'::memory_type, %s)", (content,))
                cur.execute("UPDATE memories SET speaker=%s WHERE id=%s", (speaker, cur.fetchone()[0]))
    db = database(mod, dsn, container)
    assert mod.duplicate_groups(db, mod.inspect_state(db, BACKEND / "migrations")) == []
    apply_migrations(dsn, after="007", through=LAST)


def test_a_store_from_before_speakers_is_asked_by_content_alone(fixture_dsn, fixture_container):
    """v0.1 has no speaker column; migration 001 adds it empty, so 010 will
    see every row as one speaker's."""
    mod = runner()
    with temporary_fixture(fixture_dsn, fixture_container, through="000") as dsn:
        content = "before speakers " + uuid4().hex
        with closing(psycopg2.connect(dsn)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                ids = []
                for _ in range(2):
                    cur.execute("SELECT create_memory('semantic'::memory_type, %s)", (content,))
                    ids.append(str(cur.fetchone()[0]))
        db = database(mod, dsn, fixture_container)
        state = mod.inspect_state(db, BACKEND / "migrations")
        assert state["applied"] == []
        groups = mod.duplicate_groups(db, state)
        assert [[row["id"] for row in group] for group in groups] == [ids]
        db.apply(mod.archive_duplicates_sql(groups))
        assert live(dsn, ids[1]) == (False, "archived", f"duplicate of {ids[0]}")
        apply_migrations(dsn, after="000", through=LAST)
