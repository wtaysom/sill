"""Ctrl-C stops the beat worker cleanly.

`docs/onboarding/03-first-beats.md` tells an operator to run the worker in
the foreground and stop it with Ctrl-C. The v0.2.0 clean-machine acceptance
rehearsal could not check that — it ran the worker detached, where SIGINT is
ignored by inheritance, and stopped it with SIGTERM — so
`docs/RELEASE-REHEARSAL.md` listed it unverified and guessed at the
behaviour. Run under a pty, the guess was half right: the worker printed an
eleven-frame traceback ending in `KeyboardInterrupt` and exited on the
signal, both mid-beat and while sleeping between beats. (The other half of
the guess — that a mid-beat Ctrl-C might hold the terminal until the 30
minute beat timeout — was wrong: it returned in 0.05 s, because
`subprocess.run()` kills the child on its way out.)

These tests use real worker processes and a `/bin/sh` stub for the agent
CLI. The Ctrl-C cases send SIGINT to the worker's foreground process group
as a terminal would. The agent CLI starts in its own session, so the worker
must terminate its group on interruption.
"""

import json
import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import beat_worker as bw

BACKEND_ROOT = Path(__file__).resolve().parents[1]
WORKER_PY = BACKEND_ROOT / "worker.py"


def _build_project(root: Path, *, cli_sleep: float, produce_output: bool) -> Path:
    """A one-voice beat project whose 'agent CLI' is a shell stub."""
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "probe.md").write_text("Standing prompt.\n")
    (root / "notes").mkdir(exist_ok=True)
    (root / "beats.json").write_text(json.dumps({
        "voices": [{
            "name": "probe",
            "prompt": "prompts/probe.md",
            "transcripts": "logs/probe",
            "output_glob": "notes/*.md",
            "kickoff": "Begin.",
        }],
    }))
    cli = root / "fake-cli"
    lines = ["#!/bin/sh"]
    if produce_output:
        lines.append(f'echo made-a-note > "{root}/notes/probe-$$.md"')
    lines.append(f"sleep {cli_sleep}")
    cli.write_text("\n".join(lines) + "\n")
    cli.chmod(0o755)
    return cli


