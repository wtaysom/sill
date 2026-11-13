"""Receipt checks against synthetic snapshots and read-only transport doubles."""

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from scripts import receipt_audit as receipts
from scripts.beat_audit import Window


NOTE = Path("record.md")
MINTED = datetime.fromisoformat("2026-11-13T12:01:00+08:00")
EARLIER = datetime.fromisoformat("2026-11-12T12:01:00+08:00")
WINDOW = Window(
    datetime.fromisoformat("2026-11-13T12:00:00+08:00"),
    datetime.fromisoformat("2026-11-13T04:02:00+00:00"),
)
FIRST = "4c44c088-ebfa-42dc-b200-000000000001"
SECOND = "4c44c088-ebff-42dc-b200-000000000002"
OLDER = "72228257-aabb-42dc-b200-000000000003"


def store(*rows):
    return lambda keys: {
        key: [(memory_id, stamp) for memory_id, stamp in rows if memory_id.startswith(key)]
        for key in keys
    }


def findings(result):
    return [(finding.line, finding.code, finding.origin) for finding in result.findings]


def test_receipt_checks_full_prefix_and_flags_ambiguity():
    after = {NOTE: "\n".join((
        f"Stored: {FIRST}",
        "- **Stored:** `4c44c088-ebfa-42`",
        "## Stored: 4c44c088-dead",
        "Stored: 4c44c088-ebf",
        f"Stored: {FIRST}dead",
        f"Stored: {FIRST} [supersedes 4c44c088 — archived]",
        f"Stored: {FIRST} [supersedes deadbeef] [home: goal cccccccc]",
    ))}
    result = receipts.audit({}, after, WINDOW, lookup=store((FIRST, MINTED), (SECOND, EARLIER)))
    assert result.status == "checked"
    assert findings(result) == [
        (3, "missing_receipt", "new"),
        (4, "ambiguous_receipt", "new"),
        (5, "invalid_receipt", "new"),
        (6, "ambiguous_supersedes", "new"),
        (7, "missing_supersedes", "new"),
    ]


def test_new_receipt_checks_window_preexisting_checks_existence_only():
    before = {NOTE: f"# Original\nStored: {OLDER}\nStored: deadbeef\n"}
    after = {NOTE: (
        f"# Edited\n\nStored: {OLDER} [reformatted]\nStored: deadbeef\n"
        f"Stored: {FIRST}\nStored: {SECOND} [supersedes {OLDER}]\n"
    )}
    result = receipts.audit(before, after, WINDOW, lookup=store(
        (OLDER, EARLIER), (FIRST, MINTED), (SECOND, EARLIER),
    ))
    assert result.status == "checked"
    assert findings(result) == [
        (4, "missing_receipt", "preexisting"),
        (6, "outside_window", "new"),
    ]


def test_new_duplicate_occurrence_is_time_checked():
    before = {NOTE: f"Stored: {OLDER}"}
    after = {NOTE: f"Stored: {OLDER}\nStored: {OLDER}"}
    result = receipts.audit(before, after, WINDOW, lookup=store((OLDER, EARLIER)))
    assert findings(result) == [(2, "outside_window", "new")]


def test_fenced_mentions_are_not_receipts():
    after = {NOTE: "\n".join((
        "> Stored: deadbeef",
        "`Stored: deadbeef`",
        "Some prose mentions `Stored: deadbeef`.",
        "````markdown",
        "Stored: deadbeef",
        "``` markdown",
        "Stored: MINT-PENDING",
        "````",
        "~~~text",
        "Stored: deadbeef",
        "~~~",
        f"Stored: {FIRST}",
    ))}

    def lookup(keys):
        assert keys == {FIRST}
        return store((FIRST, MINTED))(keys)

    assert receipts.audit({}, after, WINDOW, lookup=lookup).findings == []


