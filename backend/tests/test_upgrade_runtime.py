"""Upgrade ordering and the atomic migration envelope, without a live database."""
from pathlib import Path
import importlib.util
import json
import re
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
import json, os, pathlib, re, sys
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
if args[:2] == ["container", "ls"]:
    output = "sill_db\nsill_embeddings\nsill_maintenance_worker"
elif args[:2] == ["container", "inspect"]:
    service = {"sill_db": "db", "sill_embeddings": "embeddings", "sill_maintenance_worker": "maintenance_worker"}[args[-1]]
    previous = [json.loads(line) for line in (root / "docker-calls").read_text().splitlines()] if (root / "docker-calls").exists() else []
    phase = os.getenv("TEST_DB_CHANGE_AFTER", "preflight")
    changed = phase == "preflight" or (phase == "backup" and any("pg_dump" in c for c in previous)) or (phase == "build" and any("build" in c for c in previous)) or (phase == "worker" and any("up" in c and "maintenance_worker" in c for c in previous)) or (phase == "stop" and any("stop" in c for c in previous))
    state = os.getenv("TEST_DB_STATE", "") if changed and service == "db" else ""
    if state == "volume-owner":
        (root / "changed-volume-owner").touch()
    if state == "missing":
        raise SystemExit(1)
    output = json.dumps([{"Id": ("e" if state == "container" else "c") * 64,
      "Image": "" if state == "unknown-image" else "sha256:" + ("e" if state == "image" else "d") * 64,
      "State": {"Running": state != "stopped"}, "Config": {"Labels": {
      "com.docker.compose.project": "wrong" if fail == "project" or state == "project" else "backend", "com.docker.compose.service": service},
      "Cmd": ["--model-id", "custom/model"], "Env": (["EMBEDDING_DIMENSION=768"] if fail in ("legacy-revision", "unknown-revision") else ["REVISION=" + ("main" if fail == "symbolic-revision" else "a" * 40), "EMBEDDING_DIMENSION=768"])},
      "Mounts": [{"Destination": "/var/lib/postgresql/data", "Name": "other_volume" if state == "mount" else "backend_postgres_data"}]}])
elif args[:2] == ["volume", "inspect"]:
    output = json.dumps([{"Labels": {"com.docker.compose.project": "other" if (root / "changed-volume-owner").exists() else "backend", "com.docker.compose.volume": "postgres_data"}}])
elif args[:2] == ["image", "inspect"]:
    output = "sha256:new"
elif args[0] == "inspect" and args[2] == "{{.Image}}":
    output = "sha256:old" if fail == "stale-worker" else "sha256:new"
elif args[0] == "exec":
    if args[-1] == "true":
        status = 1 if fail == "missing" else 0
    elif "http://localhost:80/info" in args:
        output = json.dumps({"model_id": "custom/model", "model_sha": None if fail == "unknown-revision" else os.getenv("TEST_OBSERVED_REVISION", "a" * 40)})
    elif "pg_dump" in args:
        output = "-- PostgreSQL database dump\n" * 2000
        status = 1 if fail == "dump" else 0
    elif "python" in args:
        output = re.search(r'^version\s*=\s*"([^"]+)"',
                           (checkout / 'backend/pyproject.toml').read_text(), re.MULTILINE).group(1)
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
            elif "to_regclass" in sql and "embedding_regime" in sql:
                output = "t"
            elif "SELECT profile" in sql:
                output = os.getenv("TEST_ACTIVE_PROFILE", "")
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


def candidate_version(checkout):
    return re.search(r'^version\s*=\s*"([^"]+)"',
                     (checkout / 'backend/pyproject.toml').read_text(), re.MULTILINE).group(1)


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
    version = candidate_version(checkout)
    script = re.sub(r'"version": "[^"]+"', f'"version": "{version}"', mcp.read_text())
    script = script.replace('assert sys.argv[1] == "--env-file"',
        f'if sys.argv[1:] == ["--version"]:\n    print("sill-mcp {version}")\n    raise SystemExit()\nassert sys.argv[1] == "--env-file"')
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
    assert choice["EMBEDDING_REVISION"] == "a" * 40


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
    script = mcp.read_text().replace(f'"version": "{candidate_version(upgrade[0])}"', '"version": "0.0.1"')
    mcp.write_text(script)
    result = run(upgrade, "upgrade.sh", "--no-verify", TEST_UPGRADE_FAILURE="current")
    assert result.returncode != 0
    assert "MCP running version" in result.stderr
    assert "Upgrade complete" not in result.stdout


