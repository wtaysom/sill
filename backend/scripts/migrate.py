"""Transaction-aware migration runner for upgrade.sh and integration parity tests.

Frozen files may contain one outer BEGIN/COMMIT pair. Parse SQL lexical
boundaries (including dollar quotes), remove only that pair, and execute
DDL and the ledger stamp in one server transaction. No psql -1 assumption.

Preflight is read-only. Full apply fences the selected database via its admin
DB while retaining one migration connection, and refuses other client sessions
without terminating them. SIGINT/SIGTERM restore the original allowance. A
hard kill leaves a private marker; ordinary preflight then refuses to connect.
The printed `recover` command verifies cluster and database OID, requires the
migration session to have ended, restores only that database, and removes the
marker. It never recovers automatically or discards an unknown marker.
"""
import argparse
import json
import os
import signal
import select
import time
import tempfile
import uuid
from pathlib import Path
import re
import subprocess
import sys


def statements(sql):
    """Split SQL outside quotes/comments; retain function bodies verbatim."""
    result, buf = [], []
    i = 0
    while i < len(sql):
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end
            buf.append("\n")
        elif sql.startswith("/*", i):
            depth, end = 1, i + 2
            while depth and end < len(sql):
                if sql.startswith("/*", end):
                    depth += 1
                    end += 2
                elif sql.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            if depth:
                raise ValueError("Unterminated SQL comment")
            buf.append(" ")
            i = end
        elif sql[i] in "'\"" or sql[i] == "$" and re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", sql[i:]):
            start = i
            if sql[i] == "$":
                tag = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", sql[i:]).group()
                end = sql.find(tag, i + len(tag))
                if end < 0:
                    raise ValueError("Unterminated SQL dollar quote")
                i = end + len(tag)
            else:
                quote = sql[i]
                escape = i > 0 and sql[i - 1] in "eE" and (i < 2 or not sql[i - 2].isalnum())
                i += 1
                while i < len(sql):
                    if escape and sql[i] == "\\":
                        i += 2
                    elif sql[i] == quote:
                        i += 1
                        if i < len(sql) and sql[i] == quote:
                            i += 1
                        else:
                            break
                    else:
                        i += 1
                else:
                    raise ValueError("Unterminated SQL quote")
            buf.append(sql[start:i])
        elif sql[i] == ";":
            value = "".join(buf).strip()
            if value:
                result.append(value)
            buf = []
            i += 1
        else:
            buf.append(sql[i])
            i += 1
    if "".join(buf).strip():
        result.append("".join(buf).strip())
    return result


def pending_ids(known, applied):
    if known != [f"{n:03}" for n in range(1, len(known) + 1)] or applied != known[:len(applied)]:
        raise ValueError("Migration ledger must be a contiguous known prefix; unknown, duplicate or gapped stamps")
    return known[len(applied):]


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def atomic_sql(path, ledger_schema, applied, create_ledger=False):
    body = statements(path.read_text())
    if body and body[0].upper() == "BEGIN" and body[-1].upper() == "COMMIT":
        body = body[1:-1]
    for part in body:
        if re.match(r"^(BEGIN|COMMIT|END|ROLLBACK|START\s+TRANSACTION|PREPARE\s+TRANSACTION|VACUUM|\\)", part, re.I):
            raise ValueError(f"Unsupported transaction boundary in {path.name}")
    ledger = f'{ident(ledger_schema)}."schema_migrations"'
    migration_id = path.name[:3]
    if not re.fullmatch(r"\d{3}", migration_id):
        raise ValueError("Invalid migration filename")
    opening = ["BEGIN", f"SET LOCAL search_path = {ident(ledger_schema)}, public, ag_catalog", "SELECT pg_advisory_xact_lock(1397312588)"]
    if create_ledger:
        opening += [f"CREATE TABLE {ledger} (migration_id text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now(), source text NOT NULL DEFAULT 'upgrade.sh')",
                    f"COMMENT ON TABLE {ledger} IS 'Applied schema migrations. Fresh installs are stamped by initdb; upgrades are stamped by upgrade.sh. verify.sh check 5 compares this against backend/migrations/.'"]
    expected = "ARRAY[" + ",".join("'" + s + "'" for s in applied) + "]::text[]"
    opening += [f"LOCK TABLE {ledger} IN EXCLUSIVE MODE",
                f"DO $ledger$ BEGIN IF (SELECT COALESCE(array_agg(migration_id ORDER BY migration_id), ARRAY[]::text[]) FROM {ledger}) <> {expected} THEN RAISE EXCEPTION 'Migration ledger changed since preflight'; END IF; END $ledger$"]
    return ";\n".join(opening + body + [f"INSERT INTO {ledger} (migration_id) VALUES ('{migration_id}')", "COMMIT"]) + ";\n"


class Database:
    def __init__(self, container, user, database):
        self.command = ["docker", "exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", database, "-tA"]

    def query(self, sql):
        return subprocess.run(self.command + ["-c", sql], text=True, capture_output=True, check=True).stdout.strip()

    def apply(self, sql):
        subprocess.run(self.command, input=sql, text=True, check=True)


