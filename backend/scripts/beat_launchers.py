"""Build headless host commands and extract evidence for a single beat."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, Mapping
from uuid import UUID


Launcher = Literal["claude", "codex"]
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]*")


@dataclass(frozen=True)
class LaunchResult:
    host_session_id: str | None
    effective_model: str | None
    model_source: str | None
    text: str
    diagnostics: str
    failed: bool


def command(
    launcher: Launcher, cli: str, model: str | None, attempt_id: str, root: Path, prompt: str,
) -> list[str]:
    model_arg = ["--model", model] if model is not None else []
    if launcher == "claude":
        return [cli, "--print", "--session-id", attempt_id, "--output-format", "json",
                *model_arg, "-p", prompt]
    if launcher == "codex":
        return [cli, "exec", "--json", "--color", "never", "--cd", str(root),
                "--skip-git-repo-check", *model_arg, "--", prompt]
    raise ValueError(f"invalid launcher {launcher!r}")


def _json_object(line: str) -> dict:
    try:
        data = json.loads(line)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _records(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                record = _json_object(line)
                if not record:
                    raise ValueError(f"invalid JSON record in {path}")
                yield record


def _session_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        canonical = str(UUID(value))
    except ValueError:
        return None
    return canonical if value.lower() == canonical else None


def _model_id(value: object) -> bool:
    return isinstance(value, str) and bool(_MODEL_ID.fullmatch(value))


def _host_dir(env: Mapping[str, str], key: str, default: str, root: Path) -> Path:
    configured = env.get(key)
    if configured:
        return root / configured
    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    return home / default


def _claude_models(root: Path, env: Mapping[str, str], attempt_id: str) -> set[str] | None:
    """Return native models, an empty set for invalid model data, or None for unverified identity."""
    projects = _host_dir(env, "CLAUDE_CONFIG_DIR", ".claude", root) / "projects"
    models = set()
    found_assistant = False
    invalid_model = False
    try:
        paths = list(projects.glob(f"*/{attempt_id}.jsonl"))
        if len(paths) != 1 or not paths[0].is_file():
            return None
        for row in _records(paths[0]):
            if row.get("type") != "assistant" or row.get("isSidechain"):
                continue
            message = row.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if model == "<synthetic>":
                continue
            native_id = _session_id(row.get("sessionId"))
            if "sessionId" in row and native_id != attempt_id:
                return None
            if _model_id(model):
                found_assistant = True
                models.add(model)
            else:
                found_assistant |= native_id == attempt_id
                invalid_model = True
    except (OSError, ValueError):
        return None
    if not found_assistant:
        return None
    return set() if invalid_model else models


def _claude_result(
    result: subprocess.CompletedProcess[str], attempt_id: str, root: Path, env: Mapping[str, str],
) -> LaunchResult:
    stdout = result.stdout or ""
    payload = _json_object(stdout)
    structured = payload.get("type") == "result" or (
        "type" not in payload and "result" in payload and "is_error" in payload
    )
    text = payload.get("result") if structured else stdout
    outcome = payload.get("is_error")
    failed = result.returncode != 0 or (structured and outcome is not False)
    diagnostics = result.stderr or ""
    if structured and not isinstance(outcome, bool):
        diagnostics = "\n".join(filter(None, (diagnostics, "Claude result did not report a boolean is_error.")))
    text = text if isinstance(text, str) else ""

    reported = _session_id(payload.get("session_id")) if structured else None
    if structured and payload.get("session_id") is not None and reported != attempt_id:
        return LaunchResult(None, None, None, text, diagnostics, failed)

    usage = payload.get("modelUsage", {}) if structured else {}
    valid_usage = isinstance(usage, dict) and all(
        _model_id(key) and isinstance(value, dict) for key, value in usage.items()
    )
    usage_models = set(usage) if valid_usage else set()
    if reported == attempt_id and len(usage_models) == 1:
        return LaunchResult(reported, next(iter(usage_models)), "claude_result_model_usage",
                            text, diagnostics, failed)

    native_models = _claude_models(root, env, attempt_id) if _session_id(attempt_id) == attempt_id else None
    host_session_id = attempt_id if native_models is not None else reported
    if valid_usage and native_models and len(native_models) == 1 and (not usage_models or native_models == usage_models):
        return LaunchResult(host_session_id, next(iter(native_models)), "claude_native_session_metadata",
                            text, diagnostics, failed)
    return LaunchResult(host_session_id, None, None, text, diagnostics, failed)


def _codex_model(root: Path, env: Mapping[str, str], thread_id: str) -> str | None:
    sessions = _host_dir(env, "CODEX_HOME", ".codex", root) / "sessions"
    try:
        paths = list(sessions.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
        if len(paths) != 1 or not paths[0].is_file():
            return None
        ids = set()
        models = set()
        for row in _records(paths[0]):
            payload = row.get("payload")
            if row.get("type") == "session_meta":
                ids.add(_session_id(payload.get("id")) if isinstance(payload, dict) else None)
            elif row.get("type") == "turn_context":
                model = payload.get("model") if isinstance(payload, dict) else None
                if not _model_id(model):
                    return None
                models.add(model)
        if ids == {thread_id} and len(models) == 1:
            return next(iter(models))
    except (OSError, ValueError):
        pass
    return None


def _codex_result(
    result: subprocess.CompletedProcess[str], root: Path, env: Mapping[str, str],
) -> LaunchResult:
    thread_ids = set()
    terminal_events = []
    diagnostics = []
    text = ""
    for line in (result.stdout or "").splitlines():
        event = _json_object(line)
        kind = event.get("type")
        if kind == "thread.started":
            thread_ids.add(_session_id(event.get("thread_id")))
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                text = item["text"]
        elif kind in ("error", "turn.failed", "turn.completed"):
            if kind != "error":
                terminal_events.append(kind)
            error = event.get("error") if kind == "turn.failed" else event
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                diagnostics.append(error["message"])

    thread_id = next(iter(thread_ids)) if len(thread_ids) == 1 else None
    failed = result.returncode != 0 or terminal_events != ["turn.completed"]
    if not terminal_events:
        diagnostics.append("Codex did not report a terminal turn event.")
    elif terminal_events != ["turn.completed"]:
        diagnostics.append("Codex did not report one successful turn completion.")
    model = _codex_model(root, env, thread_id) if thread_id and not failed else None
    return LaunchResult(thread_id, model, "codex_native_session_metadata" if model else None,
                        text, "\n".join(diagnostics), failed)


def parse_result(
    launcher: Launcher, result: subprocess.CompletedProcess[str], attempt_id: str,
    root: Path, env: Mapping[str, str],
) -> LaunchResult:
    if launcher == "claude":
        return _claude_result(result, attempt_id, root, env)
    if launcher == "codex":
        return _codex_result(result, root, env)
    raise ValueError(f"invalid launcher {launcher!r}")
