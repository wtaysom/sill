"""Explicitly opted-in disposable PostgreSQL tests. Never fall back to app DSNs.

Set SILL_TEST_DSN and SILL_TEST_ALLOW_DATABASE=1. Only loopback test-named
stores/users on non-production ports are accepted. Individual tests roll back.
The session applies new migrations only to that explicitly selected fixture.
"""
import os
from pathlib import Path

import psycopg2
from psycopg2.extensions import parse_dsn
import pytest

BACKEND = Path(__file__).resolve().parents[2]


def guarded_dsn(dsn, allow):
    if allow != "1":
        raise ValueError("disposable database requires SILL_TEST_ALLOW_DATABASE=1")
    p = parse_dsn(dsn)
    if (p.get("host") not in {"localhost", "127.0.0.1", "::1"}
            or p.get("port", "5432") in {"5432", "15432"}
            or not p.get("dbname", "").startswith(("sill_test_", "sill_v03_test"))
            or p.get("user") != "sill_test"
            or p.get("service") or p.get("options") or p.get("hostaddr")):
        raise ValueError("refusing non-disposable Sill database (host, port, user or name)")
    return dsn


def embedding_stub():
    # Only the network boundary is replaced. Supersession, triggers, subtype and
    # concept writes execute unchanged. Signature matches the real public API.
    return """
    CREATE OR REPLACE FUNCTION public.get_embedding(text_content text)
    RETURNS vector LANGUAGE sql AS $$
      SELECT array_fill(0.01::real, ARRAY[public.embedding_dimension()])::vector
    $$;
    """


@pytest.fixture(scope="session")
def fixture_dsn():
    dsn = os.environ.get("SILL_TEST_DSN")
    if not dsn:
        if os.environ.get("SILL_TEST_REQUIRED") == "1":
            pytest.fail("required integration fixture missing SILL_TEST_DSN")
        pytest.skip("set explicit disposable SILL_TEST_DSN to run integration tests")
    return guarded_dsn(dsn, os.environ.get("SILL_TEST_ALLOW_DATABASE"))


@pytest.fixture(scope="session")
def migrated_dsn(fixture_dsn):
    with psycopg2.connect(fixture_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            assert cur.fetchone() == (parse_dsn(fixture_dsn)["dbname"], "sill_test")
            cur.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations "
                        "(migration_id text PRIMARY KEY, applied_at timestamptz DEFAULT now(), source text)")
            cur.execute("SELECT migration_id FROM public.schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
    # New migrations retain their transaction boundaries. Existing baseline is
    # never dropped/rebuilt. bootstrap_fixture.py creates it in a fresh test DB.
    conn = psycopg2.connect(fixture_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for path in sorted((BACKEND / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                if path.name[:3] >= "008" and path.name[:3] not in applied:
                    cur.execute(path.read_text())
                    cur.execute("INSERT INTO public.schema_migrations(migration_id,source) VALUES(%s,'pytest')",
                                (path.name[:3],))
    finally:
        conn.close()
    return fixture_dsn


@pytest.fixture
def db(migrated_dsn):
    conn = psycopg2.connect(migrated_dsn)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL search_path = ag_catalog, public")
        cur.execute("SET LOCAL statement_timeout = '10s'")
        cur.execute(embedding_stub())
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture(scope="session")
def fixture_container():
    name = os.environ.get("SILL_TEST_CONTAINER")
    if not name:
        if os.environ.get("SILL_TEST_REQUIRED") == "1":
            pytest.fail("required integration bootstrap missing SILL_TEST_CONTAINER")
        pytest.skip("set SILL_TEST_CONTAINER for fresh/upgrade fixture bootstrap")
    from bootstrap_fixture import guarded_container
    return guarded_container(name)


@pytest.fixture(scope="session")
def isolated_store(fixture_dsn, fixture_container):
    from bootstrap_fixture import temporary_fixture
    with temporary_fixture(fixture_dsn, fixture_container) as dsn:
        yield dsn
