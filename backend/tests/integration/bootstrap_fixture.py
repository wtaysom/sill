"""Create a NEW disposable database from the public image's extensions/schema.

Example (server already built from backend/Dockerfile on an isolated port):
  SILL_TEST_ALLOW_DATABASE=1 SILL_TEST_DSN=... SILL_TEST_CONTAINER=sill_v03_test_db \
    python backend/tests/integration/bootstrap_fixture.py --name sill_test_fresh

No default DSN/container. Refuses house names, non-loopback hosts, normal DB and
probe ports; never drops a caller's existing database. Fixture teardown can drop
only the uniquely named database this module just created. No private data.
"""
import argparse
from contextlib import closing, contextmanager
import os
import re
import subprocess
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from conftest import BACKEND, embedding_stub, guarded_dsn


def guarded_container(name):
    if not re.fullmatch(r"sill_(?:v03_test_db|test_[a-z0-9_]+)", name or ""):
        raise ValueError("refusing non-fixture container; use an explicit sill_test_* container")
    return name


def baseline_sql():
    original = (BACKEND / "schema.sql").read_text()
    # The baseline performs embedding-dependent seed operations. Replace exactly
    # this network function, preserving its name/argument/return contract; no
    # transactional function being tested is rewritten. Retain psql meta commands.
    pattern = r"CREATE OR REPLACE FUNCTION public\.get_embedding\(text_content TEXT\).*?\$\$ LANGUAGE plpgsql;"
    text, count = re.subn(pattern, lambda _: embedding_stub(), original, flags=re.S)
    if count != 1:
        raise RuntimeError("baseline get_embedding boundary changed; inspect before stubbing")
    return text


def load_sql(container, name, text):
    result = subprocess.run(["docker", "exec", "-i", container, "psql", "-X", "-U", "sill_test",
                             "-d", name, "-v", "ON_ERROR_STOP=1"],
                            input=text, text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError("fixture SQL failed:\n" + result.stderr[-6000:])


def verify_container_identity(container, name, marker):
    # No writes through Docker until its database proves it is the one just
    # created through the guarded TCP connection. A mismatched/absent marker,
    # missing table, failed command or timeout fails closed.
    result = subprocess.run([
        "docker", "exec", container, "psql", "-X", "-qAt", "-U", "sill_test",
        "-d", name, "-v", "ON_ERROR_STOP=1", "-c",
        "BEGIN READ ONLY; SELECT marker FROM public.fixture_identity; COMMIT;",
    ], text=True, capture_output=True, timeout=10)
    if result.returncode or result.stdout.strip() != marker:
        raise RuntimeError("container/DSN fixture identity mismatch")


def drop_owned_fixture(dsn, name):
    # Call only after this invocation witnessed successful CREATE DATABASE.
    # Never FORCE a drop or terminate another connection.
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def create_fixture(dsn, container, name, through="009"):
    guarded_dsn(dsn, os.environ.get("SILL_TEST_ALLOW_DATABASE"))
    guarded_container(container)
    if not re.fullmatch(r"sill_test_[a-z0-9_]+", name):
        raise ValueError("new fixture name must start sill_test_")
    target = make_dsn(dsn, dbname=name)
    created = False
    try:
        # CREATE fails if the name exists. Only a witnessed successful CREATE
        # gives this invocation permission to clean up on initialization failure.
        with closing(psycopg2.connect(dsn)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
                created = True
                cur.execute(sql.SQL("ALTER DATABASE {} SET search_path=ag_catalog,public").format(sql.Identifier(name)))
        marker = str(uuid4())
        with closing(psycopg2.connect(target)) as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE TABLE public.fixture_identity(marker text)")
                    cur.execute("INSERT INTO public.fixture_identity VALUES (%s)", (marker,))
        verify_container_identity(container, name, marker)
        load_sql(container, name, baseline_sql())
        apply_migrations(target, through=through)
        return target
    except BaseException:
        if created:
            drop_owned_fixture(dsn, name)
        raise


def apply_migrations(dsn, *, through="009", after="000"):
    guarded_dsn(dsn, os.environ.get("SILL_TEST_ALLOW_DATABASE"))
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations "
                        "(migration_id text PRIMARY KEY, applied_at timestamptz DEFAULT now(), source text)")
            for path in sorted((BACKEND / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                if after < path.name[:3] <= through:
                    cur.execute(path.read_text())
                    cur.execute("INSERT INTO public.schema_migrations(migration_id,source) VALUES(%s,'fixture')",
                                (path.name[:3],))
            # Migration 004 restores the real embedding body: set the test-only
            # boundary again. All function contract snapshots exclude its body.
            cur.execute(embedding_stub())
    finally:
        conn.close()


@contextmanager
def temporary_fixture(dsn, container, *, through="009"):
    name = "sill_test_" + uuid4().hex
    target = create_fixture(dsn, container, name, through=through)
    try:
        yield target
    finally:
        drop_owned_fixture(dsn, name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--through", default="009")
    args = parser.parse_args()
    create_fixture(os.environ["SILL_TEST_DSN"], os.environ["SILL_TEST_CONTAINER"], args.name, args.through)
    print("Created fixture " + args.name + " through " + args.through)
