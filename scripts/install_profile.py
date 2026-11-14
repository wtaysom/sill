"""Fresh-install profile receipt. Never infers historical identity from live state.

Only called for explicit embeddinggemma-search-v1, before initdb and then
between schema/service readiness and maintenance/client startup.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from embedding_profiles import EmbeddingProfile
from instance_config import instance_dsn, load_instance_env
from scripts.reembed_documents import ProfileMigration

MODEL = "unsloth/embeddinggemma-300m"
REVISION = "bfa3c846ac738e62aa61806ef9112d34acb1dc5a"
LABEL = "org.sill.fresh-profile-intent"
SOURCE = EmbeddingProfile(MODEL, REVISION, 768)
TARGET = EmbeddingProfile(MODEL, REVISION, 768,
                          "task: search result | query: ", "title: none | text: ")
RECONCILE = (
    "Profile preparation was started without a saved operation ID; refusing a second prepare. "
    "Operator reconciliation required: inspect backend/scripts/reembed_documents.py status "
    "with the selected DSN environment variable, compare source/target and backup with the "
    "private receipt, then explicitly resume and activate that operation. "
    "See docs/EMBEDDING-PROFILES.md. Retain the receipt, volume and backups."
)


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError("Could not verify/create the selected profile volume or container; inspect Docker and retry")
    return result.stdout


def selection(config):
    db, embeddings = config["services"]["db"], config["services"]["embeddings"]
    env, command = embeddings["environment"], embeddings["command"]
    if (env.get("EMBEDDING_MODEL_ID") != MODEL or env.get("REVISION") != REVISION
            or str(env.get("EMBEDDING_DIMENSION")) != "768"
            or command.count("--model-id") != 1
            or command[command.index("--model-id") + 1] != MODEL
            or "app.embedding_dimension=768" not in db["command"]):
        raise ValueError("Prompted installation requires the tested unsloth model, immutable revision and dimension 768; retain raw for custom configurations")
    mounts = [v for v in db["volumes"] if v["target"] == "/var/lib/postgresql/data"]
    if len(mounts) != 1 or mounts[0]["type"] != "volume":
        raise ValueError("Prompted installation requires a newly created managed database volume")
    key = mounts[0]["source"]
    volume = config["volumes"][key]
    if volume.get("external") or volume.get("driver", "local") != "local" or volume.get("driver_opts"):
        raise ValueError("Prompted installation requires a new local database volume without driver options")
    return {"project": config["name"], "volume_key": key, "volume": volume["name"],
            "database": db["environment"]["POSTGRES_DB"], "container": db["container_name"],
            "source": SOURCE.to_dict(), "target": TARGET.to_dict(),
            "fingerprint": TARGET.fingerprint}


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Profile receipt/lock must be a private regular file owned by the current user (mode 0600)")


@contextmanager
def locked(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        private_file(path)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def save(path, receipt):
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(path, selected):
    private_file(path)
    receipt = json.loads(path.read_text())
    if receipt["selection"] != selected:
        raise ValueError("Profile receipt does not match the requested configuration/fingerprint; refusing to change this store")
    return receipt


def volume_identity(selected, nonce):
    volume, = json.loads(docker("volume", "inspect", selected["volume"]))
    if (volume["Name"] != selected["volume"] or volume.get("Labels", {}).get(LABEL) != nonce
            or volume["Driver"] != "local" or volume.get("Options")):
        raise ValueError("Selected database volume lacks matching creation evidence; refusing prompted installation")
    return {"name": volume["Name"], "created_at": volume["CreatedAt"], "nonce": nonce}


def reserve(path, selected):
    names = docker("volume", "ls", "--format", "{{.Name}}").splitlines()
    if path.exists() or path.is_symlink():
        receipt = read(path, selected)
    else:
        if selected["volume"] in names:
            raise ValueError("Existing database volume has no matching creation evidence; retain the active/raw profile")
        receipt = {"selection": selected, "nonce": str(uuid.uuid4()),
                   "volume_identity": None, "database_identity": None,
                   "prepare_started": False, "operation_id": None}
        # Durable configuration/intent precedes volume creation and raw initdb seeds.
        save(path, receipt)
    if selected["volume"] not in names:
        if receipt["volume_identity"] is not None:
            raise ValueError("Previously recorded database volume is missing; refusing to recreate it")
        docker("volume", "create", "--label", LABEL + "=" + receipt["nonce"],
               "--label", "com.docker.compose.project=" + selected["project"],
               "--label", "com.docker.compose.volume=" + selected["volume_key"], selected["volume"])
    identity = volume_identity(selected, receipt["nonce"])
    if receipt["volume_identity"] is None:
        receipt["volume_identity"] = identity
        save(path, receipt)
    elif receipt["volume_identity"] != identity:
        raise ValueError("Database volume identity differs from the creation receipt")


def matching_operation(status, receipt):
    operation = status["operation"]
    if (not operation or operation["id"] != receipt["operation_id"]
            or operation["source_profile"] != SOURCE.to_dict()
            or operation["target_profile"] != TARGET.to_dict()
            or operation["state"] != "preparing"):
        raise ValueError("Latest embedding operation differs from saved installer intent/operation ID; operator reconciliation required")


def activate(path, selected, runtime_env):
    receipt = read(path, selected)
    if receipt["volume_identity"] != volume_identity(selected, receipt["nonce"]):
        raise ValueError("Database volume identity differs from the creation receipt")
    container, = json.loads(docker("inspect", selected["container"]))
    if not any(m.get("Type") == "volume" and m.get("Name") == selected["volume"]
               and m["Destination"] == "/var/lib/postgresql/data" for m in container["Mounts"]):
        raise ValueError("Selected running database does not mount the proven-created volume")
    operator = ProfileMigration(instance_dsn(load_instance_env(runtime_env)))
    with operator.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT system_identifier::text,current_database(),(SELECT oid::text FROM pg_database WHERE datname=current_database()) FROM pg_catalog.pg_control_system()")
            identity = list(cur.fetchone())
    if identity[1] != selected["database"]:
        raise ValueError("Selected database differs from the creation intent")
    if receipt["database_identity"] is None:
        receipt["database_identity"] = identity
        save(path, receipt)
    elif receipt["database_identity"] != identity:
        raise ValueError("Database identity differs from the creation receipt")
    status = operator.status()
    if status["active_fingerprint"] == TARGET.fingerprint:
        print("Embedding profile active: " + TARGET.fingerprint + " (already satisfied)")
        return
    if status["active_profile"] not in (None, SOURCE.to_dict()):
        raise ValueError("An incompatible profile is already active; the installer will not change it")
    if receipt["operation_id"] is None:
        if receipt["prepare_started"]:
            raise ValueError(RECONCILE)
        if status["operation"]:
            raise ValueError("An existing embedding operation is not owned by this installer receipt; operator reconciliation required")
        receipt["prepare_started"] = True
        save(path, receipt)
        result = operator.prepare(TARGET, source_attestation=SOURCE,
                                  backup_dir=path.parent / ".env.embedding-profile-backups",
                                  pg_tools_container=selected["container"])
        receipt["operation_id"] = result["id"]
        save(path, receipt)
    matching_operation(operator.status(), receipt)
    result = operator.resume()
    if result["remaining"]:
        raise ValueError("Profile staging remains incomplete; rerun the same installer selection to resume")
    matching_operation(operator.status(), receipt)
    operator.activate()
    if operator.status()["active_fingerprint"] != TARGET.fingerprint:
        raise ValueError("Profile activation did not produce the requested fingerprint")
    print("Embedding profile active: " + TARGET.fingerprint)


def main():
    try:
        action, env_file, runtime_env = sys.argv[1:]
        path = Path(env_file).with_name(".env.embedding-profile.json")
        selected = selection(json.load(sys.stdin))
        with locked(path.with_suffix(".lock")):
            if action == "reserve":
                reserve(path, selected)
            elif action == "activate":
                activate(path, selected, Path(runtime_env))
            else:
                raise ValueError("Invalid profile installer phase")
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
    except Exception:
        # Database errors, JSON, subprocess and path failures can contain secrets.
        print("Profile installation failed; retain the private receipt and backups. Check the selected database/service and rerun; see docs/EMBEDDING-PROFILES.md.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
