"""Shared pytest fixtures and path setup for the sill-memory backend tests.

The backend exposes top-level modules (see ``py-modules`` in pyproject.toml)
rather than a package, so make sure the backend root is importable whether
tests are run from the repo root or from ``backend/``.
"""

import ast
import io
import json
import os
import runpy
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT.parent / "plugin/hooks"))

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture
def recall_hook(monkeypatch, capsys, tmp_path):
    """Run the actual prompt hook against synthetic core and archive boundaries."""
    hook = BACKEND_ROOT.parent / "plugin" / "hooks" / "spontaneous-recall.py"
    episodic_cli = tmp_path / "episodic-memory"
    episodic_cli.touch()

    def run(*, settings=None, prompt="Cedar meadow survey records",
            store_response=None, episodes="", delays=None):
        calls = {"store": [], "episodes": []}
        elapsed = 0.0

        def spend(stage, args, timeout):
            nonlocal elapsed
            delay = (delays or {}).get(stage, 0)
            elapsed += min(delay, timeout)
            if delay > timeout:
                raise subprocess.TimeoutExpired(args, timeout)

        def fake_run(args, *, timeout, **kwargs):
            if args[0] == "git":
                spend("project", args, timeout)
                return subprocess.CompletedProcess(args, 0, str(tmp_path), "")
            if args[0] == str(episodic_cli):
                assert args[1:4] == ["search", prompt, "--limit"]
                calls["episodes"].append(int(args[4]))
                spend("episodes", args, timeout)
                return subprocess.CompletedProcess(args, 0, episodes, "")
            raise AssertionError(f'Unexpected subprocess: {args}')

        from scripts import recall_core as core
        import _store

        def fused(text, previous='', **kwargs):
            calls['store'].append(kwargs)
            try:
                spend('store', [], max(0, kwargs['deadline'] - time.monotonic()))
                if isinstance(store_response, Exception) or (store_response is not None and store_response.returncode):
                    raise core.RecallUnavailable('unavailable')
                return core.parse_memory_rows(store_response.stdout, kwargs['limit']) if store_response else []
            except (OSError, subprocess.SubprocessError):
                raise core.RecallUnavailable('unavailable') from None

        def clock(*args, **kwargs):
            spend('clock', [], kwargs['timeout'])
            return []

        def exposure(ids, **kwargs):
            if ids:
                try:
                    spend('access', [], max(0, min(10, kwargs['deadline']-time.monotonic())))
                except subprocess.SubprocessError:
                    raise core.RecallUnavailable('unavailable') from None

        with monkeypatch.context() as patch:
            for name in tuple(os.environ):
                if name.startswith(("SILL_", "CLAUDE_")):
                    patch.delenv(name)
            patch.setenv("SILL_LOG_DIR", str(tmp_path))
            patch.setenv("SILL_EPISODIC_MEMORY_PATH", str(episodic_cli))
            for name, value in (settings or {}).items():
                patch.setenv(name, value)
            patch.setattr(subprocess, "run", fake_run)
            patch.setattr(core, 'fused_recall', fused)
            patch.setattr(core, 'full_contents', lambda *a, **kw: {})
            patch.setattr(core, 'record_exposure', exposure)
            patch.setattr(_store, 'query', clock)
            if delays is not None:
                patch.setattr(time, "monotonic", lambda: elapsed)
            patch.setattr(sys, "stdin", io.StringIO(json.dumps({
                "prompt": prompt, "session_id": "test-session", "cwd": str(tmp_path),
            })))
            with pytest.raises(SystemExit) as exited:
                runpy.run_path(str(hook), run_name="__main__")

        captured = capsys.readouterr()
        assert exited.value.code == 0
        assert captured.err == ""
        if delays is not None:
            calls["elapsed"] = elapsed
        return json.loads(captured.out), calls

    return run


@pytest.fixture
def prompt_hook(monkeypatch, tmp_path):
    """The prompt hook's definitions and its entry code, run separately in-process.

    `head()` returns a module holding everything spontaneous-recall.py defines
    before it reads its payload, so a test can call one function or replace one.
    `run(module, payload)` then executes the rest of the file against that module
    and returns what the hook printed (None if it printed nothing).

    Nothing live is behind it. Any subprocess (the database, git, a conversation
    search) raises; the hook's log, its session journal and the pattern sidecar
    live under `log_dir` in tmp_path; and the settings and session identity of
    whoever is running the suite are taken out of the environment.
    """
    hook = BACKEND_ROOT.parent / "plugin" / "hooks" / "spontaneous-recall.py"
    tree = ast.parse(hook.read_text(encoding="utf-8"))
    split = next(i for i, node in enumerate(tree.body) if "json.load(sys.stdin)" in ast.unparse(node))
    head_code, entry_code = (compile(ast.Module(body=body, type_ignores=[]), str(hook), "exec")
                             for body in (tree.body[:split], tree.body[split:]))

    def refuse(*args, **kwargs):
        raise RuntimeError("this test starts no subprocess: no database, no git, no search")

    from scripts import recall_core as core

    for name in tuple(os.environ):
        if name.startswith(("SILL_", "CLAUDE_")):
            monkeypatch.delenv(name)
    log_dir = tmp_path / "sill-log"
    log_dir.mkdir()
    monkeypatch.setenv("SILL_LOG_DIR", str(log_dir))
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(core, "full_contents", lambda ids, **kw: {})
    monkeypatch.setattr(core, "record_exposure", lambda ids, **kw: None)

    def head():
        module = types.ModuleType("spontaneous_recall_under_test")
        module.__file__ = str(hook)
        exec(head_code, module.__dict__)
        return module

    def run(module, payload):
        printed = io.StringIO()
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
            patch.setattr(sys, "stdout", printed)
            try:
                exec(entry_code, module.__dict__)
            except SystemExit as stop:
                assert stop.code in (0, None)
        return json.loads(printed.getvalue()) if printed.getvalue().strip() else None

    return types.SimpleNamespace(head=head, run=run, log_dir=log_dir, path=hook)
