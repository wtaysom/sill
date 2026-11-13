"""Shared pytest fixtures and path setup for the sill-memory backend tests.

The backend exposes top-level modules (see ``py-modules`` in pyproject.toml)
rather than a package, so make sure the backend root is importable whether
tests are run from the repo root or from ``backend/``.
"""

import io
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture
def recall_hook(monkeypatch, capsys, tmp_path):
    """Run the actual prompt hook with synthetic Docker and archive responses."""
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
            assert args[0] == "docker", args
            query = args[-1]
            if "INSERT INTO session_activity" in query:
                spend("clock", args, timeout)
                return subprocess.CompletedProcess(args, 0, "|||-1|||1\n", "")
            if query.startswith("UPDATE memories "):
                spend("access", args, timeout)
                return subprocess.CompletedProcess(args, 0, "", "")
            calls["store"].append(query)
            spend("store", args, timeout)
            if isinstance(store_response, Exception):
                raise store_response
            if store_response is not None:
                return store_response
            return subprocess.CompletedProcess(args, 0, "", "")

        with monkeypatch.context() as patch:
            for name in tuple(os.environ):
                if name.startswith(("SILL_", "CLAUDE_")):
                    patch.delenv(name)
            patch.setenv("SILL_LOG_DIR", str(tmp_path))
            patch.setenv("SILL_EPISODIC_MEMORY_PATH", str(episodic_cli))
            for name, value in (settings or {}).items():
                patch.setenv(name, value)
            patch.setattr(subprocess, "run", fake_run)
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
