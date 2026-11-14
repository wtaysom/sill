"""Exercise the native acceptance snapshot offline with stored seed rows."""
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
OBSERVED_AT = "2026-11-13T13:39:48.906861+00:00"
ATTRIBUTION = {"author": "sill-seed-v0.1", "trust": 0.5, "observed_at": OBSERVED_AT}


@pytest.fixture
def pack():
    return [json.loads(line) for line in (ROOT / "seed/methodology.jsonl").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


@pytest.fixture
def rows(pack):
    return [dict(id=str(index), content=row["content"], type=row["type"],
                 attribution=dict(ATTRIBUTION), created_at=OBSERVED_AT, speaker=None,
                 status="active", archived_at=None, dimension=768)
            for index, row in enumerate(pack)]


@pytest.fixture
def snapshot(tmp_path, pack):
    pytest.importorskip("tomllib", reason="the native Linux job uses Python 3.12")
    rehearsal = runpy.run_path(str(ROOT / ".github/scripts/native_linux_runtime.py"))
    profile = rehearsal["PROFILE"]
    source = dict(profile, query_prefix="", document_prefix="")
    operation = dict(id="fixture-operation", state="active", source=source, target=profile,
                     backup_transport="explicit-container", backup_verification="pg_restore --list; verified")
    identity = ["system-id", "sill", "db-oid"]
    volume = dict(name="fixture_postgres_data", created_at="fixture-time", nonce="fixture-nonce")
    receipt = dict(prepare_started=True, operation_id=operation["id"], database_identity=identity,
                   selection=dict(source=source, target=profile, fingerprint="fixture-fingerprint"),
                   volume_identity=volume, nonce=volume["nonce"])
    path = tmp_path / "backend/.env.embedding-profile.json"
    path.parent.mkdir()
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    owned = {"db": {"Id": "fixture-db", "Mounts": [
        dict(Type="volume", Name=volume["name"], Destination="/var/lib/postgresql/data")]},
        "postgres_data": dict(Name=volume["name"], CreatedAt=volume["created_at"],
                              Labels={"org.sill.fresh-profile-intent": volume["nonce"]})}

    def capture(rows, store):
        responses = iter([
            [f"{i:03}" for i in range(1, 16)], 15,
            dict(profile=profile, fingerprint="fixture-fingerprint",
                 provenance={"historical_identity": "declared-unverified"}),
            [operation], identity, rows,
        ])

        def docker(*args):
            return json.dumps(next(responses))

        rehearsal["snapshot"](docker, owned, tmp_path, pack, store)

    return capture


def test_snapshot_accepts_canonical_seed_attribution(snapshot, pack, rows):
    store = {}

    snapshot(rows, store)

    assert store["seed_count"] == len(pack)
    assert store["seed_rows"] == rows


@pytest.mark.parametrize(("change", "message"), [
    ({"attribution": ATTRIBUTION | {"author": "other"}}, "content, type, or author"),
    ({"attribution": ATTRIBUTION | {"trust": 0.7}}, "normalized attribution"),
    ({"attribution": ATTRIBUTION | {"origin": "agi-memory development"}}, "normalized attribution"),
    ({"attribution": {"author": "sill-seed-v0.1", "trust": 0.5}}, "normalized attribution"),
    ({"attribution": ATTRIBUTION | {"observed_at": "invalid"}, "created_at": "invalid"}, "observed_at"),
    ({"attribution": ATTRIBUTION | {"observed_at": OBSERVED_AT[:-6]}}, "observed_at"),
    ({"attribution": ATTRIBUTION | {"observed_at": "2026-11-13T13:40:48+00:00"}}, "observed_at"),
    ({"content": "Different seed content"}, "content, type, or author"),
    ({"type": "strategic"}, "content, type, or author"),
    ({"speaker": "Unexpected speaker"}, "speaker, status, archive timestamp, or vector dimension"),
    ({"status": "invalidated"}, "speaker, status, archive timestamp, or vector dimension"),
    ({"archived_at": OBSERVED_AT}, "speaker, status, archive timestamp, or vector dimension"),
    ({"dimension": 384}, "speaker, status, archive timestamp, or vector dimension"),
])
def test_snapshot_rejects_and_preserves_corrupt_seed_row(snapshot, rows, change, message):
    rows[0].update(change)
    store = {}

    with pytest.raises(RuntimeError, match=message):
        snapshot(rows, store)

    assert store["seed_rows"] == rows


@pytest.mark.parametrize("row_count", [0, 21, 23])
def test_snapshot_rejects_and_preserves_wrong_seed_count(snapshot, rows, row_count):
    rows = (rows + rows[:1])[:row_count]
    store = {}

    with pytest.raises(RuntimeError, match="row count"):
        snapshot(rows or None, store)

    assert store["seed_count"] == row_count
    assert store["seed_rows"] == rows
