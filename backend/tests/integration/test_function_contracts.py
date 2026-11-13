"""Measured callable contracts, including legacy AGE shadows, on both routes."""
import json
from pathlib import Path

import psycopg2

from bootstrap_fixture import apply_migrations, temporary_fixture
from test_supersession_production import row, scalar, seed, supersede

MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "function-contracts.json"


def catalog(conn, names):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
                   pg_get_function_result(p.oid), p.proconfig, p.prosrc
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname IN ('public','ag_catalog') AND p.proname=ANY(%s)
            ORDER BY n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)
        """, (names,))
        return [dict(zip(("namespace", "name", "arguments", "result", "settings", "body"), r)) for r in cur.fetchall()]


def contract_only(rows):
    return [{k: v for k, v in r.items() if k != "body"} for r in rows]


def test_fresh_and_v02_function_contracts(fixture_dsn, fixture_container):
    manifest = json.loads(MANIFEST.read_text())
    names = manifest["function_names"]
    with temporary_fixture(fixture_dsn, fixture_container, through="007") as upgraded:
        with psycopg2.connect(upgraded) as conn:
            assert contract_only(catalog(conn, names)) == manifest["v02"]
            historical = seed(conn, speaker="Historical fixture", source_attribution={"ref": "fixture:original"},
                              created_at="2001-01-01T00:00:00Z")
            before = row(conn, historical)
        conn.close()
        apply_migrations(upgraded, after="007", through="009")
        with temporary_fixture(fixture_dsn, fixture_container) as fresh:
            snapshots = []
            for dsn in (fresh, upgraded):
                conn = psycopg2.connect(dsn)
                try:
                    snapshots.append(catalog(conn, names))
                    assert contract_only(snapshots[-1]) == manifest["v03_m1"]
                    scalar(conn, "SELECT set_config('search_path','ag_catalog,public',true)")
                    # Both explicit legacy creation routes are callable; unqualified
                    # resolution deliberately reaches AGE's existing shadow.
                    for namespace in ("public.", "ag_catalog.", ""):
                        mid = scalar(conn, f"SELECT {namespace}create_memory('semantic',%s)", ("Synthetic contract content long enough for the existing legacy minimum length guard: " + namespace,))
                        assert row(conn, mid)["content"] == "Synthetic contract content long enough for the existing legacy minimum length guard: " + namespace
                    for namespace, legacy in (("", True), ("public.", True), ("", False), ("public.", False)):
                        old = seed(conn, speaker="Past voice")
                        new = supersede(conn, old, namespace=namespace, legacy=legacy)
                        assert row(conn, new)["source_attribution"]["predecessor"]["speaker"] == "Past voice"
                    if dsn == upgraded:
                        after = row(conn, historical)
                        assert after["production"] is None and after["minted_in"] is None
                        assert {k: after[k] for k in before} == before
                finally:
                    conn.rollback()
                    conn.close()
            # Bodies as well as signatures/settings must agree across install paths.
            assert snapshots[0] == snapshots[1]