def _run_until_interrupt(root: Path, cli: Path, *, settle: float) -> tuple[int, str]:
    """Start the worker on a pty in its own session, let it settle, then send
    SIGINT to its process group the way a terminal's ^C does. Returns
    (exit status, everything it printed)."""
    env = _beat_env(root, cli, timeout=300)
    env["PYTHONUNBUFFERED"] = "1"
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(WORKER_PY), "--mode", "beat"],
        stdin=slave, stdout=slave, stderr=slave,
        env=env, cwd=str(root), start_new_session=True,
    )
    os.close(slave)
    captured: list[str] = []

    def drain(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([master], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:      # pty closes when the child exits
                return
            if not chunk:
                return
            captured.append(chunk.decode("utf-8", "replace"))

    try:
        drain(settle)
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        deadline = time.time() + 15
        while proc.poll() is None and time.time() < deadline:
            drain(0.2)
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            pytest.fail(
                "the beat worker did not exit within 15s of Ctrl-C — output so "
                f"far:\n{''.join(captured)}"
            )
        drain(0.3)
        return proc.returncode, "".join(captured)
    finally:
        os.close(master)


@pytest.mark.skipif(os.name != "posix", reason="pty/process groups are POSIX")
def test_ctrl_c_mid_beat_stops_cleanly(tmp_path):
    """The hard case: a beat is in flight when the operator gives up on it."""
    cli = _build_project(tmp_path, cli_sleep=60, produce_output=False)
    status, output = _run_until_interrupt(tmp_path, cli, settle=3.0)

    assert "Traceback" not in output, (
        "Ctrl-C during a beat printed a Python traceback — the runbook's own "
        f"way of stopping a supervised run must not look like a crash:\n{output}"
    )
    assert "KeyboardInterrupt" not in output
    assert status == 0, f"expected a clean exit, got {status}:\n{output}"
    assert "Interrupted (Ctrl-C)" in output
    # Rotation must not advance past a beat that never finished. With one
    # voice the index is 0 either way, so check the stronger thing: an
    # interrupted beat writes no state file at all.
    assert not (tmp_path / "nested" / "state" / "rotation.json").exists(), (
        "an interrupted beat wrote rotation state — it did not finish, so it "
        "must not count as taken"
    )


@pytest.mark.skipif(os.name != "posix", reason="pty/process groups are POSIX")
def test_ctrl_c_between_beats_stops_cleanly(tmp_path):
    """The common case: the operator has read the first beat's output and is
    done. The worker is asleep on `time.sleep(INTERVAL)`."""
    cli = _build_project(tmp_path, cli_sleep=0, produce_output=True)
    status, output = _run_until_interrupt(tmp_path, cli, settle=4.0)

    assert "Sleeping" in output, f"the worker never reached its sleep:\n{output}"
    assert "Traceback" not in output, output
    assert status == 0, f"expected a clean exit, got {status}:\n{output}"
    assert "Interrupted (Ctrl-C)" in output


@pytest.mark.skipif(os.name != "posix", reason="pty/process groups are POSIX")
def test_ctrl_c_mid_beat_leaves_no_orphan_child(tmp_path):
    """The detached CLI must exit when the worker handles Ctrl-C."""
    cli = _build_project(tmp_path, cli_sleep=90, produce_output=False)
    marker = str(tmp_path / "fake-cli")
    _run_until_interrupt(tmp_path, cli, settle=3.0)

    time.sleep(0.5)
    survivors = subprocess.run(
        ["pgrep", "-f", marker], capture_output=True, text=True,
    )
    assert survivors.returncode != 0, (
        f"the beat's agent CLI survived Ctrl-C as an orphan: pids "
        f"{survivors.stdout.split()}"
    )


@pytest.fixture
def beat_project(tmp_path):
    cli = _build_project(tmp_path, cli_sleep=0, produce_output=True)
    # A second voice makes unintended rotation observable on failure.
    config = tmp_path / "beats.json"
    data = json.loads(config.read_text())
    data["voices"].append({**data["voices"][0], "name": "other"})
    config.write_text(json.dumps(data))
    return tmp_path, cli


def _beat_env(root, cli, *, timeout):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("SILL_BEAT_", "SILL_LOG_DIR"))}
    env.update({
        "HOME": str(root),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONPATH": str(BACKEND_ROOT),
        "SILL_PROJECT_ROOT": str(root),
        "SILL_BEAT_CLI": str(cli),
        "SILL_BEAT_INTERVAL_SECONDS": "120",
        "SILL_BEAT_TIMEOUT_SECONDS": str(timeout),
        "SILL_BEAT_STATE_PATH": str(root / "nested" / "state" / "rotation.json"),
        "SILL_BEAT_LOG_PATH": str(root / "worker.log"),
    })
    return env


def _worker_cmd(*, once=False, legacy=False):
    if legacy:
        return [sys.executable, "-m", "beat_worker"]
    args = [sys.executable, str(WORKER_PY), "--mode", "beat"]
    if once:
        args.append("--once")
    return args


@contextmanager
def _running_worker(root, env, *, legacy=False):
    with subprocess.Popen(
        _worker_cmd(legacy=legacy), cwd=root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    ) as proc:
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()


def _worker_result(root, env, *, once=False, legacy=False):
    return subprocess.run(
        _worker_cmd(once=once, legacy=legacy), cwd=root, env=env,
        capture_output=True, text=True, timeout=5,
    )


def _wait_for(predicate, *, what):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {what}")