@pytest.mark.parametrize("dry,force", [(True, False), (True, True), (False, True)])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("codex", [False, True])
def test_hook_review_output_excludes_settings_and_command_secrets(tmp_path, capsys, dry, force, compact, codex):
    import shlex
    mod = hooks_module()
    template = ROOT / "plugin/codex.hooks.json.template"
    plugin = ROOT / "plugin"
    dst = tmp_path / "settings.json"
    data = mod.render(template, plugin, "python3", "/selected/instance.env")
    data["env"] = {"PRIVATE_API_TOKEN": "top-level-secret-fixture"}
    data["unrelated_config_marker"] = {"value": "unrelated-setting-fixture"}
    managed = data["hooks"]["Stop"][0]["hooks"][0]
    managed["command"] = shlex.join(["env", "API_TOKEN=managed-env-secret-fixture", "python3",
        str(plugin / "hooks/response-patterns.py"), "--token", "managed-argument-secret-fixture"])
    managed["statusMessage"] = "managed-setting-secret-fixture"
    data["hooks"]["Stop"][0]["matcher"] = "managed-matcher-secret-fixture"
    data["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "unrelated-hook-fixture --token unrelated-token-secret-fixture"})
    original = json.dumps(data, indent=None if compact else 2)
    dst.write_text(original)
    mod.refresh(template, dst, plugin, "python3", "/selected/instance.env", codex=codex, dry=dry, force=force)
    captured = capsys.readouterr()
    logs = captured.out + captured.err
    for hidden in ["PRIVATE_API_TOKEN", "top-level-secret-fixture", "unrelated_config_marker", "unrelated-setting-fixture",
                   "managed-env-secret-fixture", "managed-argument-secret-fixture", "managed-setting-secret-fixture", "managed-matcher-secret-fixture",
                   "unrelated-hook-fixture", "unrelated-token-secret-fixture"]:
        assert hidden not in logs
    assert "response-patterns.py" in logs and "Stop" in logs
    assert "refresh" in logs.lower() or "replace" in logs.lower()
    if dry:
        assert dst.read_text() == original
    else:
        written = json.loads(dst.read_text())
        assert written["env"] == data["env"]
        assert written["unrelated_config_marker"] == data["unrelated_config_marker"]
        assert any(h["command"] == "unrelated-hook-fixture --token unrelated-token-secret-fixture"
                   for entry in written["hooks"]["Stop"] for h in entry["hooks"])



@pytest.mark.parametrize("prior_revision", ["legacy-revision", "symbolic-revision"])
def test_omitted_identity_survives_upgrade_and_independent_install(upgrade, prior_revision):
    checkout, home, env = upgrade
    path = checkout / 'backend/.env'
    original = b'# operator bytes\nPOSTGRES_PORT=5432\n'
    path.write_bytes(original)
    result = run(upgrade,'upgrade.sh','--no-verify',TEST_UPGRADE_FAILURE=prior_revision)
    assert result.returncode == 0, details(result)
    from instance_config import load_instance_env
    saved = load_instance_env(path)
    assert saved['EMBEDDING_MODEL_ID'] == 'custom/model'
    assert saved['EMBEDDING_REVISION'] == 'a'*40
    assert saved['EMBEDDING_DIMENSION'] == '768'
    assert path.read_bytes().startswith(original)
    backup, = path.parent.glob('.env.embedding-identity.bak-*')
    assert backup.read_bytes() == original and backup.stat().st_mode & 0o777 == 0o600
    first = path.read_bytes()
    result = run(upgrade,'install.sh','--no-seed',TEST_UPGRADE_FAILURE=prior_revision)
    assert result.returncode == 0, details(result)
    choice = json.loads((Path(env['TEST_FIXTURE'])/'embedding-choice').read_text())
    assert choice['EMBEDDING_REVISION'] == 'a'*40
    assert choice['EMBEDDING_MODEL_ID'] == 'custom/model'
    assert choice['EMBEDDING_DIMENSION'] == '768'
    assert path.read_bytes() == first
    assert any('SELECT profile' in c[-1] for c in calls(upgrade))


@pytest.mark.parametrize('field,value', [('EMBEDDING_REVISION','different'),('EMBEDDING_MODEL_ID','different/model'),('EMBEDDING_DIMENSION','384')])
def test_install_existing_identity_conflict_precedes_runtime(upgrade, field, value):
    result = run(upgrade,'install.sh','--no-seed',**{field:value})
    assert result.returncode != 0, details(result)
    assert field in result.stderr
    assert not any('up' in c or 'build' in c for c in calls(upgrade))


def test_install_existing_profile_identity_mismatch_is_refused(upgrade):
    profile = json.dumps({'model_id':'custom/model','revision':'other-commit','dimension':768})
    result = run(upgrade,'install.sh','--no-seed',TEST_ACTIVE_PROFILE=profile)
    assert result.returncode != 0, details(result)
    assert 'profile' in result.stderr.lower()
    assert not any('up' in c or 'build' in c for c in calls(upgrade))


def test_upgrade_dry_run_leaves_omitted_identity_undocumented(upgrade):
    path = upgrade[0]/'backend/.env'
    original = path.read_bytes()
    result = run(upgrade,'upgrade.sh','--dry-run',TEST_UPGRADE_FAILURE='legacy-revision')
    assert result.returncode == 0, details(result)
    assert path.read_bytes() == original
    assert not list(path.parent.glob('.env.embedding-identity.bak-*'))



def runtime_validator():
    spec = importlib.util.spec_from_file_location("upgrade_runtime", ROOT / "scripts/upgrade_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identity_pin_appends_only_missing_fields_and_retains_literal_bytes(tmp_path):
    mod = runtime_validator()
    path = tmp_path / '.env'
    original = b"# hand-written\r\nEMBEDDING_MODEL_ID='custom/model' # keep\r\nUNRELATED='literal $value'"
    path.write_bytes(original)
    path.chmod(0o640)
    actual = {'EMBEDDING_MODEL_ID':'custom/model','EMBEDDING_REVISION':'a'*40,'EMBEDDING_DIMENSION':'768'}
    mod.persist_identity(path,mod.snapshot(path),mod.load_instance_env(path),actual)
    assert path.read_bytes().startswith(original+b'\n')
    assert path.read_bytes().count(b'EMBEDDING_MODEL_ID=') == 1
    assert mod.load_instance_env(path) == dict(actual,UNRELATED='literal $value')
    backup, = tmp_path.glob('.env.embedding-identity.bak-*')
    assert backup.read_bytes() == original and backup.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o640
    before = path.read_bytes()
    mod.persist_identity(path,mod.snapshot(path),mod.load_instance_env(path),actual)
    assert path.read_bytes() == before and len(list(tmp_path.glob('*.bak-*'))) == 1


@pytest.mark.parametrize('when', ['before', 'during'])
def test_identity_pin_refuses_concurrent_dotenv_change(tmp_path, monkeypatch, when):
    mod = runtime_validator()
    path = tmp_path / '.env'
    path.write_bytes(b'# original\n')
    original = mod.snapshot(path)
    if when == 'before': path.write_bytes(b'# concurrent\n')
    else:
        stage = mod.stage
        def concurrent(*args):
            result = stage(*args)
            path.write_bytes(b'# concurrent\n')
            return result
        monkeypatch.setattr(mod,'stage',concurrent)
    with pytest.raises(ValueError,match='changed'):
        mod.persist_identity(path,original,{}, {'EMBEDDING_REVISION':'a'*40})
    assert path.read_bytes() == b'# concurrent\n'
    assert not list(tmp_path.glob('.embedding-identity-tmp-*'))


def test_identity_pin_refuses_explicit_choice_and_symlink(tmp_path):
    mod = runtime_validator()
    path = tmp_path / '.env'
    path.write_text('EMBEDDING_REVISION=chosen\n')
    with pytest.raises(ValueError,match='conflicts'):
        mod.persist_identity(path,mod.snapshot(path),mod.load_instance_env(path),{'EMBEDDING_REVISION':'a'*40})
    assert path.read_text() == 'EMBEDDING_REVISION=chosen\n'
    link = tmp_path/'link'; link.symlink_to(path)
    with pytest.raises(OSError): mod.snapshot(link)


def test_existing_install_dry_run_validates_without_pinning(upgrade):
    path = upgrade[0]/'backend/.env'
    original = path.read_bytes()
    result = run(upgrade,'install.sh','--dry-run',TEST_UPGRADE_FAILURE='legacy-revision')
    assert result.returncode == 0, details(result)
    assert path.read_bytes() == original
    assert not list(path.parent.glob('.env.embedding-identity.bak-*'))
    assert any('http://localhost:80/info' in c for c in calls(upgrade))
    assert not any('up' in c or 'build' in c for c in calls(upgrade))


@pytest.mark.parametrize('script', ['install.sh', 'upgrade.sh'])
def test_explicit_symbolic_pin_is_preserved_and_restart_refused(upgrade, script):
    path = upgrade[0]/'backend/.env'
    original = b'# explicit choice\nEMBEDDING_REVISION=main\n'
    path.write_bytes(original)
    result = run(upgrade,script,TEST_UPGRADE_FAILURE='symbolic-revision')
    assert result.returncode != 0
    assert 'TEI /info model_sha ' + 'a'*40 in result.stderr
    assert path.read_bytes() == original
    assert not any('up' in c or 'build' in c or 'pg_dump' in c for c in calls(upgrade))


def test_installer_checks_loaded_sha_even_when_configured_pins_match(upgrade):
    path = upgrade[0]/'backend/.env'
    path.write_text('EMBEDDING_REVISION='+'a'*40+'\n')
    original = path.read_bytes()
    result = run(upgrade,'install.sh','--no-seed',TEST_OBSERVED_REVISION='b'*40)
    assert result.returncode != 0
    assert 'EMBEDDING_REVISION' in result.stderr
    assert path.read_bytes() == original
    assert not any('up' in c or 'build' in c for c in calls(upgrade))


def test_fresh_identity_preflight_needs_no_installed_python_dependencies(tmp_path):
    import os
    executable(tmp_path/'docker', '#!/bin/sh\nexit 0\n')
    result = subprocess.run([sys.executable,'-I','-S',str(ROOT/'scripts/upgrade_runtime.py'),
        str(tmp_path/'.env'),'--install'], input=json.dumps({'services':{},'volumes':{}}),
        env=dict(os.environ,PATH=str(tmp_path)+':/usr/bin:/bin'), text=True,capture_output=True)
    assert result.returncode == 0, details(result)
    assert json.loads(result.stdout) == {}
    assert not (tmp_path/'.env').exists()


@pytest.mark.parametrize("script,version", [("install.sh", "current"), ("upgrade.sh", ""), ("upgrade.sh", "current")])
def test_existing_runtime_never_builds_or_recreates_database(upgrade, script, version):
    result = run(upgrade, script, "--no-seed" if script == "install.sh" else "--no-verify", TEST_UPGRADE_FAILURE=version)
    assert result.returncode == 0, details(result)
    commands = [c[7:] for c in calls(upgrade) if c[0] == "compose"]
    builds = [c for c in commands if c[0] == "build"]
    assert builds == [["build", "maintenance_worker"]]
    starts = [c for c in commands if c[0] == "up"]
    assert starts and all("db" not in c and "--no-deps" in c for c in starts)
    assert any("maintenance_worker" in c for c in starts)
    assert "retaining" in result.stdout.lower() and "database" in result.stdout.lower()
    assert "_database" not in (upgrade[0] / "backend/.env").read_text()


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
@pytest.mark.parametrize("state", ["stopped", "missing", "unknown-image"])
def test_existing_database_must_be_running_and_identifiable_before_runtime_mutation(upgrade, script, state):
    result = run(upgrade, script, TEST_DB_STATE=state)
    assert result.returncode != 0, details(result)
    assert not any("build" in c or "up" in c or "pg_dump" in c for c in calls(upgrade))


@pytest.mark.parametrize("script,phase,state", [
    (script, phase, state)
    for script, phase in [("upgrade.sh", "backup"), ("upgrade.sh", "build"),
                          ("upgrade.sh", "stop"), ("upgrade.sh", "worker"),
                          ("install.sh", "build"), ("install.sh", "worker")]
    for state in ["container", "image"]
] + [(script, "build", state) for script in ["install.sh", "upgrade.sh"]
     for state in ["project", "mount", "volume-owner", "stopped", "missing"]])
def test_changed_database_is_never_substituted_or_reported_success(upgrade, script, phase, state):
    result = run(upgrade, script, "--no-seed" if script == "install.sh" else "--no-verify",
                 TEST_DB_STATE=state, TEST_DB_CHANGE_AFTER=phase)
    assert result.returncode != 0, details(result)
    assert "Upgrade complete" not in result.stdout and "Sill is installed" not in result.stdout
    commands = [c[7:] for c in calls(upgrade) if c[0] == "compose"]
    assert not any(c[0] in ("build", "up") and ("db" in c or c == ["build"]) for c in commands)
    if phase in ("backup", "build", "stop"):
        assert not (Path(upgrade[2]["TEST_FIXTURE"]) / "applied-sql").exists()
        assert not any(c[0] in ("up", "start") for c in commands)


@pytest.mark.parametrize("script", ["install.sh", "upgrade.sh"])
def test_existing_database_dry_run_checks_without_runtime_changes(upgrade, script):
    before = (upgrade[0] / "backend/.env").read_bytes()
    result = run(upgrade, script, "--dry-run")
    assert result.returncode == 0, details(result)
    assert before == (upgrade[0] / "backend/.env").read_bytes()
    assert not any("build" in c or "up" in c or "pg_dump" in c for c in calls(upgrade))
