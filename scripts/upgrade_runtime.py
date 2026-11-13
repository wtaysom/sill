"""Read-only validation of the installed Compose runtime before upgrade."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from instance_config import load_instance_env


def inspect(kind, name):
    return json.loads(subprocess.check_output(["docker", kind, "inspect", name], text=True))[0]


def validate(config, settings, dimension):
    project = config["name"]
    services = config["services"]
    db = inspect("container", services["db"]["container_name"])
    for service in ("db", "embeddings", "maintenance_worker"):
        obj = db if service == "db" else inspect("container", services[service]["container_name"])
        labels = obj["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.service") != service:
            raise ValueError(f"{service} does not belong to the requested Compose project")
        if service == "db" and not obj["State"]["Running"]:
            raise ValueError("Database is stopped; start the installed instance with its existing configuration before upgrading")
        if service == "embeddings":
            embedding = obj
    volumes = [v for v in services["db"]["volumes"] if v["target"] == "/var/lib/postgresql/data"]
    if len(volumes) != 1 or volumes[0]["type"] != "volume":
        raise ValueError("Unsupported database storage; expected the installed named Compose volume")
    source = volumes[0]["source"]
    expected = config["volumes"][source]["name"]
    mounts = [m for m in db["Mounts"] if m["Destination"] == "/var/lib/postgresql/data"]
    if len(mounts) != 1 or mounts[0].get("Name") != expected:
        raise ValueError("Requested Compose volume differs from the existing database volume")
    volume = inspect("volume", expected)
    labels = volume.get("Labels") or {}
    if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.volume") != source:
        raise ValueError("Database volume has unexpected Compose ownership")
    command = embedding["Config"]["Cmd"]
    model = command[command.index("--model-id") + 1]
    env = dict(x.split("=", 1) for x in embedding["Config"]["Env"] if "=" in x)
    revision = env.get("REVISION")
    if not revision:
        # v0.1/v0.2 did not configure REVISION. Pin the model actually loaded
        # by that service, never a new default or a guessed "main" snapshot.
        try:
            info = json.loads(subprocess.check_output([
                "docker", "exec", services["embeddings"]["container_name"],
                "curl", "-fsS", "--max-time", "10", "http://localhost:80/info",
            ], text=True))
            revision = info.get("model_sha")
            if info.get("model_id") != model or not isinstance(revision, str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
                raise ValueError()
        except (ValueError, subprocess.CalledProcessError):
            raise ValueError("Cannot prove the installed embedding revision from TEI /info; verify and persist EMBEDDING_REVISION before upgrading") from None
    actual = {"EMBEDDING_MODEL_ID": model, "EMBEDDING_REVISION": revision, "EMBEDDING_DIMENSION": str(dimension)}
    if env.get("EMBEDDING_DIMENSION", str(dimension)) != str(dimension):
        raise ValueError("Running embeddings dimension differs from stored vectors")
    for key, value in actual.items():
        if settings.get(key) and settings[key] != value:
            raise ValueError(f"{key} would change the installed embedding space; routine upgrades do not activate profiles")
    return actual


def main():
    config = json.load(sys.stdin)
    path = Path(sys.argv[1])
    saved = load_instance_env(path) if path.exists() else {}
    print(json.dumps(validate(config, {**saved, **os.environ}, int(sys.argv[2]))))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, IndexError, subprocess.CalledProcessError) as exc:
        print(f"Runtime preflight: {exc}", file=sys.stderr)
        sys.exit(1)