def inspect_state(db, directory):
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    known = [p.name[:3] for p in files]
    if not known:
        raise ValueError("No migration files found")
    major = int(db.query("SHOW server_version_num")) // 10000
    if major != 16:
        raise ValueError("Upgrade requires PostgreSQL 16")
    ledgers = json.loads(db.query("SELECT COALESCE(json_agg(n.nspname), '[]') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname='schema_migrations' AND c.relkind IN ('r','p')"))
    if len(ledgers) > 1:
        raise ValueError("Multiple schema_migrations ledgers; resolve ambiguity manually")
    # initdb creates its ledger on the configured search_path (often AGE).
    schema = ledgers[0] if ledgers else db.query("SELECT current_schema()")
    if schema not in {"public", "ag_catalog"}:
        raise ValueError("Unsupported migration ledger namespace")
    applied = db.query(f'SELECT migration_id FROM {ident(schema)}.schema_migrations ORDER BY migration_id').splitlines() if ledgers else []
    pending = pending_ids(known, applied)
    extensions = set(db.query("SELECT extname FROM pg_extension").splitlines())
    if not {"vector", "age", "http"} <= extensions:
        raise ValueError("Unsupported database extensions; expected vector, AGE and http")
    # Namespaces vary in supported installs; callable lookup follows the
    # server's existing search_path, just as the frozen migrations do.
    signatures = ["embedding_dimension()", "public.get_embedding(text)",
                  "public.discover_relationship(uuid,uuid,graph_edge_type,double precision,text,uuid,text)"]
    if len(applied) >= 5:
        signatures.append("touch_memory_access(uuid)")
    if len(applied) >= 8:
        signatures.append("public.supersede_memory(uuid,text,text,jsonb)")
    for signature in signatures:
        if db.query("SELECT to_regprocedure('" + signature + "') IS NOT NULL") != "t":
            raise ValueError("Unsupported schema callable: " + signature)
    for number, name in [(2, "session_activity"), (6, "memory_reuse_events")]:
        exists = db.query(f"SELECT to_regclass('{name}') IS NOT NULL") == "t"
        if exists != (len(applied) >= number):
            raise ValueError("Schema shape disagrees with ledger: " + name)
    columns = json.loads(db.query("SELECT COALESCE(json_object_agg(attname, format_type(atttypid, atttypmod)), '{}') FROM pg_attribute WHERE attrelid=to_regclass('public.memories') AND attnum>0 AND NOT attisdropped"))
    baseline = {"id": "uuid", "content": "text", "source_attribution": "jsonb", "importance": "double precision", "access_count": "integer", "trust_level": "double precision"}
    if any(columns.get(key) != value for key, value in baseline.items()):
        raise ValueError("Unsupported legacy memories shape")
    match = re.fullmatch(r"(?:public\.)?vector\((\d+)\)", columns.get("embedding", ""))
    if not match:
        raise ValueError("Unsupported embedding column")
    # Stamp-dependent shape guards catch a partially or manually altered lane.
    for number, names in [(1, ["force", "speaker"]), (9, ["production", "minted_in"]), (10, ["restates_id"])]:
        for name in names:
            if (name in columns) != (len(applied) >= number):
                raise ValueError(f"Schema shape disagrees with ledger: memories.{name}")
    baseline_tables = ["episodic_memories", "semantic_memories", "procedural_memories", "strategic_memories", "working_memory", "embedding_config", "memory_changes", "identity_aspects"]
    present = json.loads(db.query("SELECT json_agg(relname) FROM pg_class WHERE relkind='r' AND relnamespace='public'::regnamespace"))
    if not set(baseline_tables) <= set(present or []):
        raise ValueError("Unsupported legacy table set")
    dim = int(db.query("SELECT embedding_dimension()"))
    if dim != int(match.group(1)):
        raise ValueError("Embedding configuration and stored dimension differ")
    for path in files[len(applied):]:
        atomic_sql(path, schema, applied, not ledgers)
    return {"ledger_schema": schema, "create_ledger": not ledgers, "applied": applied, "pending": pending, "dimension": dim}


def admin_database(database):
    return "template1" if database == "postgres" else "postgres"


def fence_identity(db, database):
    literal = "'" + database.replace("'", "''") + "'"
    return json.loads(db.query("SELECT json_build_object('system_id', system_identifier::text, "
        "'database_oid', d.oid, 'allow_connections', d.datallowconn, "
        "'superuser', (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)) "
        "FROM pg_control_system(), pg_database d WHERE d.datname=" + literal))


def recovery_command(args):
    import shlex
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "recover", "--container", args.container,
                      "--user", args.user, "--database", args.database, "--migrations", str(args.migrations),
                      "--recovery-file", str(args.recovery_file)])