def _worker_log_contains(root, message):
    log = root / "worker.log"
    return log.exists() and message in log.read_text()


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX")
def test_worker_lock_refuses_second_process_during_interval_sleep(beat_project):
    root, cli = beat_project
    env = _beat_env(root, cli, timeout=5)
    with _running_worker(root, env) as owner:
        _wait_for(lambda: _worker_log_contains(root, "Sleeping"), what="interval sleep")
        contender = _worker_result(root, env, legacy=True)
        assert contender.returncode == 75, contender.stderr
        assert len(list(root.glob("notes/*.md"))) == 1

        lock = Path(env["SILL_BEAT_STATE_PATH"] + ".lock")
        inode = lock.stat().st_ino
        owner.send_signal(signal.SIGTERM)
        assert owner.wait(timeout=5) == 0
        assert lock.stat().st_ino == inode

    next_owner = _worker_result(root, env, once=True)
    assert next_owner.returncode == 0, next_owner.stderr
    assert lock.stat().st_ino == inode
    assert len(list(root.glob("notes/*.md"))) == 2


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX")
def test_worker_lock_survives_parent_death_while_cli_owns_fd(beat_project):
    root, cli = beat_project
    env = _beat_env(root, cli, timeout=30)
    cli.write_text("""#!/bin/sh
echo "$$" >> cli-pids
i=0
while [ "$i" -lt 100 ]; do
    if [ -e release-cli ]; then
        echo made-a-note > "notes/probe-$$.md"
        exit 0
    fi
    i=$((i + 1))
    sleep 0.1
done
exit 1
""")
    pids = root / "cli-pids"
    release = root / "release-cli"

    try:
        with _running_worker(root, env, legacy=True) as owner:
            _wait_for(lambda: pids.exists() and pids.stat().st_size, what="agent CLI startup")
            cli_pid = int(pids.read_text().strip())
            owner.kill()
            assert owner.wait(timeout=5) == -signal.SIGKILL
            assert _pid_exists(cli_pid)
            contender = _worker_result(root, env, once=True)
            assert contender.returncode == 75, contender.stderr
            assert pids.read_text().splitlines() == [str(cli_pid)]
    finally:
        release.touch()

    def take_ownership():
        next_owner = _worker_result(root, env, once=True)
        if next_owner.returncode == 75:
            return False
        assert next_owner.returncode == 0, next_owner.stderr
        return True

    _wait_for(take_ownership, what="orphaned agent CLI to release the lock")
    assert len(pids.read_text().splitlines()) == 2


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_timeout_kills_grandchild_and_keeps_voice(beat_project):
    root, cli = beat_project
    env = _beat_env(root, cli, timeout=1)
    state = Path(env["SILL_BEAT_STATE_PATH"])
    state.parent.mkdir(parents=True)
    old = b'{ "next_index": 1 }\n'
    state.write_bytes(old)
    cli.write_text("""#!/bin/sh
(sleep 2; touch delayed-marker) &
echo "$$" > cli-pid
wait
""")
    with _running_worker(root, env, legacy=True):
        _wait_for(lambda: _worker_log_contains(root, "timed out"), what="timeout")
        assert (root / "cli-pid").exists()
        time.sleep(1.5)
        assert not (root / "delayed-marker").exists()
        assert state.read_bytes() == old


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_sigterm_finalizes_child_cleanup(beat_project):
    root, cli = beat_project
    env = _beat_env(root, cli, timeout=10)
    cli.write_text("""#!/bin/sh
(sleep 2; touch delayed-marker) &
echo "$$" > cli-pid
wait
""")
    pid_file = root / "cli-pid"
    with _running_worker(root, env) as worker:
        _wait_for(lambda: pid_file.exists() and pid_file.stat().st_size, what="agent CLI startup")
        worker.send_signal(signal.SIGTERM)
        output = worker.communicate(timeout=5)[1]
        assert worker.returncode == 0, output
        assert "SIGTERM" in output
        assert not _pid_exists(int(pid_file.read_text()))
        time.sleep(2)
        assert not (root / "delayed-marker").exists()
        assert not Path(env["SILL_BEAT_STATE_PATH"]).exists()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT"])
