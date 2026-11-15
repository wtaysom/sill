"""Validate existing database ownership and preserve the installed embedding space."""
import fcntl
import stat
import tempfile
import json
import os
import re
from pathlib import Path
import subprocess
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "backend"),
                str(Path(__file__).resolve().parent)]


def load_instance_env(path):
    # A fresh --dry-run may precede installation of python-dotenv.
    from instance_config import load_instance_env as load
    return load(path)


def inspect(kind, name):
    return json.loads(subprocess.check_output(["docker", kind, "inspect", name], text=True))[0]


def database_identity(config, expected=None):
    """Identify the running container, its immutable image and its data volume.

    A managed update never lets Compose substitute another image or volume.
    The one deliberate change is `database_plan` below, which keeps both.
    """
    project = config["name"]
    service = config["services"]["db"]
    db = inspect("container", service["container_name"])
    labels = db["Config"].get("Labels") or {}
    if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.service") != "db":
        raise ValueError("Database does not belong to the requested Compose project")
    if not db["State"]["Running"]:
        raise ValueError("Database is stopped; start the installed instance with its existing configuration before upgrading")
    container_id, image_id = db.get("Id", ""), db.get("Image", "")
    if not re.fullmatch(r"[a-f0-9]{64}", container_id) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("Cannot prove the existing database container and immutable image identity")
    volumes = [v for v in service["volumes"] if v["target"] == "/var/lib/postgresql/data"]
    if len(volumes) != 1 or volumes[0]["type"] != "volume":
        raise ValueError("Unsupported database storage; expected the installed named Compose volume")
    source = volumes[0]["source"]
    volume_name = config["volumes"][source]["name"]
    mounts = [m for m in db["Mounts"] if m["Destination"] == "/var/lib/postgresql/data"]
    if len(mounts) != 1 or mounts[0].get("Name") != volume_name:
        raise ValueError("Requested Compose volume differs from the existing database volume")
    volume = inspect("volume", volume_name)
    labels = volume.get("Labels") or {}
    if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.volume") != source:
        raise ValueError("Database volume has unexpected Compose ownership")
    identity = {"project": project, "container_name": service["container_name"],
                "container_id": container_id, "image_id": image_id,
                "volume_name": volume_name, "volume_source": source}
    if expected is not None and identity != expected:
        raise ValueError("Database identity changed during this operation; no replacement will be started")
    return identity


EVERY_INTERFACE = {"", "0.0.0.0", "::"}


def database_plan(config, identity):
    """What to do about a database container older than this checkout's settings.

    v0.2 published PostgreSQL on every interface, with trust authentication
    and no restart policy. A restart policy can be changed in place. A port
    binding cannot: closing it takes a new container. That is planned only
    when the image Compose would start is provably the one this database
    already runs on, so the new container differs in settings and nothing
    else. Otherwise the container is left alone and the plan says why.
    """
    service = config["services"]["db"]
    host = inspect("container", service["container_name"]).get("HostConfig") or {}
    published = sorted({binding.get("HostIp") or "" for binding in
                        (host.get("PortBindings") or {}).get("5432/tcp") or []})
    wanted = [port for port in service.get("ports", []) if port.get("target") == 5432]
    bind = wanted[0].get("host_ip") or "0.0.0.0" if len(wanted) == 1 else None
    restart = (host.get("RestartPolicy") or {}).get("Name") or "no"
    wanted_restart = service.get("restart") or "no"
    plan = {"action": "keep", "container": service["container_name"], "image": identity["image_id"],
            "volume": identity["volume_name"], "bind": bind, "restart": wanted_restart,
            "published": [address or "0.0.0.0" for address in published]}
    if bind is not None and bind not in EVERY_INTERFACE and EVERY_INTERFACE.intersection(published):
        tag = service.get("image") or f'{config["name"]}-db'
        try:
            tagged = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                                             text=True, stderr=subprocess.DEVNULL).strip()
        except subprocess.CalledProcessError:
            tagged = ""
        plan["action"] = "recreate" if tagged == identity["image_id"] else "blocked"
        plan["tag"] = tag
    elif restart != wanted_restart:
        plan["action"] = "restart"
    return plan


def adopt_recreated(config, previous):
    """Accept the one container this run recreated: same image, same volume, new settings."""
    current = database_identity(config)
    if current["container_id"] == previous["container_id"] or current != {**previous, "container_id": current["container_id"]}:
        raise ValueError("The recreated database is not the same image on the same volume")
    if database_plan(config, current)["action"] != "keep":
        raise ValueError("The recreated database does not have this checkout's port and restart settings")
    return current


HEX = r"[a-fA-F0-9]{40,64}"


