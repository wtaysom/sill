"""Installed-hook entry point to the shared transport and instance selector."""
import json
import os
import sys
from pathlib import Path

# Source installs and packaged backend interpreters use the same modules.
_backend = Path(__file__).resolve().parents[2] / 'backend'
if _backend.is_dir():
    sys.path.insert(0, str(_backend))
from scripts import recall_core as core
from instance_config import instance_dsn, load_instance_env


def selected_dsn():
    selected = os.environ.get('SILL_INSTANCE_ENV_FILE')
    return instance_dsn(load_instance_env(Path(selected))) if selected is not None else None


def query(sql, *, timeout=5, deadline=None, json_output=False):
    """Return JSON rows for arbitrary hook SQL, without delimiter ambiguity."""
    # Existing hook statements are single SELECT/CTE queries. DML returning no
    # rows uses execute instead. Never route an invalid selector to legacy DB.
    return core._json_rows(core._run_sql(
        sql if json_output else 'SELECT row_to_json(hook_row) FROM (' + sql.strip().rstrip(';') + ') hook_row',
        timeout=timeout, deadline=deadline))


def execute(sql, *, timeout=5, deadline=None):
    return core._run_sql(sql, timeout=timeout, deadline=deadline)


def selected_legacy_rows(sql, *, unavailable_message, timeout=5):
    """Adapt selected SQL to the optional hooks' positional, empty-null rows.

    Column names need not be unique. Keep every position through the existing
    selected row-array transport instead of decoding object keys first.
    """
    try:
        if sql.lstrip().upper().startswith('UPDATE '):
            execute(sql, timeout=timeout)
            return []
        output = core._run_sql(sql, timeout=timeout, row_arrays=True)
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        if any(not isinstance(row, list) for row in rows):
            raise ValueError('invalid positional row shape')
        return [['' if value is None else str(value) for value in row] for row in rows]
    except Exception:
        print(unavailable_message, file=sys.stderr)
        return []


def selected_connection(connect):
    """Connect this single-threaded hook without libpq environment defaults.

    The selected URI may omit a username/database; inherited PGSERVICE or
    PGUSER must not silently supply coordinates from another installation.
    """
    dsn = selected_dsn()
    if dsn is None:
        return None
    inherited = {key:value for key,value in os.environ.items() if key.startswith('PG')}
    try:
        for key in inherited:
            del os.environ[key]
        return connect(dsn, connect_timeout=5, options='-c statement_timeout=5000')
    finally:
        os.environ.update(inherited)


def recall_min_similarity():
    """Shared configurable query-cosine floor, with the public .25 default."""
    try:
        value = float(os.environ.get('SILL_RECALL_MIN_SIMILARITY', '.25'))
    except ValueError:
        return .25
    return value if 0 <= value <= 1 else .25