def test_stop_during_cli_startup_reaps_child_and_releases_lock(beat_project, signal_name):
    root, cli = beat_project
    env = _beat_env(root, cli, timeout=10)
    state = Path(env["SILL_BEAT_STATE_PATH"])
    marker = root / "delayed-marker"
    pid_file = root / "started-pid"
    cli.write_text("""#!/bin/sh
i=0
while [ "$i" -lt 100 ]; do
    if [ -e release-cli ]; then
        touch delayed-marker
        exit 0
    fi
    i=$((i + 1))
    sleep 0.05
done
exit 1
""")
    start_and_signal = """
import os
import signal
import sys
from pathlib import Path

import beat_worker

popen = beat_worker.subprocess.Popen

def start_then_signal(*args, **kwargs):
    process = popen(*args, **kwargs)
    Path("started-pid").write_text(str(process.pid))
    os.kill(os.getpid(), getattr(signal, sys.argv[1]))
    return process

beat_worker.subprocess.Popen = start_then_signal
stop_signals = (signal.SIGTERM, signal.SIGINT)
previous_handlers = [signal.getsignal(signum) for signum in stop_signals]
status = beat_worker.main(once=True)
assert [signal.getsignal(signum) for signum in stop_signals] == previous_handlers
raise SystemExit(status)
"""
    try:
        worker = subprocess.run(
            [sys.executable, "-c", start_and_signal, signal_name],
            cwd=root, env=env, capture_output=True, text=True, timeout=5,
        )
        assert worker.returncode == 1, worker.stderr
        assert f"Interrupted ({'Ctrl-C' if signal_name == 'SIGINT' else signal_name})" in worker.stderr
        child_pid = int(pid_file.read_text())
        with bw.worker_lock(state) as lock_fd:
            released = lock_fd is not None

        (root / "release-cli").touch()
        if _pid_exists(child_pid):
            _wait_for(marker.exists, what="surviving CLI's delayed effect")

        assert not marker.exists()
        assert released
        assert not _pid_exists(child_pid)
        assert not state.exists()
    finally:
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX")
def test_once_uses_same_lock_and_does_not_sleep(beat_project):
    root, cli = beat_project
    env = _beat_env(root, cli, timeout=5)
    state = Path(env["SILL_BEAT_STATE_PATH"])
    with _running_worker(root, env, legacy=True) as owner:
        _wait_for(lambda: _worker_log_contains(root, "Sleeping"), what="interval sleep")
        blocked = _worker_result(root, env, once=True)
        assert blocked.returncode == 75, blocked.stderr
        assert len(list(root.glob("notes/*.md"))) == 1
        assert json.loads(state.read_bytes())["next_index"] == 1
        owner.send_signal(signal.SIGINT)
        assert owner.wait(timeout=5) == 0

    once = _worker_result(root, env, once=True)
    assert once.returncode == 0, once.stderr
    assert "Sleeping" not in once.stderr
    assert len(list(root.glob("notes/*.md"))) == 2
    assert json.loads(state.read_bytes())["next_index"] == 0

    cli.write_text("#!/bin/sh\nexit 0\n")
    failed = _worker_result(root, env, once=True)
    assert failed.returncode == 1, failed.stderr
    assert json.loads(state.read_bytes())["next_index"] == 0


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX")
def test_spawn_beat_obeys_worker_lock_without_owning_rotation(beat_project, monkeypatch):
    root, cli = beat_project
    env = _beat_env(root, cli, timeout=5)
    state = Path(env["SILL_BEAT_STATE_PATH"])
    monkeypatch.setattr(bw, "PROJECT_ROOT", root)
    monkeypatch.setattr(bw, "CLI", str(cli))
    monkeypatch.setattr(bw, "WATCHDOG_SOCKET", None)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("SILL_BEAT_STATE_PATH", str(state))
    voices = bw.load_voices(root / "beats.json")

    with _running_worker(root, env, legacy=True) as owner:
        _wait_for(lambda: _worker_log_contains(root, "Sleeping"), what="interval sleep")
        assert bw.spawn_beat(voices[1], voices) == (False, 0.0, "")
        assert len(list(root.glob("notes/*.md"))) == 1
        owner.send_signal(signal.SIGINT)
        assert owner.wait(timeout=5) == 0

    old = state.read_bytes()
    success, duration, transcript = bw.spawn_beat(voices[1], voices)
    assert success and duration >= 0 and Path(transcript).is_file()
    assert len(list(root.glob("notes/*.md"))) == 2
    assert state.read_bytes() == old


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_worker_capture_does_not_wait_for_escaped_descendant(beat_project):
    root, cli = beat_project
    env = _beat_env(root, cli, timeout=12)
    cli.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} agent.py\n")
    (root / "agent.py").write_text(
        """
import subprocess
import sys
from pathlib import Path

escaped = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    start_new_session=True,
)
Path("escaped-pid").write_text(str(escaped.pid))
Path("notes/probe.md").write_text("output")
print("agent output")
"""
    )
    pid_file = root / "escaped-pid"
    try:
        with _running_worker(root, env, legacy=True) as worker:
            _wait_for(lambda: _worker_log_contains(root, "Beat complete"), what="captured CLI output")
            assert _pid_exists(int(pid_file.read_text()))
            transcript, = (root / "logs" / "probe").glob("*.txt")
            assert "agent output" in transcript.read_text()
            worker.send_signal(signal.SIGTERM)
            assert worker.wait(timeout=5) == 0
    finally:
        if pid_file.exists() and pid_file.stat().st_size:
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
