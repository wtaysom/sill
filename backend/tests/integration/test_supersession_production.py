"""Real transactional contracts: history must never become current authorship."""
from contextlib import contextmanager
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json, RealDictCursor
import pytest

from conftest import guarded_dsn


DECLARATION = {"made": [{"who": "Ada Example", "kind": "person", "work": "fixture idea",
                         "at": ["fixture:one"], "declared": {"by": {"who": "Ada Example", "kind": "person"},
                         "when": "2026-01-01T00:00:00Z", "basis": "stated"}}], "note": "synthetic"}


def scalar(db, sql, args=()):
    with db.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def row(db, mid):
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM memories WHERE id=%s", (mid,))
        return dict(cur.fetchone())


def seed(db, **fields):
    fields = {"content": "fixture " + str(uuid4()), "type": "semantic", **fields}
    # field names belong exclusively to these tests.
    columns = ",".join(fields)
    values = [Json(v) if k in {"production", "minted_in", "source_attribution"} and v is not None
              else v for k, v in fields.items()]
    return scalar(db, f"INSERT INTO memories({columns},embedding) VALUES ("
                  + ",".join(["%s"] * len(values)) + ",public.get_embedding('fixture')) RETURNING id", values)


def supersede(db, mid, metadata=None, namespace="", legacy=False):
    if legacy:
        return scalar(db, f"SELECT {namespace}supersede_memory(%s,%s)", (mid, "successor " + str(uuid4())))
    return scalar(db, f"SELECT {namespace}supersede_memory(%s,%s,%s,%s::jsonb)",
                  (mid, "successor " + str(uuid4()), "fixture revision", Json(metadata or {})))


@contextmanager
def refused(db, match):
    with db.cursor() as cur:
        cur.execute("SAVEPOINT invalid_input")
    try:
        with pytest.raises(psycopg2.Error, match=match):
            yield
    finally:
        with db.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT invalid_input")
            cur.execute("RELEASE SAVEPOINT invalid_input")


@pytest.mark.parametrize("namespace,legacy", [("", False), ("public.", False), ("", True), ("public.", True)])
def test_supersede_preserves_history_not_current_authorship(db, namespace, legacy):
    old = seed(db, speaker="Original author", force="assertive", source_attribution={"ref": "fixture:source"})
    with db.cursor() as cur:
        cur.execute("INSERT INTO semantic_memories(memory_id,confidence,last_validated,category) "
                    "VALUES(%s,.91,'2020-01-01T00:00:00Z',ARRAY['historical'])", (old,))
    new = supersede(db, old, namespace=namespace, legacy=legacy)
    got = row(db, new)
    assert got["speaker"] is None and got["force"] is None
    history = got["source_attribution"]["predecessor"]
    assert history["speaker"] == "Original author"
    assert history["source_attribution"] == {"ref": "fixture:source"}
    assert history["type_metadata"]["confidence"] == .91
    assert "ref" not in got["source_attribution"]
    assert scalar(db, "SELECT confidence FROM semantic_memories WHERE memory_id=%s", (new,)) == .5
    assert scalar(db, "SELECT last_validated FROM semantic_memories WHERE memory_id=%s", (new,)) is None
    assert row(db, old)["status"] == "archived"
    explicit = supersede(db, new, {"speaker": "New author", "force": "directive", "type_metadata": {"confidence": .8}})
    assert row(db, explicit)["speaker"] == "New author"
    assert row(db, explicit)["force"] == "directive"
    assert scalar(db, "SELECT confidence FROM semantic_memories WHERE memory_id=%s", (explicit,)) == .8
    assert row(db, explicit)["source_attribution"]["metadata_ancestors"][0]["id"] == str(old)


def test_supersede_concepts_inherit_nearest_or_explicit_empty(db):
    ancestor = seed(db)
    cid = scalar(db, "INSERT INTO concepts(name) VALUES(%s) RETURNING id", ("fixture " + str(uuid4()),))
    with db.cursor() as cur:
        cur.execute("INSERT INTO memory_concepts(memory_id,concept_id,strength) VALUES(%s,%s,.7)", (ancestor, cid))
    intermediate = seed(db, supersedes_id=ancestor)
    new = supersede(db, intermediate)
    assert scalar(db, "SELECT strength FROM memory_concepts WHERE memory_id=%s AND concept_id=%s", (new, cid)) == .7
    cleared = supersede(db, new, {"concepts": []})
    descendant = supersede(db, cleared)
    assert scalar(db, "SELECT count(*) FROM memory_concepts WHERE memory_id=%s", (descendant,)) == 0
    # A malformed historical cycle must terminate in both ancestry traversals.
    a, b = seed(db), seed(db)
    with db.cursor() as cur:
        cur.execute("UPDATE memories SET supersedes_id=%s WHERE id=%s", (a, b))
        cur.execute("UPDATE memories SET supersedes_id=%s WHERE id=%s", (b, a))
    assert row(db, supersede(db, a))["supersedes_id"] == a


def test_production_optional_and_never_guessed(db):
    old = seed(db, speaker="Named speaker", minted_in={"session": "old-session"})
    assert row(db, old)["production"] is None
    new = supersede(db, old)
    assert row(db, new)["production"] is None
    assert row(db, new)["minted_in"].get("session") is None
    declared = supersede(db, new, {"production": DECLARATION, "minted_in": {"session": "new-session"}})
    assert row(db, declared)["production"] == DECLARATION
    assert row(db, declared)["minted_in"] == {"session": "new-session"}
    for bad in ({}, {"made": None}, [], "invalid", {"made": "invalid"}):
        with refused(db, "production"):
            seed(db, production=bad)
    with refused(db, "minted_in"):
        seed(db, minted_in=[])


