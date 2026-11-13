"""Read a specific installation's dotenv file without evaluating it."""

import ipaddress
import os
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from dotenv.parser import parse_stream


_DEFAULTS = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "sill",
    "POSTGRES_USER": "sill",
    "POSTGRES_PASSWORD": "sill_password",
}


class InstanceConfigError(ValueError):
    """A redacted error about the explicitly selected instance."""


def load_instance_env(path: Path) -> dict[str, str]:
    """Return the file's literal settings, checking DB settings in place."""
    if not path.is_absolute():
        raise InstanceConfigError(f"{path}: --env-file requires an absolute path")

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise InstanceConfigError(f"{path}: instance env file must be a regular file")
            settings: dict[str, str] = {}
            for binding in parse_stream(stream):
                key, value = binding.key, binding.value
                if (
                    binding.error
                    or key is not None and (
                        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                        or value is None
                        or "\0" in value
                    )
                ):
                    raise InstanceConfigError(
                        f"{path}: malformed dotenv setting at line {binding.original.line}"
                    )
                if key is None:
                    continue
                if key in settings:
                    raise InstanceConfigError(
                        f"{path}: duplicate dotenv setting at line {binding.original.line}"
                    )
                settings[key] = value
    except (OSError, UnicodeError) as exc:
        raise InstanceConfigError(f"{path}: could not read the instance env file") from exc

    try:
        _postgres_dsn(settings)
    except InstanceConfigError as exc:
        raise InstanceConfigError(f"{path}: {exc}") from None
    if "SILL_DB_DSN" in settings:
        try:
            validate_dsn(settings["SILL_DB_DSN"])
        except InstanceConfigError as exc:
            raise InstanceConfigError(f"{path}: SILL_DB_DSN: {exc}") from None
    return settings


def _valid_port(port: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,5}", port)) and 1 <= int(port) <= 65535


def _validate_targets(target: str, query_ports: str) -> None:
    for endpoint in target.split(","):
        if endpoint.startswith("/"):
            host, separator, port = endpoint, "", ""
        elif endpoint.startswith("["):
            host, end, suffix = endpoint[1:].partition("]")
            if not end or (suffix and not suffix.startswith(":")):
                raise ValueError("invalid IPv6 target")
            ipaddress.IPv6Address(unquote(host))
            separator, port = suffix[:1], suffix[1:]
        else:
            host, separator, port = endpoint.partition(":")
        host = unquote(host)
        if not host.strip() or any(ord(char) < 32 for char in host):
            raise ValueError("missing host or socket")
        if separator and not _valid_port(unquote(port)):
            raise ValueError("invalid port")
    if query_ports and not all(_valid_port(port) for port in query_ports.split(",")):
        raise ValueError("invalid port list")


def validate_dsn(dsn: str) -> str:
    """Check the URI form without surfacing any credential-bearing text."""
    try:
        parsed = urlsplit(dsn)
        query = parse_qs(parsed.query, strict_parsing=True) if parsed.query else {}
        authority = parsed.netloc.split("@", 1)[-1]
        if (
            dsn.lower().startswith(("postgresql://", "postgres://"))
            and "#" not in dsn
            and parsed.netloc.count("@") <= 1
            and not re.search(r"%(?![0-9a-fA-F]{2})", dsn)
            and not any(char.isspace() or ord(char) < 32 for char in dsn)
        ):
            target = authority or query.get("host", [""])[-1]
            _validate_targets(target, query.get("port", [""])[-1])
            return dsn
    except ValueError:
        pass
    raise InstanceConfigError(
        "expected a PostgreSQL URI with an explicit host or socket, valid ports, "
        "and URL-encoded credentials"
    )


def _postgres_dsn(settings: dict[str, str]) -> str:
    fields = _DEFAULTS | settings
    for key in _DEFAULTS:
        value = fields[key]
        if not value or (key != "POSTGRES_PASSWORD" and not value.strip()):
            raise InstanceConfigError(f"{key} must be nonempty")

    port = fields["POSTGRES_PORT"]
    if not _valid_port(port):
        raise InstanceConfigError("POSTGRES_PORT must be an integer from 1 to 65535")

    host = fields["POSTGRES_HOST"]
    if host.startswith("/"):
        # Asyncpg accepts a percent-encoded Unix socket directory in the URI.
        host = quote(host, safe="")
    elif ":" in host:
        address = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        try:
            ipaddress.IPv6Address(address)
        except ValueError:
            raise InstanceConfigError("POSTGRES_HOST must be a hostname or IP address") from None
        host = f"[{quote(address, safe=':')}]"
    elif not re.fullmatch(r"[\w.-]+", host):
        raise InstanceConfigError("POSTGRES_HOST must be a hostname or IP address")

    user = quote(fields["POSTGRES_USER"], safe="")
    password = quote(fields["POSTGRES_PASSWORD"], safe="")
    database = quote(fields["POSTGRES_DB"], safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def instance_dsn(settings: dict[str, str]) -> str:
    """Resolve the DSN from validated settings or the built-in defaults."""
    return settings.get("SILL_DB_DSN") or _postgres_dsn(settings)
