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


def _validate_targets(target: str, query_ports: list[str], *, uri_authority: bool) -> None:
    endpoints = target.split(",")
    if query_ports:
        if len(query_ports) not in {1, len(endpoints)}:
            raise InstanceConfigError("DSN must specify one query port or one port per host")
        if not all(_valid_port(port) for port in query_ports):
            raise InstanceConfigError("DSN contains an invalid query port")

    for endpoint in endpoints:
        if endpoint.startswith("/"):
            host, separator, port = endpoint, "", ""
        elif endpoint.startswith("["):
            host, end, suffix = endpoint[1:].partition("]")
            if not end or (suffix and not suffix.startswith(":")):
                raise ValueError("invalid IPv6 target")
            ipaddress.IPv6Address(unquote(host) if uri_authority else host)
            separator, port = suffix[:1], suffix[1:]
        else:
            host, separator, port = endpoint.partition(":")
        if uri_authority:
            host = unquote(host)
        if not host.strip() or any(ord(char) < 32 for char in host):
            raise ValueError("missing host or socket")
        if separator:
            if query_ports:
                raise InstanceConfigError("DSN must specify ports in the query host list or in port=")
            # Asyncpg only recognizes literal digits after a bracketed host.
            if not _valid_port(port):
                raise InstanceConfigError("DSN ports must be literal digits from 1 to 65535")


def validate_dsn(dsn: str) -> str:
    """Check the URI form without surfacing any credential-bearing text."""
    try:
        if (
            not dsn.lower().startswith(("postgresql://", "postgres://"))
            or "#" in dsn
            or re.search(r"%(?![0-9a-fA-F]{2})", dsn)
            or any(char.isspace() or ord(char) < 32 for char in dsn)
        ):
            raise ValueError("malformed URI")
        parsed = urlsplit(dsn)
        if parsed.netloc.count("@") > 1:
            raise ValueError("malformed authority")
        query = parse_qs(parsed.query, strict_parsing=True) if parsed.query else {}
        authority = parsed.netloc.split("@", 1)[-1]
        query_hosts = query.get("host", [])
        query_ports = query.get("port", [])
        if len(query_hosts) > 1 or len(query_ports) > 1:
            raise InstanceConfigError("DSN must specify host= and port= at most once each")
        # Asyncpg ignores query host and port when the URI authority contains
        # a host. Reject mixed forms before a setting can be silently ignored.
        if authority and (query_hosts or query_ports):
            raise InstanceConfigError("DSN must specify the host and port in the authority or the query")
        target = authority or next(iter(query_hosts), "")
        ports = query_ports[0].split(",") if query_ports else []
        _validate_targets(target, ports, uri_authority=bool(authority))
        return dsn
    except InstanceConfigError:
        raise
    except ValueError:
        raise InstanceConfigError(
            "expected a PostgreSQL URI with an explicit host or socket and URL-encoded credentials"
        ) from None


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