def test_store_down_is_unchecked_but_pending_slot_is_reported():
    after = {NOTE: f"Stored: {FIRST}\nStored: MINT-PENDING"}

    def lookup(_):
        raise receipts.StoreUnreachable("offline")

    result = receipts.audit({}, after, WINDOW, lookup=lookup)
    assert result.status == "unchecked"
    assert result.exit_code == 3
    assert findings(result) == [(2, "mint_pending", "new")]
    assert "offline" in result.reason


@pytest.mark.parametrize("before,window,origin", [(None, WINDOW, "unknown"), ({}, None, "new")])
def test_missing_timing_evidence_preserves_existence_checks(before, window, origin):
    after = {NOTE: f"Stored: {OLDER}\nStored: deadbeef"}
    result = receipts.audit(before, after, window, lookup=store((OLDER, EARLIER)))
    assert result.status == "unchecked"
    assert findings(result) == [(2, "missing_receipt", origin)]


def test_literal_instance_is_selected_and_ambient_pg_settings_are_removed(tmp_path, monkeypatch):
    instance = tmp_path / "selected.env"
    instance.write_text("POSTGRES_HOST=fixture.invalid\nPOSTGRES_PORT=15434\n"
                        "POSTGRES_DB=fixture\nPOSTGRES_USER=reader\n"
                        "POSTGRES_PASSWORD='fixture $ literal'\n")
    monkeypatch.setenv("SILL_INSTANCE_ENV_FILE", str(instance))
    monkeypatch.setenv("SILL_DB_DSN", "postgresql://stale.invalid/stale")
    monkeypatch.setenv("SILL_DB_CONTAINER", "stale-container")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGHOSTADDR", "127.0.0.1")
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=0")

    def run(args, *, input, env, timeout, cwd, **kwargs):
        assert args[0] != "docker"
        assert cwd == Path(receipts.__file__).resolve().parents[1]
        assert not any(name.startswith("PG") for name in env)
        assert "fixture%20%24%20literal" not in " ".join(args)
        payload = json.loads(input)
        assert payload["dsn"] == "postgresql://reader:fixture%20%24%20literal@fixture.invalid:15434/fixture"
        assert payload["keys"] == [FIRST]
        assert 0 < timeout <= 20
        return subprocess.CompletedProcess(args, 0, json.dumps([[FIRST, FIRST, MINTED.isoformat()]]), "")

    monkeypatch.setattr(receipts.subprocess, "run", run)
    assert receipts.lookup_rows({FIRST}) == {FIRST: [(FIRST, MINTED)]}


def test_unavailable_selected_instance_never_reaches_docker_or_default_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SILL_INSTANCE_ENV_FILE", str(tmp_path / "missing.env"))

    def run(*args, **kwargs):
        pytest.fail("A missing selected instance must not start a query")

    monkeypatch.setattr(receipts.subprocess, "run", run)
    result = receipts.audit({}, {NOTE: f"Stored: {FIRST}"}, WINDOW)
    assert result.status == "unchecked"
    assert result.findings == []


def test_selected_query_failure_is_unchecked_and_redacted(tmp_path, monkeypatch):
    instance = tmp_path / "selected.env"
    instance.write_text("SILL_DB_DSN=postgresql://reader:secret@fixture.invalid:15434/fixture\n")
    monkeypatch.setenv("SILL_INSTANCE_ENV_FILE", str(instance))
    invocations = []

    def run(args, **kwargs):
        invocations.append(args[0])
        return subprocess.CompletedProcess(args, 1, "", "Connection failed with secret")

    monkeypatch.setattr(receipts.subprocess, "run", run)
    result = receipts.audit({}, {NOTE: f"Stored: {FIRST}"}, WINDOW)
    assert len(invocations) == 1
    assert result.status == "unchecked"
    assert "secret" not in result.to_json()