def restore_fence(args, marker, admin):
    actual = fence_identity(admin, args.database)
    if marker["container"] != args.container or marker["database"] != args.database or any(
        actual.get(key) != marker[key] for key in ("system_id", "database_oid")
    ):
        raise ValueError("Recovery identity differs; refusing to change any database")
    app = marker["application_name"].replace("'", "''")
    if admin.query(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{app}'") != "0":
        raise ValueError("Migration session is still active; wait for it to finish before recovery")
    allowed = "true" if marker["allow_connections"] else "false"
    admin.query(f"ALTER DATABASE {ident(args.database)} ALLOW_CONNECTIONS {allowed}")
    args.recovery_file.unlink()


def apply_fenced(args, db, state, identity, admin):
    if not state["pending"]:
        return
    args.recovery_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = {**identity, "container": args.container, "database": args.database,
              "application_name": "sill-upgrade-" + uuid.uuid4().hex}
    # Publish complete identity metadata atomically and exclusively before
    # fencing. An interrupted write cannot leave a half-JSON recovery file.
    fd, temporary = tempfile.mkstemp(prefix=".upgrade-fence-", dir=args.recovery_file.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(marker, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, args.recovery_file)
    finally:
        os.unlink(temporary)
    print(f"Recovery marker: {args.recovery_file}\nIf interrupted: {recovery_command(args)}", flush=True)
    script = ""
    applied = state["applied"][:]
    for path in sorted(args.migrations.glob("[0-9][0-9][0-9]_*.sql"))[len(applied):]:
        script += atomic_sql(path, state["ledger_schema"], applied, state["create_ledger"] and not applied)
        applied.append(path.name[:3])
        script += "\\echo Applied " + applied[-1] + "\n"
    command = db.command[:2] + ["-e", "PGAPPNAME=" + marker["application_name"]] + db.command[2:]
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        process.stdin.write("SELECT pg_backend_pid();\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 30)
        pid = process.stdout.readline().strip() if ready else ""
        if not pid.isdigit():
            raise ValueError("Migration connection did not become ready within 30s")
        # PostgreSQL cannot disallow connections from the target DB itself.
        # Its already-established migration session survives this admin-DB
        # fence; no new client, even a superuser, can connect afterward.
        admin.query(f"ALTER DATABASE {ident(args.database)} ALLOW_CONNECTIONS false")
        others = admin.query(f"SELECT count(*) FROM pg_stat_activity WHERE datid={identity['database_oid']} AND pid<>{pid} AND backend_type='client backend'")
        if others != "0":
            raise ValueError("Other database clients remain. Pause the beat schedule, close connected agent/MCP sessions, then retry; their transactions were not terminated.")
        output, _ = process.communicate(script)
        print(output, end="", flush=True)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    except BaseException:
        # Only this upgrader's connection may be terminated, never a
        # client session. docker exec may already have exited on a signal
        # while its remote psql is still alive.
        app = marker["application_name"]
        admin.query(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='{app}' AND datid={marker['database_oid']}")
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        raise
    finally:
        try:
            for attempt in range(50):
                if admin.query("SELECT count(*) FROM pg_stat_activity WHERE application_name='" + marker["application_name"] + "'") == "0":
                    break
                time.sleep(0.1)
            restore_fence(args, marker, admin)
        except Exception:
            print("Connection allowance recovery required: " + recovery_command(args), file=sys.stderr)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "apply", "recover"])
    parser.add_argument("--container", required=True)
    parser.add_argument("--user", default="sill")
    parser.add_argument("--database", default="sill")
    parser.add_argument("--migrations", type=Path, required=True)
    parser.add_argument("--recovery-file", type=Path)
    args = parser.parse_args(argv)
    if args.recovery_file is None:
        args.recovery_file = args.migrations.resolve().parents[1] / "backups/.upgrade-fence.json"
    admin = Database(args.container, args.user, admin_database(args.database))
    if args.recovery_file.exists():
        if args.action != "recover":
            raise ValueError("An interrupted upgrade needs explicit recovery before connecting: " + recovery_command(args))
        marker = json.loads(args.recovery_file.read_text())
        restore_fence(args, marker, admin)
        print("Restored original connection allowance for the verified database")
        return
    if args.action == "recover":
        raise ValueError("No interrupted-upgrade marker exists")
    identity = fence_identity(admin, args.database)
    if not identity or not identity["superuser"] or not identity["allow_connections"]:
        raise ValueError("Upgrade needs a superuser, an accessible admin database, and the selected database accepting connections")
    db = Database(args.container, args.user, args.database)
    state = inspect_state(db, args.migrations)
    if args.action == "preflight":
        print(json.dumps(state))
        return
    try:
        apply_fenced(args, db, state, identity, admin)
    except subprocess.CalledProcessError:
        ledger = f'{ident(state["ledger_schema"])}.schema_migrations'
        exists = db.query("SELECT to_regclass('" + ledger.replace("'", "''") + "') IS NOT NULL") == "t"
        last = db.query(f'SELECT max(migration_id) FROM {ledger}') if exists else "none"
        print(f"Migration failed; last successful stamp: {last or 'none'}", file=sys.stderr)
        raise


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        sys.exit(1)
