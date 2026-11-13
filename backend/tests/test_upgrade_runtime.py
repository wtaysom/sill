"""Upgrade ordering and the atomic migration envelope, without a live database."""
from pathlib import Path
import importlib.util
import json
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def runner():
    spec = importlib.util.spec_from_file_location("migration_runner", ROOT / "backend/scripts/migrate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_frozen_migrations_share_one_transaction_with_stamp():
    mod = runner()
    for path in sorted((ROOT / "backend/migrations").glob("[0-9][0-9][0-9]_*.sql")):
        script = mod.atomic_sql(path, "ag_catalog", [])
        statements = mod.statements(script)
        controls = [s.strip().upper() for s in statements if s.strip().upper() in {"BEGIN", "COMMIT"}]
        assert controls == ["BEGIN", "COMMIT"]
        assert script.index('INSERT INTO "ag_catalog"."schema_migrations"') < script.rindex("COMMIT;")
        assert 'SET LOCAL search_path = "ag_catalog", public, ag_catalog' in script


def test_sql_parser_preserves_function_bodies_and_rejects_internal_commit(tmp_path):
    mod = runner()
    path = tmp_path / "011_fixture.sql"
    body = "DO $f$ BEGIN RAISE NOTICE 'COMMIT; -- hi'; END $f$;"
    path.write_text("-- comment\nBEGIN;\n" + body + "\nCOMMIT;\n")
    assert body in mod.atomic_sql(path, "public", [])
    path.write_text("BEGIN; SELECT 1; COMMIT; SELECT 2;")
    with pytest.raises(ValueError, match="transaction"):
        mod.atomic_sql(path, "public", [])


@pytest.mark.parametrize("applied", [["002"], ["001", "003"], ["001", "999"], ["001", "001"]])
def test_unknown_gapped_or_duplicate_stamps_fail(applied):
    with pytest.raises(ValueError, match="prefix"):
        runner().pending_ids(["001", "002", "003"], applied)


def test_current_schema_has_zero_pending_but_validates_lane():
    assert runner().pending_ids(["001", "002"], ["001", "002"]) == []


def test_worker_copies_every_packaged_module():
    import re
    package = (ROOT / "backend/pyproject.toml").read_text().split("py-modules = [", 1)[1].split("]", 1)[0]
    dockerfile = (ROOT / "backend/Dockerfile.worker").read_text()
    assert "COPY *.py /app/" in dockerfile or all(name + ".py" in dockerfile for name in re.findall(r'"([a-z_]+)"', package))

from test_install_runtime import runtime, run, details, executable, DOCKER

UPGRADE_DOCKER = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_FIXTURE"])
checkout = pathlib.Path(os.environ["TEST_CHECKOUT"])
fail = os.getenv("TEST_UPGRADE_FAILURE", "")
known = [p.name[:3] for p in sorted((checkout / "backend/migrations").glob("[0-9][0-9][0-9]_*.sql"))]
state = root / "upgrade-ledger"
ids = json.loads(state.read_text()) if state.exists() else known[:7]
if fail == "gap": ids = ["001", "003"]
if fail == "future": ids = known + ["999"]
if fail == "legacy": ids = []
if fail == "current": ids = known
handled = True
output = ""
status = 0
if args[:2] == ["container", "inspect"]:
    service = {"sill_db": "db", "sill_embeddings": "embeddings", "sill_maintenance_worker": "maintenance_worker"}[args[-1]]
    output = json.dumps([{"State": {"Running": True}, "Config": {"Labels": {
      "com.docker.compose.project": "wrong" if fail == "project" else "backend", "com.docker.compose.service": service},
      "Cmd": ["--model-id", "custom/model"], "Env": (["EMBEDDING_DIMENSION=768"] if fail in ("legacy-revision", "unknown-revision") else ["REVISION=custom-commit", "EMBEDDING_DIMENSION=768"])},
      "Mounts": [{"Destination": "/var/lib/postgresql/data", "Name": "backend_postgres_data"}]}])
elif args[:2] == ["volume", "inspect"]:
    output = json.dumps([{"Labels": {"com.docker.compose.project": "backend", "com.docker.compose.volume": "postgres_data"}}])
elif args[:2] == ["image", "inspect"]:
    output = "sha256:new"
elif args[0] == "inspect" and args[2] == "{{.Image}}":
    output = "sha256:old" if fail == "stale-worker" else "sha256:new"
elif args[0] == "exec":
    if args[-1] == "true":
        status = 1 if fail == "missing" else 0
    elif "http://localhost:80/info" in args:
        output = json.dumps({"model_id": "custom/model", "model_sha": None if fail == "unknown-revision" else "a" * 40})
    elif "pg_dump" in args:
        output = "-- PostgreSQL database dump\n" * 2000
        status = 1 if fail == "dump" else 0
    elif "python" in args:
        output = "0.2.1"
    elif "psql" in args:
        if "-c" not in args:
            ready = sys.stdin.readline()
            assert ready.strip() == "SELECT pg_backend_pid();"
            print("98765", flush=True)
            sql = sys.stdin.read()
            (root / "applied-sql").write_text(sql)
            if fail == "migration":
                status = 1
            else:
                state.write_text(json.dumps(known))
        else:
            sql = args[-1]
            if "pg_control_system()" in sql:
                output = json.dumps({"system_id": "fixture", "database_oid": 1234, "allow_connections": True, "superuser": True})
            elif "SELECT extname" in sql:
                output = "vector\nage\nhttp"
            elif "to_regprocedure" in sql:
                output = "t"
            elif "to_regclass" in sql and "IS NOT NULL" in sql:
                number = 2 if "session_activity" in sql else (6 if "memory_reuse_events" in sql else 1)
                output = "t" if len(ids) >= number else "f"
            elif "server_version_num" in sql:
                output = "150000" if fail == "pg" else "160006"
            elif "json_agg(n.nspname)" in sql:
                output = json.dumps([] if fail == "legacy" else ["ag_catalog"])
            elif "current_schema()" in sql:
                output = "ag_catalog"
            elif "SELECT migration_id" in sql:
                output = "\n".join(ids)
            elif "json_object_agg(attname" in sql:
                cols = {"id": "uuid", "content": "text", "source_attribution": "jsonb", "importance": "double precision", "access_count": "integer", "trust_level": "double precision", "embedding": "vector(768)"}
                if len(ids) >= 1: cols.update(force="text", speaker="text")
                if len(ids) >= 9: cols.update(production="jsonb", minted_in="jsonb")
                if len(ids) >= 10: cols.update(restates_id="uuid")
                if fail == "shape": cols.pop("id")
                output = json.dumps(cols)
            elif "json_agg(relname)" in sql:
                output = json.dumps(["episodic_memories", "semantic_memories", "procedural_memories", "strategic_memories", "working_memory", "embedding_config", "memory_changes", "identity_aspects"])
            elif "embedding_dimension()" in sql:
                output = "768"
            elif "pg_stat_activity" in sql:
                output = "0"
            elif "max(migration_id)" in sql:
                output = ids[-1] if ids else ""
            elif "ALTER DATABASE" in sql:
                pass
            else:
                raise AssertionError(sql)
    else:
        raise AssertionError(args)
elif args[0] == "compose" and args[7] == "config" and "--format" in args:
    output = json.dumps({"name": "backend", "services": {
        "db": {"container_name": "sill_db", "environment": {"POSTGRES_USER": "sill", "POSTGRES_PASSWORD": "sill_password", "POSTGRES_DB": "sill"}, "ports": [{"published": "5432", "target": 5432, "host_ip": "127.0.0.1"}], "volumes": [{"type": "volume", "source": "postgres_data", "target": "/var/lib/postgresql/data"}]},
        "embeddings": {"container_name": "sill_embeddings"},
        "maintenance_worker": {"container_name": "sill_maintenance_worker"}}, "volumes": {"postgres_data": {"name": "backend_postgres_data"}}})
elif args[0] == "compose" and args[7] in ("stop", "start"):
    pass
elif args[0] == "compose" and args[7] == "build" and fail == "build":
    status = 1
else:
    handled = False
if handled:
    with (root / "docker-calls").open("a") as out: out.write(json.dumps(args) + "\n")
    print(output)
    sys.exit(status)
'''


@pytest.fixture
def upgrade(runtime):
    checkout, home, env = runtime
    import shutil
    shutil.copy2(ROOT / "upgrade.sh", checkout / "upgrade.sh")
    result = run(runtime, "install.sh", "--no-seed")
    assert result.returncode == 0, details(result)
    root = Path(env["TEST_FIXTURE"])
    executable(root / "test-bin/docker", f"#!{sys.executable}\n" + UPGRADE_DOCKER + DOCKER)
    mcp = root / "sill-mcp"
    script = mcp.read_text().replace('assert sys.argv[1] == "--env-file"', 'if sys.argv[1:] == ["--version"]:\n    print("sill-mcp 0.2.1")\n    raise SystemExit()\nassert sys.argv[1] == "--env-file"')
    mcp.write_text(script)
    (root / "docker-calls").unlink()
    return runtime


def calls(upgrade):
    return [json.loads(line) for line in (Path(upgrade[2]["TEST_FIXTURE"]) / "docker-calls").read_text().splitlines()]


@pytest.mark.parametrize("failure", ["gap", "future", "pg", "shape", "missing", "project"])
def test_preflight_failures_make_no_mutation(upgrade, failure):
    result = run(upgrade, "upgrade.sh", "--no-verify", TEST_UPGRADE_FAILURE=failure)
    assert result.returncode != 0
    log = calls(upgrade)
    assert not any("pg_dump" in c or "build" in c or "stop" in c or "up" in c for c in log)
    assert not (upgrade[0] / "backups").exists()


@pytest.mark.parametrize("failure", ["", "current", "legacy"])
def test_upgrade_orders_backup_build_migrate_recreate_even_without_pending(upgrade, failure):
    result = run(upgrade, "upgrade.sh", "--no-verify", TEST_UPGRADE_FAILURE=failure)
    assert result.returncode == 0, details(result)
    log = calls(upgrade)
    dump = next(i for i, c in enumerate(log) if "pg_dump" in c)
    build = next(i for i, c in enumerate(log) if "build" in c)
    stop = next(i for i, c in enumerate(log) if "stop" in c)
    recreate = next(i for i, c in enumerate(log) if "--force-recreate" in c)
    assert dump < build < stop < recreate
    assert any("{{.Image}}" in c for c in log)
    assert "Upgrade complete" in result.stdout
    backup, = (upgrade[0] / "backups").glob("*.gz")
    assert backup.stat().st_mode & 0o777 == 0o600
    choice = json.loads((Path(upgrade[2]["TEST_FIXTURE"]) / "embedding-choice").read_text())
    assert choice["EMBEDDING_REVISION"] == "custom-commit"


@pytest.mark.parametrize("failure", ["dump", "build", "migration", "stale-worker"])
def test_failures_never_report_success_and_migration_can_retry(upgrade, failure):
    result = run(upgrade, "upgrade.sh", "--no-verify", TEST_UPGRADE_FAILURE=failure)
    assert result.returncode != 0, details(result)
    assert "Upgrade complete" not in result.stdout
    log = calls(upgrade)
    if failure == "dump":
        assert not any("build" in c or "stop" in c for c in log)
        assert not list((upgrade[0] / "backups").glob("*.gz"))
    if failure == "migration":
        assert "last successful stamp: 007" in result.stderr
        assert not (upgrade[0] / "backups/.upgrade-fence.json").exists()
        retry = run(upgrade, "upgrade.sh", "--no-verify")
        assert retry.returncode == 0, details(retry)


def test_dry_run_changes_no_files_or_database(upgrade):
    result = run(upgrade, "upgrade.sh", "--dry-run")
    assert result.returncode == 0, details(result)
    assert not (upgrade[0] / "backups").exists()
    assert not any("pg_dump" in c or "build" in c or "up" in c for c in calls(upgrade))


def test_schema_only_never_calls_compose(upgrade):
    result = run(upgrade, "upgrade.sh", "--schema-only", "--no-verify")
    assert result.returncode == 0, details(result)
    assert not any(c[0] == "compose" for c in calls(upgrade))


def test_selected_client_conflict_precedes_backup(upgrade):
    checkout, home, env = upgrade
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"sill": {"command": "/some/other/mcp"}}}))
    result = run(upgrade, "upgrade.sh", "--client", "claude-code", "--no-verify")
    assert result.returncode != 0
    assert not any("pg_dump" in c for c in calls(upgrade))


def test_backup_integrity_failure_stops_before_build(upgrade):
    root = Path(upgrade[2]["TEST_FIXTURE"])
    executable(root / "test-bin/gzip", '#!/bin/sh\nif [ "$1" = -t ]; then exit 1; fi\nexec /usr/bin/gzip "$@"\n')
    result = run(upgrade, "upgrade.sh", "--no-verify")
    assert result.returncode != 0
    assert "integrity" in result.stderr
    assert not any("build" in c or "stop" in c for c in calls(upgrade))
    assert not list((upgrade[0] / "backups").glob("*.gz"))


def test_explicit_model_change_is_rejected_before_backup(upgrade):
    result = run(upgrade, "upgrade.sh", "--no-verify", EMBEDDING_MODEL_ID="different/model")
    assert result.returncode != 0
    assert "EMBEDDING_MODEL_ID" in result.stderr
    assert not any("pg_dump" in c for c in calls(upgrade))


def test_interrupted_marker_stops_before_selected_database_connection(upgrade):
    checkout, home, env = upgrade
    backup = checkout / "backups"
    backup.mkdir()
    (backup / ".upgrade-fence.json").write_text('{"partial":true}')
    result = run(upgrade, "upgrade.sh", "--no-verify")
    assert result.returncode != 0
    assert "explicit recovery" in result.stderr
    assert not any("psql" in c for c in calls(upgrade))


def test_recovery_refuses_wrong_identity_without_mutation(tmp_path):
    from types import SimpleNamespace
    mod = runner()
    marker = {"container": "fixture", "database": "sill", "system_id": "original", "database_oid": 12, "allow_connections": True, "application_name": "sill-upgrade-owned"}
    recorded = []
    class Admin:
        def query(self, sql):
            recorded.append(sql)
            return json.dumps({"system_id": "other-cluster", "database_oid": 12})
    args = SimpleNamespace(container="fixture", database="sill", recovery_file=tmp_path / "marker")
    args.recovery_file.write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="identity"):
        mod.restore_fence(args, marker, Admin())
    assert args.recovery_file.exists()
    assert not any("ALTER" in sql for sql in recorded)


def hooks_module():
    spec = importlib.util.spec_from_file_location("install_hooks", ROOT / "scripts/install_hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_forced_hook_refresh_preserves_unrelated_hooks_and_disabled_setting(tmp_path):
    mod = hooks_module()
    template = ROOT / "plugin/codex.hooks.json.template"
    plugin = ROOT / "plugin"
    dst = tmp_path / "hooks.json"
    env_file = str(tmp_path / "owner's instance/.env")
    data = mod.render(template, plugin, "python3", env_file)
    data["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "my-private-hook --keep"})
    data["keep"] = {"other": 1}
    dst.write_text(json.dumps(data))
    mod.refresh(template, dst, plugin, "new python", env_file, codex=True, force=True)
    current = json.loads(dst.read_text())
    assert current["keep"] == {"other": 1}
    assert any(h["command"] == "my-private-hook --keep" for entry in current["hooks"]["Stop"] for h in entry["hooks"])
    dst.write_text('{"hooks": false, "other": 2}')
    mod.refresh(template, dst, plugin, "python3", env_file, codex=True, force=True)
    assert json.loads(dst.read_text()) == {"hooks": False, "other": 2}


def test_edited_claude_hook_requires_force_for_metadata_and_instance(tmp_path):
    mod = hooks_module()
    template = ROOT / "plugin/codex.hooks.json.template"
    plugin = ROOT / "plugin"
    dst = tmp_path / "settings.json"
    data = mod.render(template, plugin, "python3", "/old/instance.env")
    data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 999
    original = json.dumps(data)
    dst.write_text(original)
    mod.refresh(template, dst, plugin, "python3", "/new/instance.env")
    assert dst.read_text() == original
    mod.refresh(template, dst, plugin, "python3", "/new/instance.env", force=True)
    assert dst.read_text() != original


def test_hooks_only_never_invokes_docker(upgrade):
    project = upgrade[0] / "hook target"
    project.mkdir()
    log = Path(upgrade[2]["TEST_FIXTURE"]) / "docker-calls"
    before = log.read_bytes() if log.exists() else b""
    result = run(upgrade, "upgrade.sh", "--hooks-only", "--hooks-for", str(project))
    assert result.returncode == 0, details(result)
    assert (log.read_bytes() if log.exists() else b"") == before
    data = json.loads((project / ".codex/hooks.json").read_text())
    import shlex
    for entries in data["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert shlex.split(hook["command"])[:2] == ["env", "SILL_INSTANCE_ENV_FILE=" + str(upgrade[0] / "backend/.env")]


def test_legacy_embedding_revision_is_proved_from_running_service(upgrade):
    result = run(upgrade, "upgrade.sh", "--dry-run", TEST_UPGRADE_FAILURE="legacy-revision")
    assert result.returncode == 0, details(result)
    assert any("http://localhost:80/info" in c for c in calls(upgrade))


def test_unprovable_legacy_revision_is_refused_before_backup(upgrade):
    result = run(upgrade, "upgrade.sh", "--no-verify", TEST_UPGRADE_FAILURE="unknown-revision")
    assert result.returncode != 0
    assert "revision" in result.stderr
    assert not any("pg_dump" in c for c in calls(upgrade))


def test_legacy_registry_dependencies_stage_privately_before_backup(upgrade):
    import shlex
    checkout, home, env = upgrade
    root = Path(env["TEST_FIXTURE"])
    wrapper = home / "xdg data/venvs/sill-memory/bin/python"
    executable(wrapper, f'''#!/bin/sh
case "$*" in
  '-m pip --version') exit 0 ;;
  *'import client_config'*) [ -f "$TEST_FIXTURE/private-staging" ] || exit 1 ;;
  *'-m pip install --target '*)
    printf '%s' "$5" > "$TEST_FIXTURE/private-staging"
    exit 0 ;;
