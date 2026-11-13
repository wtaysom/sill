"""Bootstrap may write only to the newly created, verified fixture target."""
from contextlib import contextmanager
from subprocess import CompletedProcess
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest

import bootstrap_fixture as bootstrap


def exists(dsn, name):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)", (name,))
            return cur.fetchone()[0]
    finally:
        conn.close()


@contextmanager
def fixture_database_name(dsn):
    name = "sill_test_" + uuid4().hex
    try:
        yield name
    finally:
        # This test owns the unique name. Cleanup remains necessary when testing
        # a broken initializer that leaks its newly created database.
        if exists(dsn, name):
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
            finally:
                conn.close()


def test_identity_mismatch_never_calls_baseline_writer(fixture_dsn, fixture_container, monkeypatch):
    writes = []
    monkeypatch.setattr(bootstrap, "load_sql", lambda *args: writes.append(args))
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *args, **kwargs:
                        CompletedProcess(args[0], 0, stdout="wrong-identity\n", stderr=""))
    with fixture_database_name(fixture_dsn) as name:
        try:
            bootstrap.create_fixture(fixture_dsn, fixture_container, name)
        except Exception as exc:
            failure = exc
        else:
            failure = None
        assert writes == [], "baseline writer ran before container identity was verified"
        assert isinstance(failure, RuntimeError) and "identity mismatch" in str(failure)
        assert not exists(fixture_dsn, name), "failed initialization leaked its new database"


def test_baseline_failure_cleans_up_its_new_database(fixture_dsn, fixture_container, monkeypatch):
    def fail_baseline(*args):
        raise RuntimeError("synthetic baseline failure")
    monkeypatch.setattr(bootstrap, "load_sql", fail_baseline)
    with fixture_database_name(fixture_dsn) as name:
        with pytest.raises(RuntimeError, match="synthetic baseline failure"):
            bootstrap.create_fixture(fixture_dsn, fixture_container, name)
        assert not exists(fixture_dsn, name)


def test_creation_failure_never_drops_existing_database(fixture_dsn, fixture_container):
    with fixture_database_name(fixture_dsn) as name:
        conn = psycopg2.connect(fixture_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        finally:
            conn.close()
        with pytest.raises(psycopg2.errors.DuplicateDatabase):
            bootstrap.create_fixture(fixture_dsn, fixture_container, name)
        assert exists(fixture_dsn, name)