def cached_snapshot(container, env, model, label, pinned):
    """The model snapshot this service loaded, read from its download cache.

    The cache names each snapshot directory by commit and keeps one pointer
    per requested revision. A single snapshot that the pointer names is the
    only one the service can have loaded. With several, the operator's own
    explicit hex EMBEDDING_REVISION settles it when it names one of them.
    Anything else stays unproved, and the message says what was found.
    """
    root = env.get("HUGGINGFACE_HUB_CACHE") or "/data"
    ref = label or "main"
    unproved = ValueError(
        "Cannot prove the installed embedding revision: TEI /info reports none and the service's "
        f"model cache has no readable snapshot for {model}. See docs/EMBEDDING-PROFILES.md before upgrading")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", model)
            or not re.fullmatch(r"/[A-Za-z0-9_./-]*", root) or ".." in root
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref) or ".." in ref):
        raise unproved
    try:
        output = subprocess.check_output([
            "docker", "exec", container, "sh", "-c",
            'd="$1/$2"; cat "$d/refs/$3" 2>/dev/null; printf "\\n--\\n"; ls -1 "$d/snapshots" 2>/dev/null',
            "sill-cached-snapshot", root, "models--" + model.replace("/", "--"), ref,
        ], text=True)
    except subprocess.CalledProcessError:
        raise unproved from None
    pointer, _, listing = output.partition("\n--\n")
    pointer = pointer.strip()
    snapshots = [line.strip() for line in listing.splitlines() if re.fullmatch(HEX, line.strip())]
    if len(snapshots) == 1 and pointer == snapshots[0]:
        return pointer
    if isinstance(pinned, str) and re.fullmatch(HEX, pinned) and pinned in snapshots:
        return pinned
    if not snapshots:
        raise unproved
    raise ValueError(
        "Cannot prove the installed embedding revision: TEI /info reports none and the service's model "
        f"cache holds {len(snapshots)} snapshot(s) for {model} that its pointer does not settle: "
        + ", ".join(sorted(snapshots)) + ". Set EMBEDDING_REVISION in backend/.env to the one your stored "
        "vectors came from, then retry")


def validate(config, settings, dimension, *, install=False, observe_revision=False):
    project = config["name"]
    services = config["services"]
    for service in (("embeddings",) if install else ("embeddings", "maintenance_worker")):
        obj = inspect("container", services[service]["container_name"])
        labels = obj["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.service") != service:
            raise ValueError(f"{service} does not belong to the requested Compose project")
        if service == "embeddings":
            embedding = obj
    command = embedding["Config"]["Cmd"]
    model = command[command.index("--model-id") + 1]
    env = dict(x.split("=", 1) for x in embedding["Config"]["Env"] if "=" in x)
    revision = env.get("REVISION")
    source = "container REVISION"
    if install or observe_revision or not revision or not re.fullmatch(HEX, revision):
        # Omitted pins and mutable labels require the actually loaded snapshot.
        # Installer restarts also verify the loaded SHA, not label equality.
        container = services["embeddings"]["container_name"]
        try:
            info = json.loads(subprocess.check_output([
                "docker", "exec", container,
                "curl", "-fsS", "--max-time", "10", "http://localhost:80/info",
            ], text=True))
            if info.get("model_id") != model:
                raise ValueError()
            observed = info.get("model_sha")
        except (ValueError, subprocess.CalledProcessError):
            raise ValueError("Cannot prove the installed embedding revision from TEI /info; verify and persist EMBEDDING_REVISION before upgrading") from None
        source = "TEI /info model_sha"
        if isinstance(observed, str) and re.fullmatch(HEX, observed):
            revision = observed
        else:
            source = "cached model snapshot"
            # TEI's model_sha repeats the revision the service was started
            # with: null when it was started without one, as every v0.2 stack
            # was, and the label itself for a symbolic one. The download cache
            # records which snapshot that resolved to.
            revision = cached_snapshot(container, env, model, revision, settings.get("EMBEDDING_REVISION"))
    actual = {"EMBEDDING_MODEL_ID": model, "EMBEDDING_REVISION": revision, "EMBEDDING_DIMENSION": str(dimension)}
    if env.get("EMBEDDING_DIMENSION", str(dimension)) != str(dimension):
        raise ValueError("Running embeddings dimension differs from stored vectors")
    for key, value in actual.items():
        if key in settings and settings[key] != value:
            if key == "EMBEDDING_REVISION" and not re.fullmatch(r"[a-fA-F0-9]{40,64}", settings[key]):
                raise ValueError(f"EMBEDDING_REVISION is not the observed immutable pin. Keep the existing service; explicitly set its {source} {revision} in backend/.env and clear conflicting shell overrides, or use an explicit embedding migration")
            raise ValueError(f"{key} would change the installed embedding space; routine upgrades do not activate profiles")
    profile_exists = db_query(config, "SELECT to_regclass('public.embedding_regime') IS NOT NULL")
    if profile_exists == "t":
        encoded = db_query(config, "SELECT profile FROM public.embedding_regime")
        if encoded:
            profile = json.loads(encoded)
            if profile is not None and any(str(profile.get(field)) != actual[key] for field, key in (
                    ("model_id", "EMBEDDING_MODEL_ID"), ("revision", "EMBEDDING_REVISION"),
                    ("dimension", "EMBEDDING_DIMENSION"))):
                raise ValueError("Active embedding profile differs from the running identity; reconcile the explicit re-embedding migration before restarting")
    return actual


