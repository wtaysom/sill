"""Resolve supplied runtime identity; never infer it from a neighbouring file.

The worker may export one SILL_SESSION_ID for a recorded execution attempt.
Interactive hosts supply CODEX_THREAD_ID or a Claude session environment value;
hook payloads are the fallback where the host has no exported identifier.
An attempt key is a correlation scope, not a claim about native host identity.
"""
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone


def _identifier(value):
    return value.strip() or None if isinstance(value, str) else None


def session_context(payload=None, *, env=None):
    payload = payload or {}
    environ = os.environ if env is None else env
    hook_id = _identifier(payload.get("session_id"))
    native_ids = [(name, value) for name in
                  ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
                  if (value := _identifier(environ.get(name)))]
    host_id = host_source = None
    native_values = {value for _, value in native_ids}
    if hook_id and native_values and hook_id not in native_values:
        # A sole environment value can also belong to an inherited parent.
        # Contradictory hook evidence establishes neither as this native host.
        host_source = "conflicting_host_evidence"
    elif len(native_values) > 1:
        # A nested CLI can inherit its parent's identifier. Only a matching
        # hook payload disambiguates those native environments safely.
        matches = [(name, value) for name, value in native_ids if value == hook_id]
        if matches:
            host_source, host_id = matches[0]
        else:
            host_source = "ambiguous_host_environment"
    elif native_ids:
        host_source, host_id = native_ids[0]
    elif hook_id:
        host_id, host_source = hook_id, "hook_payload"
    sid = _identifier(environ.get("SILL_SESSION_ID"))
    if sid:
        source = _identifier(environ.get("SILL_SESSION_ID_SOURCE")) or "SILL_SESSION_ID"
        scope = "attempt" if source == "worker_attempt" else "explicit"
    else:
        sid, source = host_id, host_source or "missing"
        scope = "host" if sid else None
    return {
        "session_id": sid,
        "session_id_source": source,
        "session_scope": scope,
        "session_joinable": sid is not None,
        "host_session_id": host_id,
        "host_session_id_source": host_source,
        "hook_session_id": hook_id,
    }


def _process_ancestors(limit: int = 8) -> list[str]:
    """Command names up the parent chain, this process first, until pid 1 or
    `limit`. [] on any error: an unreadable chain is no evidence."""
    names, pid = [], os.getpid()
    try:
        while pid > 1 and len(names) < limit:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2, check=True).stdout
            ppid, comm = out.split(None, 1)
            names.append(os.path.basename(comm.strip()))
            pid = int(ppid)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return names


def detect_harness(env: Mapping[str, str], *,
                   ancestors: Callable[[], list[str]] = _process_ancestors,
                   tty: bool | None = None) -> str:
    """Name the harness this process runs under from positive evidence only,
    first match wins. A blank value is no evidence, as in session_context; a
    failing probe is no evidence either, never an error."""
    def supplied(name):
        return _identifier(env.get(name)) is not None
    if supplied("CLAUDECODE") or supplied("CLAUDE_CODE_SESSION_ID"):
        return "claude-code"
    if (any(key.startswith("CODEX_") and _identifier(value) for key, value in env.items())
            or env.get("SILL_BEAT_BACKEND") == "codex"):
        return "codex-cli"
    try:
        if "codex" in ancestors():
            return "codex-cli"
    except Exception:
        pass
    if env.get("SILL_BEAT_BACKEND") == "opencode":
        return "opencode"
    if tty is None:
        try:
            tty = sys.stdin.isatty()
        except (AttributeError, ValueError):
            tty = False
    return "shell" if tty else "unknown"


def _git_top(cwd: str | None = None) -> str | None:
    """The git top level of `cwd` (default: the process's), or None."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                             capture_output=True, text=True, timeout=3, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip() or None


def derive_transcript(session: str | None, env: Mapping[str, str], *,
                      exists: Callable[[str], bool] = os.path.exists,
                      git_top: Callable[[], str | None] = _git_top) -> str | None:
    """The session's transcript file, or None. A path the hook supplied is
    taken as supplied; one derived from the project directory is returned only
    if it exists on disk."""
    supplied = _identifier(env.get("SILL_HOOK_TRANSCRIPT_PATH"))
    if supplied:
        return supplied
    if not session:
        return None
    config_dir = _identifier(env.get("CLAUDE_CONFIG_DIR"))
    if not config_dir:
        return None
    projects = os.path.join(config_dir, "projects")

    def candidate(project):
        return os.path.join(projects, re.sub(r"[^A-Za-z0-9]", "-", project), f"{session}.jsonl")
    project = _identifier(env.get("CLAUDE_PROJECT_DIR"))
    if project and exists(candidate(project)):
        return candidate(project)
    top = git_top()
    if top and exists(candidate(top)):
        return candidate(top)
    return None


def _safely(probe, *args, **kwargs):
    try:
        return probe(*args, **kwargs)
    except Exception:
        return None


def mint_context(via: str, *, payload: dict | None = None, when: str | None = None,
                 env: Mapping[str, str] | None = None, cwd: str | None = None) -> dict:
    """The `minted_in` stamp for a mint: where, as whom and when it happened.

    `via` names the mint path; `when` is the stamp's clock, UTC as
    %Y-%m-%dT%H:%M:%SZ, and later declarations copy it as a string. Never
    raises: a probe that fails yields None for its field. The model fields are
    None here."""
    env = os.environ if env is None else env
    session = _safely(session_context, payload or {"session_id": env.get("SILL_HOOK_SESSION_ID")}, env=env) or {}
    scope, source = session.get("session_scope"), session.get("session_id_source")
    if scope == "attempt":
        session_source = "worker_attempt"
    elif scope == "host":
        session_source = "hook_payload" if source == "hook_payload" else "env"
    else:
        session_source = None
    cwd = cwd or env.get("SILL_HOOK_CWD") or _safely(os.getcwd)
    return {
        "harness": _safely(detect_harness, env),
        "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT") or None,
        "model": None,
        "model_source": None,
        "model_source_detail": None,
        "as": env.get("SILL_VOICE") or None,
        "session": session.get("session_id"),
        "session_source": session_source,
        "attempt": env.get("SILL_ATTEMPT_ID") or None,
        "host_session_id": session.get("host_session_id"),
        "host_session_id_source": session.get("host_session_id_source"),
        # The transcript file is named by the host's session id; the stamp's own
        # `session` is the attempt key for a beat.
        "transcript": _safely(derive_transcript, session.get("host_session_id"), env,
                              git_top=lambda: _git_top(cwd)),
        "project": env.get("CLAUDE_PROJECT_DIR") or None,
        "cwd": cwd,
        "via": via,
        "when": when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
