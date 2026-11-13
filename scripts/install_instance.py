"""Reconcile the host connection with the managed Compose database.

Run with the installed backend's Python. Accept rendered Compose settings
or Docker inspection data on stdin. Only instance selection prints an
env-file path. Connection values stay private.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from instance_config import InstanceConfigError, instance_dsn, load_instance_env, validate_dsn


def _dotenv_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _local_host(host: str, bind: str) -> bool:
    if bind == "127.0.0.1":
        return host in {"localhost", bind}
    if bind == "0.0.0.0":
        return host in {"localhost", "127.0.0.1"}
    if bind in {"::", "::1"}:
        return host in {"localhost", "::1"}
    return host == bind


def _persist_error(keys: list[str], path: Path) -> InstanceConfigError:
    return InstanceConfigError(
        "Conflicting settings: " + ", ".join(keys) + ". Persist the intended values in "
        + str(path) + " and clear conflicting shell overrides before registering a client."
    )


def select(path: Path, mode: str, config: dict) -> Path:
    exists = path.exists() or path.is_symlink()
    saved = load_instance_env(path) if exists else {}
    db = config["services"]["db"]
    if mode == "persist":
        identity = {
            "COMPOSE_PROJECT_NAME": (config["name"], path.parent.name),
            "SILL_DB_CONTAINER": (db["container_name"], "sill_db"),
        }
        changes = [
            key for key, (actual, default) in identity.items()
            if actual != (saved.get(key) or default)
        ]
        if changes:
            raise _persist_error(changes, path)
    port, = [p for p in db["ports"] if p["target"] == 5432 and p.get("protocol", "tcp") == "tcp"]
    bind = port.get("host_ip", "0.0.0.0")
    effective = {
        "POSTGRES_HOST": os.environ.get("POSTGRES_HOST", saved.get("POSTGRES_HOST", "localhost")),
        "POSTGRES_PORT": str(port["published"]),
        **{key: db["environment"][key] for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")},
    }
    # Compose controls the published address and server-side role/database;
    # an MCP/seed override must still target that instance.
    if not _local_host(effective["POSTGRES_HOST"], bind):
        raise InstanceConfigError("POSTGRES_HOST must target the local address published by SILL_BIND_ADDR")
    chosen_dsn = os.environ.get("SILL_DB_DSN", saved.get("SILL_DB_DSN"))
    if chosen_dsn is not None:
        parsed = urlsplit(validate_dsn(chosen_dsn))
        try:
            host, port = parsed.hostname or "", parsed.port or 5432
        except ValueError:
            host, port = "", 0
        if (
            not _local_host(host, bind)
            or port != int(effective["POSTGRES_PORT"])
            or unquote(parsed.username or "") != effective["POSTGRES_USER"]
            or unquote(parsed.password or "") != effective["POSTGRES_PASSWORD"]
            or unquote(parsed.path.removeprefix("/")) != effective["POSTGRES_DB"]
            or parsed.query
        ):
            raise InstanceConfigError(
                "SILL_DB_DSN conflicts with the local Compose database. Persist matching settings in "
                + str(path) + " and clear conflicting shell overrides, or remove the DSN to use POSTGRES_*."
            )
        effective["SILL_DB_DSN"] = chosen_dsn

    # Compare resolved defaults as well as explicit keys. A registry receives
    # no caller shell environment; check before writing it or starting Docker.
    if instance_dsn(saved) == instance_dsn(effective):
        if exists or mode in {"install", "persist"}:
            return path
    elif mode == "persist":
        defaults = {
            "POSTGRES_HOST": "localhost", "POSTGRES_PORT": "5432", "POSTGRES_USER": "sill",
            "POSTGRES_DB": "sill", "POSTGRES_PASSWORD": "sill_password",
        }
        conflicts = [key for key, value in effective.items() if saved.get(key, defaults.get(key)) != value]
        if not conflicts:
            conflicts = ["SILL_DB_DSN"]
        raise _persist_error(conflicts, path)

    # Legacy exported overrides still work for a shell-only invocation. Put
    # the effective settings in a private short-lived file, never argv.
    fd, name = tempfile.mkstemp(prefix="sill-instance-", suffix=".env")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for key, value in effective.items():
                stream.write(f"{key}={_dotenv_literal(value)}\n")
        load_instance_env(Path(name))
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def check_running_port(path: Path, bindings: list[dict]) -> None:
    settings = load_instance_env(path)
    url = urlsplit(instance_dsn(settings))
    expected_port = url.port or 5432
    expected_host = url.hostname or ""
    if not any(
        int(item["HostPort"]) == expected_port and _local_host(expected_host, item["HostIp"])
        for item in (bindings or [])
    ):
        raise InstanceConfigError(
            "POSTGRES_PORT or POSTGRES_HOST differs from the running Compose database. "
            "Use the running instance's configuration or recreate it before verifying."
        )


async def _host_identity(dsn: str) -> list[str] | None:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=5, command_timeout=5)
    try:
        row = await conn.fetchrow(
            "SELECT system_identifier::text, current_database() FROM pg_catalog.pg_control_system()"
        )
        return list(row) if row is not None else None
    finally:
        await conn.close()


def check_running_identity(path: Path, expected: list[str]) -> None:
    import asyncio

    dsn = instance_dsn(load_instance_env(path))
    # Match the explicit MCP launcher's environment and asyncpg resolution.
    for key in tuple(os.environ):
        if key.startswith("PG"):
            del os.environ[key]
    try:
        actual = asyncio.run(_host_identity(dsn))
    except Exception:
        raise InstanceConfigError(
            "Could not confirm the host database identity. Check POSTGRES_HOST, POSTGRES_PORT, "
            "POSTGRES_DB and POSTGRES_USER for the selected Compose instance."
        ) from None
    if actual != expected:
        raise InstanceConfigError(
            "The host connection reached a different database than the selected Compose instance. "
            "Check POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB and SILL_DB_DSN."
        )


def main() -> int:
    try:
        path, mode = Path(sys.argv[1]), sys.argv[2]
        config = json.load(sys.stdin)
        if mode == "check-port":
            check_running_port(path, config)
            return 0
        if mode == "check-identity":
            check_running_identity(path, config)
            return 0
        resolved = select(path, mode, config)
    except InstanceConfigError as exc:
        print(f"Sill instance: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError, TypeError):
        print("Sill instance: could not read the selected Compose database configuration.", file=sys.stderr)
        return 1
    print(resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