esac
exec {shlex.quote(sys.executable)} "$@"
''')
    result = run(upgrade, "upgrade.sh", "--client", "claude-code", "--dry-run")
    assert result.returncode == 0, details(result)
    assert (root / "private-staging").exists()
    staged = Path((root / "private-staging").read_text())
    assert not staged.exists()
    assert not (home / ".claude.json").exists()
    assert not any("pg_dump" in c for c in calls(upgrade))


def test_legacy_preflight_staging_without_installed_pip_uses_private_uv_target(upgrade):
    import shlex
    checkout, home, env = upgrade
    root = Path(env["TEST_FIXTURE"])
    wrapper = home / "xdg data/venvs/sill-memory/bin/python"
    executable(wrapper, f'''#!/bin/sh
case "$*" in
  '-m pip --version') exit 1 ;;
  *'import client_config'*) exit 1 ;;
esac
exec {shlex.quote(sys.executable)} "$@"
''')
    executable(root / "test-bin/uv", f'''#!{sys.executable}
import os,pathlib,sys
assert sys.argv[1:3] == ["pip", "install"]
assert sys.argv[sys.argv.index("--python") + 1].endswith("sill-memory/bin/python")
assert pathlib.Path(os.environ["UV_CACHE_DIR"]).parent == pathlib.Path(sys.argv[sys.argv.index("--target")+1]).parent
pathlib.Path(os.environ["TEST_FIXTURE"], "private-staging").write_text(sys.argv[sys.argv.index("--target")+1])
''')
    result = run(upgrade, "upgrade.sh", "--client", "codex", "--dry-run")
    assert result.returncode == 0, details(result)
    assert not Path((root / "private-staging").read_text()).exists()
    assert not (home / ".codex/config.toml").exists()
    assert not any("pg_dump" in c for c in calls(upgrade))


def test_mcp_handshake_must_match_candidate_version_even_when_cli_matches(upgrade):
    mcp = Path(upgrade[2]["TEST_FIXTURE"]) / "sill-mcp"
    script = mcp.read_text().replace('"version": "0.2.1"', '"version": "0.0.1"')
    mcp.write_text(script)
    result = run(upgrade, "upgrade.sh", "--no-verify", TEST_UPGRADE_FAILURE="current")
    assert result.returncode != 0
    assert "MCP running version" in result.stderr
    assert "Upgrade complete" not in result.stdout
