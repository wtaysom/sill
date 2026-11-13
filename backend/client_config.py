"""Register one local Sill instance while preserving the client registry."""

import fcntl
import os
import stat
import tempfile
from collections.abc import MutableMapping
from contextlib import contextmanager
from decimal import DecimalException
from pathlib import Path

import simplejson
import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import InlineTable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from instance_config import InstanceConfigError, load_instance_env



class ClientConfigError(ValueError):
    """An actionable error that does not reveal the registry contents."""


def client_config_path(client: str) -> Path:
    # HOME is the base chosen by the caller. Canonicalize it so platform
    # aliases such as macOS /var work, then reject links within the registry
    # path. A user-specified CODEX_HOME stays subject to that link check.
    home = Path.home().resolve()
    if client == "claude-code":
        return home / ".claude.json"
    if client == "claude-desktop":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if client == "codex":
        codex_home = os.environ.get("CODEX_HOME")
        if not codex_home:
            return home / ".codex" / "config.toml"
        directory = Path(codex_home).expanduser().absolute()
        return directory / "config.toml"
    raise ClientConfigError(f"Unknown client {client!r}")


def _unique_json_keys(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_config(body: str, codex: bool) -> tuple[MutableMapping, dict]:
    if codex:
        # The strict parser also catches any changes to the meaning of other
        # keys when tomlkit renders a document with mixed table styles. Keep
        # the literal spelling of floats so valid NaNs compare equal here.
        return tomlkit.parse(body), _toml_values(body)
    # Decimal preserves fractions and exponents beyond binary float precision.
    parsed = simplejson.loads(body, object_pairs_hook=_unique_json_keys,
                              use_decimal=True, allow_nan=False)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed, parsed


def _toml_values(body: str) -> dict:
    return tomllib.loads(body, parse_float=lambda value: ("float", value))


def _snapshot(path: Path) -> tuple[bytes, os.stat_result] | None:
    # NOFOLLOW protects the leaf if it changed after checking its parents.
    # NONBLOCK prevents a concurrently created FIFO from hanging the command.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ClientConfigError(f"{path}: expected a regular config file")
        return stream.read(), info


def _file_identity(snapshot: tuple[bytes, os.stat_result] | None) -> tuple | None:
    if snapshot is None:
        return None
    body, info = snapshot
    return (body, info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)


def _check_path(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ClientConfigError(f"{path}: config path contains a symlink at {component}")


@contextmanager
def _stage_bytes(path: Path, body: bytes):
    fd, filename = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    staged = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        yield staged
    finally:
        staged.unlink(missing_ok=True)


def _commit(path: Path, original: tuple[bytes, os.stat_result] | None, rendered: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_path(path)
    # Serialize cooperating registrations across the recheck and replacement
    # without leaving a lock file in the client configuration directory.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        if _file_identity(_snapshot(path)) != _file_identity(original):
            raise ClientConfigError(f"{path}: config changed during registration, retry")
        with _stage_bytes(path, rendered) as staged:
            if original is None:
                # Linking is an atomic create that refuses a concurrent file.
                os.link(staged, path)
                return

            body, info = original
            with _stage_bytes(path, body) as backup:
                # The temporary basename supplies a unique backup suffix.
                suffix = backup.name.rsplit(".tmp-", 1)[1]
                os.link(backup, path.with_name(f"{path.name}.bak-{suffix}"))
            staged.chmod(stat.S_IMODE(info.st_mode))
            _check_path(path)
            if _file_identity(_snapshot(path)) != _file_identity(original):
                raise ClientConfigError(f"{path}: config changed during registration, retry")
            os.replace(staged, path)
    finally:
        os.close(directory)


def _is_legacy(entry: dict, client: str) -> bool:
    if entry.get("command") != "sill-mcp":
        return False
    optional = {"args": [], "env": {}} if client != "codex" else {}
    if client == "claude-code":
        optional["type"] = "stdio"
    return all(key in optional and value == optional[key]
               for key, value in entry.items() if key != "command")


def _is_same_instance(entry: dict, desired: dict, env_file: Path) -> bool:
    if entry.keys() != desired.keys():
        return False
    command, args = entry["command"], entry["args"]
    if (
        not isinstance(command, str) or not Path(command).is_absolute()
        or not isinstance(args, list) or len(args) != 2 or args[0] != "--env-file"
        or not isinstance(args[1], str) or not Path(args[1]).is_absolute()
        or entry.get("type") != desired.get("type")
    ):
        return False
    try:
        return Path(args[1]).resolve() == env_file
    except (OSError, RuntimeError, ValueError):
        return False


def _render(path: Path, body: str, client: str, env_file: Path, command: Path,
            adopt_legacy: bool) -> str | None:
    codex = client == "codex"
    key = "mcp_servers" if codex else "mcpServers"
    try:
        config, parsed = _parse_config(body, codex)
        entries = parsed.setdefault(key, {})
        if not isinstance(entries, dict):
            raise ValueError("expected a mapping of server objects")
        if any(not isinstance(entry, dict) for entry in entries.values()):
            raise ValueError("expected each server to be an object")
    except (ValueError, DecimalException, TOMLKitError) as exc:
        format_name = "TOML" if codex else "JSON"
        raise ClientConfigError(f"{path}: malformed {format_name} or invalid {key} shape") from exc

    desired = {"command": str(command), "args": ["--env-file", str(env_file)]}
    if client == "claude-code":
        desired = {"type": "stdio", **desired}
    existing = entries.get("sill")
    if existing == desired:
        return None
    if existing is not None:
        if _is_legacy(existing, client):
            if not adopt_legacy:
                raise ClientConfigError(
                    f"{path}: the bare sill-mcp entry has ambiguous instance ownership. "
                    "Check which database it uses before retrying with --adopt-legacy."
                )
        elif not _is_same_instance(existing, desired, env_file):
            raise ClientConfigError(
                f"{path}: conflicting {key}.sill entry. Review it before registering this instance."
            )

    entries["sill"] = desired
    try:
        if not codex:
            rendered = simplejson.dumps(config, indent=2, use_decimal=True, allow_nan=False) + "\n"
            _, rendered_values = _parse_config(rendered, codex)
        elif existing is not None:
            # Update in place so comments and quoted or dotted keys survive.
            config[key]["sill"].update(desired)
            rendered = config.as_string()
        elif isinstance(config.get(key), InlineTable):
            entry = tomlkit.inline_table()
            entry.update(desired)
            config[key]["sill"] = entry
            rendered = config.as_string()
        else:
            # An appended table cannot pull later root keys into its scope,
            # which can happen when inserting into dotted keys in tomlkit.
            addition = tomlkit.dumps({key: {"sill": desired}})
            separator = "" if not body else "\n" if body.endswith("\n") else "\n\n"
            rendered = body + separator + addition

        if codex:
            rendered_values = _toml_values(rendered)
        if rendered_values != parsed:
            raise ValueError("the merge changed unrelated settings")
        return rendered
    except (ValueError, DecimalException, TOMLKitError) as exc:
        raise ClientConfigError(f"{path}: could not render a valid config while preserving existing settings") from exc


def register_client(client: str, env_file: Path, command: Path, *,
                    dry_run: bool = False, adopt_legacy: bool = False) -> str:
    path = client_config_path(client)
    try:
        load_instance_env(env_file)
        env_file = env_file.resolve(strict=True)
        if not command.is_absolute() or not command.is_file() or not os.access(command, os.X_OK):
            raise ClientConfigError(
                f"{path}: --mcp-command must name an absolute executable file ({command})"
            )
        _check_path(path)
        original = _snapshot(path)
        if original is not None:
            body = original[0].decode("utf-8")
        else:
            body = "" if client == "codex" else "{}"
        rendered = _render(path, body, client, env_file, command, adopt_legacy)
        if rendered is None:
            return "already registered"
        if dry_run:
            return "would register"
        _commit(path, original, rendered.encode("utf-8"))
        return "registered"
    except InstanceConfigError as exc:
        raise ClientConfigError(f"{path}: {exc}") from None
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise ClientConfigError(f"{path}: could not read or update the config ({type(exc).__name__})") from exc