def db_query(config, sql):
    db = config["services"]["db"]
    return subprocess.check_output(["docker", "exec", db["container_name"], "psql", "-X", "-qAt",
        "-v", "ON_ERROR_STOP=1", "-U", db["environment"]["POSTGRES_USER"],
        "-d", db["environment"]["POSTGRES_DB"], "-c", sql], text=True).strip()


def existing_instance(config, path, fresh_profile=False):
    """Do not treat a stopped/missing service with retained data as fresh."""
    containers = subprocess.check_output(["docker", "container", "ls", "-a", "--format", "{{.Names}}"], text=True).splitlines()
    volumes = subprocess.check_output(["docker", "volume", "ls", "--format", "{{.Name}}"], text=True).splitlines()
    services = config["services"]
    if any(service.get("container_name") in containers for service in services.values()):
        return True
    existing = any(volume["name"] in volumes for volume in config.get("volumes", {}).values())
    if existing and fresh_profile:
        # A crash immediately after our volume create can precede the receipt's
        # identity save. No service has been started at this point. Verify that
        # exact pending intent and ownership before allowing reserve to resume.
        from install_profile import read, selection, volume_identity
        receipt_path = path.with_name(".env.embedding-profile.json")
        if receipt_path.exists():
            selected = selection(config)
            receipt = read(receipt_path, selected)
            if receipt["volume_identity"] is None and receipt["database_identity"] is None:
                volume_identity(selected, receipt["nonce"])
                return False
    return existing


def snapshot(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Embedding identity requires an owned regular dotenv file")
        return (stream.read(), info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)


def stage(path, body, prefix):
    fd, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def persist_identity(path, original, saved, actual):
    # Explicit values, including empty ones, are choices: never overwrite them.
    for key, value in actual.items():
        if key in saved and saved[key] != value:
            raise ValueError(f"{key} conflicts with the validated identity; reconcile it explicitly")
    missing = {key: value for key, value in actual.items() if key not in saved}
    if not missing:
        return
    if any(not re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value) for value in missing.values()):
        raise ValueError("Embedding identity cannot be represented literally; persist it explicitly")
    body = original[0] if original else b""
    rendered = body + (b"\n" if body and not body.endswith(b"\n") else b"")
    rendered += "".join(f"{key}={value}\n" for key, value in missing.items()).encode()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        if snapshot(path) != original:
            raise ValueError("Dotenv changed during embedding validation; retry")
        temporary = stage(path, rendered, ".embedding-identity-tmp-")
        if original is not None:
            # mkstemp creates a unique private backup even if the original was public.
            stage(path, body, path.name + ".embedding-identity.bak-")
            temporary.chmod(stat.S_IMODE(original[3]))
        if snapshot(path) != original:
            raise ValueError("Dotenv changed during embedding validation; retry")
        if original is None:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        os.fsync(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(directory)


def main():
    config = json.load(sys.stdin)
    path = Path(sys.argv[1])
    if sys.argv[2] == "--check-database":
        database_identity(config, json.loads(sys.argv[3]))
        return
    if sys.argv[2] == "--database-plan":
        identity = json.loads(sys.argv[3])
        database_identity(config, identity)
        print(json.dumps(database_plan(config, identity)))
        return
    if sys.argv[2] == "--adopt-recreated":
        print(json.dumps(adopt_recreated(config, json.loads(sys.argv[3])), sort_keys=True))
        return
    install = sys.argv[2] == "--install"
    if install and not existing_instance(config, path, "--fresh-profile" in sys.argv[3:]):
        print("{}")
        return
    database = database_identity(config)
    original = snapshot(path)
    saved = load_instance_env(path) if original is not None else {}
    dimension = int(db_query(config, "SELECT public.embedding_dimension()")) if install else int(sys.argv[2])
    actual = validate(config, {**saved, **os.environ}, dimension, install=install,
                      observe_revision="EMBEDDING_REVISION" not in saved)
    # Also check persisted values independently of shell overrides.
    for key, value in actual.items():
        if key in saved and saved[key] != value:
            raise ValueError(f"{key} conflicts with the validated identity; reconcile it explicitly")
    if "--persist" in sys.argv[3:]:
        persist_identity(path, original, saved, actual)
    print(json.dumps({**actual, "_database": database}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, IndexError, OSError, subprocess.CalledProcessError):
        # Avoid exposing credential-bearing configuration or subprocess output.
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "Could not validate the existing runtime; start it with its prior configuration and retry"
        print(f"Runtime preflight: {message}", file=sys.stderr)
        sys.exit(1)