def test_legacy_docker_uses_installed_settings_and_empty_rows_are_missing(monkeypatch):
    monkeypatch.delenv("SILL_INSTANCE_ENV_FILE", raising=False)
    monkeypatch.setenv("SILL_DB_CONTAINER", "fixture-container")
    monkeypatch.setenv("SILL_DB_USER", "fixture-reader")
    monkeypatch.setenv("SILL_DB_NAME", "fixture-db")

    def run(args, *, timeout, **kwargs):
        assert args[:2] == ["docker", "exec"]
        assert "fixture-container" in args
        assert args[args.index("-U") + 1] == "fixture-reader"
        assert args[args.index("-d") + 1] == "fixture-db"
        assert "default_transaction_read_only=on" in " ".join(args)
        assert "public.memories" in args[-1] and "LIMIT 2" in args[-1]
        assert 0 < timeout <= 20
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(receipts.subprocess, "run", run)
    result = receipts.audit({}, {NOTE: f"Stored: {FIRST}"}, WINDOW)
    assert result.status == "checked"
    assert findings(result) == [(1, "missing_receipt", "new")]


def test_old_cli_checks_existence_with_or_without_completed_receipt(tmp_path, monkeypatch, capsys):
    old = tmp_path / "old.md"
    old.write_text(f"Stored: {OLDER}\nStored: deadbeef\n")
    receipt = tmp_path / "attempt.json"
    receipt.write_text(json.dumps({
        "schema_version": 1, "status": "succeeded",
        "spawned_at": WINDOW.start.isoformat(), "completed_at": WINDOW.end.isoformat(),
    }))

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, f"{OLDER},{OLDER},{EARLIER.isoformat()}\n", "")

    monkeypatch.delenv("SILL_INSTANCE_ENV_FILE", raising=False)
    monkeypatch.setattr(receipts.subprocess, "run", run)
    for evidence, expected_status, expected_code in [
        (["--receipt", str(receipt)], "checked", 0),
        ([], "unchecked", 3),
    ]:
        assert receipts.main([*evidence, "--old", str(old), "--json"]) == expected_code
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == expected_status
        assert [(item["code"], item["origin"]) for item in result["findings"]] == [
            ("missing_receipt", "preexisting"),
        ]


@pytest.mark.asyncio
async def test_selected_child_binds_prefixes_in_a_readonly_transaction(monkeypatch):
    from contextlib import asynccontextmanager

    import asyncpg

    selected = "postgresql://reader:fixture@fixture.invalid:15434/fixture"

    class Connection:
        @asynccontextmanager
        async def transaction(self, *, readonly):
            assert readonly
            self.in_transaction = True
            yield
            self.in_transaction = False

        async def fetch(self, query, keys):
            assert self.in_transaction
            assert "public.memories" in query and "LIMIT 2" in query
            assert "$1::text[]" in query and FIRST not in query
            assert keys == [FIRST]
            return [(FIRST, FIRST, MINTED)]

        async def close(self, *, timeout):
            assert not self.in_transaction
            assert 0 < timeout < 5

    async def connect(dsn, *, timeout, command_timeout, server_settings):
        assert dsn == selected
        assert 0 < timeout < command_timeout < 20
        assert 0 < int(server_settings["statement_timeout"]) <= 10000
        return Connection()

    monkeypatch.setattr(asyncpg, "connect", connect)
    assert await receipts._read_selected({"dsn": selected, "keys": [FIRST]}) == [
        [FIRST, FIRST, MINTED.isoformat()]
    ]


def test_fences_inside_lists_and_inline_supersedes_mentions_are_ignored():
    after = {NOTE: "\n".join((
        "- ```text",
        "  Stored: deadbeef",
        "  ```",
        f"Stored: ``{FIRST}`` (example: `supersedes deadbeef`) [supersedes `{OLDER}`]",
    ))}

    def lookup(keys):
        assert keys == {FIRST, OLDER}
        return store((FIRST, MINTED), (OLDER, EARLIER))(keys)

    result = receipts.audit({}, after, WINDOW, lookup=lookup)
    assert result.status == "checked"
    assert result.findings == []