def test_production_supersession_append_redeclare_and_repair(db):
    old = seed(db, production=DECLARATION, minted_in={"session": "never-inherit"})
    inherited = supersede(db, old)
    assert row(db, inherited)["production"] == DECLARATION
    added = {"who": "Bea Example", "kind": "person", "work": "fixture refinement"}
    claim = {"match": {"work": "fixture idea"}, "because": "revised", "declared": {"basis": "stated"}}
    amended = supersede(db, inherited, {"production_add": [added, added], "production_not_carried": [claim]})
    got = row(db, amended)["production"]
    assert len(got["made"]) == 2 and got["made"][1] == added
    assert got["made"][0]["not_carried"] == {"because": "revised", "declared": {"basis": "stated"}}
    assert {k: v for k, v in got["made"][0].items() if k != "not_carried"} == DECLARATION["made"][0]
    redeclared = supersede(db, amended, {"production": {"made": [], "superseded_declarations": [{"made": [], "note": "older"}]}})
    stack = row(db, redeclared)["production"]["superseded_declarations"]
    assert stack == [{"made": [], "note": "older"}, got]
    assert row(db, redeclared)["source_attribution"]["predecessor"]["production"] == got
    raw = seed(db, supersedes_id=redeclared)
    assert row(db, raw)["production"] == row(db, redeclared)["production"]
    assert row(db, raw)["minted_in"] is None
    with refused(db, "never erased"):
        scalar(db, "UPDATE memories SET production=NULL WHERE id=%s RETURNING id", (raw,))
    with refused(db, "sill.production_repair"):
        scalar(db, "UPDATE memories SET production=%s WHERE id=%s RETURNING id", (Json(DECLARATION), raw))
    with db.cursor() as cur:
        cur.execute("SET LOCAL sill.production_repair = on")
        cur.execute("UPDATE memories SET production=%s WHERE id=%s", (Json(DECLARATION), raw))
    assert row(db, raw)["production"]["superseded_declarations"] == [row(db, redeclared)["production"]]


def test_invalid_successor_rolls_back_all_writes(db):
    old = seed(db)
    before = scalar(db, "SELECT count(*) FROM memories")
    with refused(db, "concept"):
        supersede(db, old, {"concepts": [{"name": ""}]})
    assert row(db, old)["status"] == "active"
    assert scalar(db, "SELECT count(*) FROM memories") == before


@pytest.mark.parametrize("dsn,allow", [
    ("dbname=agi_db user=sill_test host=127.0.0.1 port=15434", "1"),
    ("dbname=sill_v03_tests user=sill_test host=127.0.0.1 port=5432", "1"),
    ("dbname=sill_v03_tests user=sill_test host=127.0.0.1 port=15432", "1"),
    ("dbname=sill_v03_tests user=sill_test host=remote port=15434", "1"),
    ("dbname=sill_v03_tests user=sill_test host=127.0.0.1 port=15434", None),
])
def test_fixture_refuses_unsafe_database(dsn, allow):
    with pytest.raises(ValueError):
        guarded_dsn(dsn, allow)


def test_json_null_declarations_and_metadata_are_absent(db):
    old = seed(db, production=DECLARATION)
    new = supersede(db, old, {"production": None, "type_metadata": None, "minted_in": None})
    assert row(db, new)["production"] == DECLARATION
    null_metadata = scalar(db, "SELECT public.supersede_memory(%s,%s,NULL,'null'::jsonb)", (new, "null metadata"))
    assert row(db, null_metadata)["production"] == DECLARATION
    blank = seed(db)
    with db.cursor() as cur:
        cur.execute("UPDATE memories SET minted_in='null'::jsonb WHERE id=%s", (blank,))
    assert row(db, blank)["minted_in"] is None


def test_supersession_concurrent_loser_does_not_create_a_second_successor(isolated_store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic

    winner = psycopg2.connect(isolated_store)
    loser = psycopg2.connect(isolated_store)
    try:
        with loser.cursor() as cur:
            cur.execute("SET statement_timeout='8s'")
        old = seed(winner)
        winner.commit()
        successor = supersede(winner, old)
        started = Event()

        def contend():
            started.set()
            try:
                supersede(loser, old)
            except psycopg2.Error as exc:
                loser.rollback()
                return str(exc)
            raise AssertionError("second successor unexpectedly committed")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contend)
            assert started.wait(5)
            # Observe the loser actually waiting for a lock before committing.
            deadline = monotonic() + 5
            while monotonic() < deadline:
                waiting = scalar(winner, "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=%s", (loser.get_backend_pid(),))
                if waiting:
                    break
                scalar(winner, "SELECT pg_stat_clear_snapshot()")
            else:
                raise AssertionError("concurrent contender never reached its row lock")
            winner.commit()
            assert "already archived" in future.result(timeout=5)
        assert scalar(winner, "SELECT count(*) FROM memories WHERE supersedes_id=%s", (old,)) == 1
        assert row(winner, successor)["status"] == "active"
    finally:
        winner.rollback()
        loser.rollback()
        winner.close()
        loser.close()
