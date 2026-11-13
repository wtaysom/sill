#!/usr/bin/env python3
"""
Beat worker: a config-driven, alternating reflective loop.

Every SILL_BEAT_INTERVAL_SECONDS (default 7200 = 2h), spawns one headless
agent-CLI session running the next voice in a configured rotation (see
SILL_BEAT_CONFIG, default beats.json under the project root; example at
beats.example.json). Each voice gets its own standing prompt, its own
transcript directory, and — optionally — an output_glob checked after every
beat so an exit-0 subprocess that produced nothing does not get counted as
a success.

Detached by design: the subprocess has no interactive user, so anything
that would prompt for permission or wait for direction has nowhere to
route. A voice must decide and act on its own.

Every child also gets SILL_BEAT_JOURNAL_DIRS set to the voice config's own
directories (see journal_dirs_for_voices()) — the scope the two receipt
guards (plugin/hooks/stored-slot-guard.py, tool-type-witness.py) and
state-language-check.py's beat-aware fallback read. This is the only place
that variable is ever set: an install with no beats.json never sets it.

Rotation state persists outside /tmp (see default_state_path()) so a
worker restart, or an unrelated reboot that clears /tmp, cannot silently
reset which voice is due next with no record that it happened.

Ported from agi-memory's gnomon_worker.py (GNOMON_* env renamed to
SILL_BEAT_*, its bounded-trial correspondence channel excised entirely,
per-voice output verification added).
"""

import fcntl
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from scripts import beat_launchers


# ---------------------------------------------------------------------------
# Configuration (env-driven; see docs/beats.md for the full surface)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(os.environ.get("SILL_PROJECT_ROOT", ".")).resolve()
INTERVAL_SECONDS = int(os.environ.get("SILL_BEAT_INTERVAL_SECONDS", "7200"))  # 2h
TIMEOUT_SECONDS = int(os.environ.get("SILL_BEAT_TIMEOUT_SECONDS", "1800"))  # 30min
CLI = "claude"
WATCHDOG_SOCKET = os.environ.get("SILL_BEAT_WATCHDOG_SOCKET") or None
POST_HOOK = os.environ.get("SILL_BEAT_POST_HOOK") or None
LOG_PATH = Path(
    os.environ.get("SILL_BEAT_LOG_PATH")
    or (Path(os.environ.get("SILL_LOG_DIR", "/tmp")) / "beat-worker.log")
)

logger = logging.getLogger("beat_worker")


def _config_path() -> Path:
    override = os.environ.get("SILL_BEAT_CONFIG")
    return Path(override) if override else PROJECT_ROOT / "beats.json"


def _configure_logging() -> None:
    """Attach the beat-worker log file. Deferred out of module import (unlike
    the upstream worker, which did this at import time) so importing this
    module for tests never touches the filesystem as a side effect.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    if logger.handlers:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_PATH)
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning(f"Could not attach beat-worker log file {LOG_PATH}: {exc}")


# ---------------------------------------------------------------------------
# Voice configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Voice:
    """One rotation slot, read verbatim from one entry of the `voices` array
    in the JSON config. Paths (prompt/transcripts/output_glob) stay relative
    strings — they're resolved against PROJECT_ROOT at spawn/check time, not
    here.
    """
    name: str
    prompt: str
    transcripts: str
    output_glob: str | None
    kickoff: str
    launcher: beat_launchers.Launcher = "claude"
    cli: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if self.launcher not in ("claude", "codex"):
            raise ValueError(f"voice {self.name!r}: invalid launcher {self.launcher!r}")
        for key, value in (("cli", self.cli), ("model", self.model)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"voice {self.name!r}: {key} must be a nonempty string")


def load_voices(path: Path) -> list[Voice]:
    """Read voice definitions from a beats.json config. Each entry in the
    top-level `voices` array becomes one Voice, in rotation order.
    """
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc

    raw_voices = data.get("voices", []) if isinstance(data, dict) else []
    if not raw_voices:
        raise ValueError(f"no voices defined in {path}")

    voices = []
    for i, entry in enumerate(raw_voices):
        if not isinstance(entry, dict):
            raise ValueError(f"voice entry {i} in {path} is not an object")
        try:
            voices.append(Voice(
                name=entry["name"],
                prompt=entry["prompt"],
                transcripts=entry["transcripts"],
                output_glob=entry.get("output_glob"),
                kickoff=entry.get("kickoff", "Begin."),
                launcher=entry.get("launcher", "claude"),
                cli=entry.get("cli"),
                model=entry.get("model"),
            ))
        except KeyError as exc:
            raise ValueError(f"voice entry {i} in {path} is missing required key {exc}") from exc
    return voices


# ---------------------------------------------------------------------------
# Guard scope derivation — plugin/hooks/stored-slot-guard.py and
# tool-type-witness.py are opt-in via SILL_BEAT_JOURNAL_DIRS (unset = no
# scope = they check nothing), and state-language-check.py reads the same
# variable too — but there it REPLACES, not adds to, its journals/+docs/
# default: once this is set, only the derived voice directories are in
# scope, so a beat child writing under journals/ or docs/ directly is no
# longer state-language-checked. Nothing in this codebase sets that
# variable except spawn_beat() below, computed fresh from the voice config
# already loaded for this worker — so a beat's own journal writes are
# guarded with zero operator configuration, and an install that never
# defines voices never sets the variable at all.
# ---------------------------------------------------------------------------

_GLOB_METACHARS = re.compile(r"[*?\[]")


def _literal_prefix_dir(glob_pattern: str) -> str:
    """The longest literal (no *, ?, or [) leading directory of a glob
    pattern, e.g. 'journals/sili-beats/*.md' -> 'journals/sili-beats'.

    A '**' path segment (matched by _GLOB_METACHARS same as '*') is itself
    a wildcard, not a directory name — 'journals/**/*.md' must stop at
    'journals', not include the '**' component. os.path.dirname() doesn't
    know this: it would return 'journals/**' verbatim, and the guards that
    consume this fragment do a plain substring check (`f in path`), never
    re-interpreting '**' as a glob — so a fragment containing the literal
    two characters "**" can never match a real write path, silently
    disabling the guards for that voice. Stopping at the last literal
    ancestor instead (here, 'journals/') keeps the scope correct, if
    broader than a non-recursive glob's exact parent directory would be.
    """
    parts = glob_pattern.split("/")
    literal: list[str] = []
    for part in parts[:-1]:  # the last part is the filename pattern, never a directory
        if _GLOB_METACHARS.search(part):
            break
        literal.append(part)
    return "/".join(literal)


def journal_dirs_for_voices(voices: list[Voice]) -> str:
    """Colon-joined, deduped, stable-order scope fragments for
    SILL_BEAT_JOURNAL_DIRS: the literal leading directory of every voice's
    output_glob (see _literal_prefix_dir), then that voice's transcripts
    dir verbatim (already a directory, not a glob) — in voice order,
    first-seen-wins on dupes. Both are exactly the paths a beat's own
    writes land under, so together they are the guards' correct scope
    without any operator input.
    """
    fragments: list[str] = []

    def add(fragment: str) -> None:
        normalized = fragment if fragment.endswith("/") else f"{fragment}/"
        if normalized not in fragments:
            fragments.append(normalized)

    for v in voices:
        if v.output_glob:
            directory = _literal_prefix_dir(v.output_glob)
            if directory:
                add(directory)
        if v.transcripts:
            add(v.transcripts)
    return ":".join(fragments)


# ---------------------------------------------------------------------------
# Rotation state (never /tmp — see default_state_path())
# ---------------------------------------------------------------------------

def default_state_path() -> Path:
    """Resolve where rotation state lives: SILL_BEAT_STATE_PATH if set, else
    the XDG state dir. Deliberately never /tmp: the upstream lesson this
    guards against is a reboot silently clearing /tmp and resetting rotation
    to voice 0 with nothing in the log to say so.
    """
    override = os.environ.get("SILL_BEAT_STATE_PATH")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "sill" / "beat-rotation.json"


def read_state(path: Path) -> int:
    """Read the rotation index, tolerating a missing or corrupt file. A beat
    worker must never crash its loop over a bad state file — worst case, it
    restarts rotation at voice 0.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("next_index", 0))
    except (TypeError, ValueError):
        return 0


@contextmanager
def worker_lock(state_path: Path) -> Iterator[int | None]:
    """Hold exclusive worker ownership until the parent and agent CLI exit."""
    path = state_path.resolve()
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        # Close only our descriptor on exit. An inherited CLI descriptor
        # preserves ownership if the worker dies. The pathname stays stable.
        yield lock.fileno()


def atomic_json(path: Path, data: dict) -> None:
    """Replace a JSON file only after its complete new contents are on disk."""
    temporary = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            json.dump(data, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_state(path: Path, index: int, *, attempt_id: str | None = None) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"next_index": int(index)}
    if attempt_id is not None:
        state["last_successful_attempt_id"] = attempt_id
    atomic_json(path, state)


def advance_if(path: Path, *, index: int, n_voices: int, success: bool) -> None:
    """Advance rotation past `index` only on verified success. On failure,
    the state file is left untouched, so the next scheduled beat retries
    the same voice rather than moving on.
    """
    if not success:
        return
    write_state(path, (index + 1) % n_voices)


# ---------------------------------------------------------------------------
# Transient-failure classification (diagnostic only — never changes whether
# rotation advances, only what gets logged and whether a transcript is kept)
# ---------------------------------------------------------------------------

_SKIP_PATTERNS = (
    "api error: unable to connect to api",
    "econnreset",
    "socket connection was closed unexpectedly",
    "failed to fetch",
    "connection error",
    "network error",
    "not logged in",
    "please run /login",
)


def classify_skip(output: str, duration: float) -> str | None:
    """Classify subprocess output as a transient infrastructure failure that
    should not consume the voice's rotation turn, as opposed to a real
    failure worth investigating. The caller is expected to call this only
    for a nonzero exit; a clean exit is never a "skip".
    """
    lowered = (output or "").lower()
    if any(pattern in lowered for pattern in _SKIP_PATTERNS):
        if "not logged in" in lowered or "please run /login" in lowered:
            return "the agent CLI is not logged in in this launch environment"
        if "econnreset" in lowered:
            return "the agent CLI's connection was reset"
        if "socket connection was closed unexpectedly" in lowered and duration > 600:
            return "the connection dropped for a long stretch (sleep or network loss)"
        if "socket connection was closed unexpectedly" in lowered:
            return "the agent CLI's socket closed unexpectedly"
        return "the agent CLI could not connect"

    # A timeout before any useful output is an infrastructure skip, not a beat.
    if duration >= TIMEOUT_SECONDS and not output.strip():
        return "the agent CLI subprocess timed out before producing output"

    return None


# ---------------------------------------------------------------------------
# Output verification — the guard for the silent-failure mode: a headless
# agent CLI with no permission allow-list gets its tools auto-denied rather
# than prompted, exits 0, and has done nothing. Exit code alone cannot see
# this; only checking for the voice's declared output can.
# ---------------------------------------------------------------------------

def snapshot_outputs(root: Path, glob: str) -> set[Path]:
    """Snapshot the files currently matching a voice's output_glob, so a
    later call can detect whether a beat added anything.
    """
    root = Path(root)
    paths = (Path(os.path.normpath(path)) for path in root.glob(glob))
    return {path for path in paths if path.is_relative_to(root) and path.is_file()}


def produced_output(root: Path, glob: str | None, before: set[Path]) -> bool:
    """True if `glob` gained a new file under `root` since `before`. A voice
    with no output_glob cannot be checked this way, so the answer is True by
    convention — absence of a way to verify is not evidence of failure.
    """
    if not glob:
        return True
    after = snapshot_outputs(root, glob)
    return bool(after - before)


# ---------------------------------------------------------------------------
# Optional log-only helpers (never block or fail a beat)
# ---------------------------------------------------------------------------

def _wedge_probe(socket_path: str) -> None:
    """Ping a local daemon socket before spawning a beat (e.g. a container
    engine the harness depends on). Log-only: never repairs anything, never
    blocks a beat — a WARNING is the whole effect, for a human to notice.
    """
    try:
        probe = subprocess.run(
            ["curl", "--max-time", "5", "--unix-socket", socket_path, "http://localhost/_ping"],
            capture_output=True, text=True, timeout=10,
        )
        if probe.stdout.strip() != "OK":
            logger.warning(
                f"[watchdog] socket ping failed before beat (stdout={probe.stdout!r}, "
                f"rc={probe.returncode}) — the dependency behind {socket_path} may be down"
            )
    except Exception as exc:
        logger.warning(f"[watchdog] probe error before beat: {exc}")


def _run_post_hook(command: str) -> None:
    """Run an optional command after a successful beat (e.g. a housekeeping
    script). Keyed on exit code only, never on output conventions, and never
    fails the beat that triggered it.
    """
    try:
        result = subprocess.run(
            shlex.split(command), capture_output=True, text=True,
            timeout=60, cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            logger.info(f"[post-hook] {command!r} exited 0")
        else:
            detail = (result.stderr or result.stdout).strip()[:500]
            logger.warning(f"[post-hook] {command!r} exited {result.returncode}: {detail}")
    except Exception as exc:
        logger.warning(f"[post-hook] {command!r} failed to run: {exc}")


# ---------------------------------------------------------------------------
# Recording and spawning a beat
# ---------------------------------------------------------------------------

@dataclass
class AttemptReceipt:
    attempt_id: str
    voice: str
    session_id: str
    requested_backend: str
    requested_model: str | None
    requested_host_session_id: str | None
    schema_version: int = 1
    host_session_id: str | None = None
    effective_model: str | None = None
    model_source: str | None = None
    status: str = "running"
    spawned_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    returncode: int | None = None
    state_advanced: bool | None = None
    transcript: str | None = None
    new_outputs: list[str] = field(default_factory=list)


def _clock() -> str:
    return datetime.now().astimezone().isoformat()


def _save_receipt(path: Path, receipt: AttemptReceipt) -> bool:
    try:
        atomic_json(path, asdict(receipt))
    except OSError as exc:
        logger.error("[%s] Could not write %s receipt %s: %s", receipt.voice, receipt.status, path, exc)
        return False
    return True


def _write_transcript(path: Path, receipt: AttemptReceipt, result: beat_launchers.LaunchResult) -> None:
    """Keep the final agent message and host diagnostics beside the receipt."""
    path.write_text(
        f"# {receipt.voice.title()} beat\n"
        f"# Spawned: {receipt.spawned_at}\n"
        f"# Timestamp: {receipt.completed_at}\n"
        f"# Duration: {receipt.duration_seconds:.1f}s\n"
        f"# Exit code: {receipt.returncode}\n"
        f"# Backend: {receipt.requested_backend}\n"
        f"# Attempt: {receipt.attempt_id}\n"
        f"# Model requested: {receipt.requested_model or '(CLI default)'}\n"
        f"# Model: {receipt.effective_model or 'unknown'} (source: {receipt.model_source or 'unknown'})\n"
        + "=" * 60
        + "\n\n"
        + result.text
        + (f"\n\n# Diagnostics:\n{result.diagnostics}" if result.diagnostics else ""),
        encoding="utf-8",
    )


def _child_env(voice: Voice, voices: list[Voice], receipt: AttemptReceipt) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
        "SILL_INTERACTIVE", "SILL_BEAT_JOURNAL_DIRS",
    ):
        env.pop(key, None)
    env.update(
        SILL_DETACHED_BEAT="1",
        SILL_ATTEMPT_ID=receipt.attempt_id,
        SILL_SESSION_ID=receipt.session_id,
        SILL_SESSION_ID_SOURCE="worker_attempt",
    )
    if voice.launcher == "claude":
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
    if journal_dirs := journal_dirs_for_voices(voices):
        env["SILL_BEAT_JOURNAL_DIRS"] = journal_dirs
    return env


@contextmanager
def _defer_stop_signals() -> Iterator[None]:
    """Run existing Python stop handlers after Popen has returned the child."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    handlers = {}
    pending = None

    def defer(signum, frame):
        nonlocal pending
        if pending is None:
            pending = signum, frame

    try:
        # Deferring in Python leaves the child's inherited signal mask intact.
        for signum in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(signum)
            if callable(handler):
                handlers[signum] = handler
                signal.signal(signum, defer)
        yield
    finally:
        # Restore all handlers before a newly arriving signal can interrupt
        # restoration. This mask is set only in the parent, after Popen.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handlers)
        try:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if pending is not None:
            signum, frame = pending
            handlers[signum](signum, frame)


def _run_beat_subprocess(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float, lock_fd: int,
    on_spawn: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run and capture a detached CLI. Stop its process group on interruption."""
    # A descendant that leaves the group can keep writing after the CLI
    # exits. Regular files let us collect output without waiting for EOF
    # from every inherited descriptor as we would with pipes.
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr,
    ):
        process = None
        try:
            with _defer_stop_signals():
                process = subprocess.Popen(
                    argv, cwd=cwd, env=env, start_new_session=True, pass_fds=(lock_fd,),
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                )
                if on_spawn is not None:
                    on_spawn()
            returncode = process.wait(timeout=timeout)
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(argv, returncode, stdout.read(), stderr.read())
        except BaseException:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise


def spawn_beat(voice: Voice, voices: list[Voice]) -> tuple[bool, float, str]:
    """Run one voice under the worker lock, without changing rotation.

    Return (verified_success, duration_seconds, transcript_path). If the
    lock is held by another worker, return (False, 0.0, "") without launching.
    `voices` is the full rotation used to derive the child's guard scope.
    """
    state_path = default_state_path()
    try:
        with worker_lock(state_path) as lock_fd:
            if lock_fd is None:
                logger.warning("A beat worker already owns rotation state %s", state_path)
                return False, 0.0, ""
            return _spawn_beat_locked(voice, voices, lock_fd=lock_fd)
    except OSError as exc:
        logger.error("[%s] Beat attempt error: %s", voice.name, exc)
        return False, 0.0, ""


def _run_attempt(
    voice: Voice, voices: list[Voice], receipt: AttemptReceipt, receipt_path: Path, lock_fd: int,
) -> None:
    before = None
    start = None
    result = None

    def mark_spawned() -> None:
        receipt.spawned_at = _clock()
        atomic_json(receipt_path, asdict(receipt))

    try:
        prompt_path = PROJECT_ROOT / voice.prompt
        standing_prompt = prompt_path.read_text(encoding="utf-8")
        prompt = f"{standing_prompt}\n\n---\n\n{voice.kickoff}\n"
        before = snapshot_outputs(PROJECT_ROOT, voice.output_glob) if voice.output_glob else None
        env = _child_env(voice, voices, receipt)
        cli = voice.cli or ((env.get("SILL_BEAT_CLI") or CLI) if voice.launcher == "claude" else "codex")
        argv = beat_launchers.command(voice.launcher, cli, voice.model, receipt.attempt_id, PROJECT_ROOT, prompt)
        if WATCHDOG_SOCKET:
            _wedge_probe(WATCHDOG_SOCKET)
        start = time.monotonic()
        result = _run_beat_subprocess(
            argv, cwd=PROJECT_ROOT, env=env, timeout=TIMEOUT_SECONDS, lock_fd=lock_fd, on_spawn=mark_spawned,
        )
        receipt.returncode = result.returncode
    except subprocess.TimeoutExpired:
        receipt.status = "timeout"
        logger.warning("[%s] Beat subprocess timed out after %ss", voice.name, TIMEOUT_SECONDS)
    except (OSError, ValueError, NotImplementedError) as exc:
        receipt.status = "launch_error"
        logger.error("[%s] Beat attempt error: %s", voice.name, exc)
    finally:
        receipt.completed_at = _clock()
        receipt.duration_seconds = time.monotonic() - start if start is not None else 0.0
        if before is not None and receipt.spawned_at is not None:
            try:
                after = snapshot_outputs(PROJECT_ROOT, voice.output_glob)
                receipt.new_outputs = sorted(path.relative_to(PROJECT_ROOT).as_posix() for path in after - before)
            except OSError as exc:
                logger.error("[%s] Could not check beat outputs: %s", voice.name, exc)
                if result is not None:
                    receipt.status = "failed"

    if result is None:
        return
    parsed = beat_launchers.parse_result(voice.launcher, result, receipt.attempt_id, PROJECT_ROOT, env)
    receipt.host_session_id = parsed.host_session_id
    receipt.effective_model = parsed.effective_model
    receipt.model_source = parsed.model_source

    if parsed.failed:
        output = "\n".join((parsed.text, parsed.diagnostics))
        skip_reason = classify_skip(output, receipt.duration_seconds) if result.returncode != 0 else None
        if skip_reason:
            receipt.status = "skipped"
            logger.warning("[%s] Beat attempt skipped — %s (exit %s)", voice.name, skip_reason, result.returncode)
            return
        receipt.status = "failed"
        logger.error("[%s] Beat CLI failed (exit %s)", voice.name, result.returncode)
    elif receipt.status == "running":
        if voice.output_glob and not receipt.new_outputs:
            receipt.status = "no_record"
            logger.warning(
                "[%s] Beat exited 0 in %.1fs but produced no file matching %r — "
                "the agent CLI may be denying tools non-interactively. Rotation remains on [%s].",
                voice.name, receipt.duration_seconds, voice.output_glob, voice.name,
            )
        else:
            receipt.status = "succeeded"

    transcript_path = receipt_path.with_suffix(".txt")
    try:
        _write_transcript(transcript_path, receipt, parsed)
    except OSError as exc:
        receipt.status = "failed"
        logger.error("[%s] Could not write beat transcript %s: %s", voice.name, transcript_path, exc)
    else:
        receipt.transcript = str(transcript_path)


def _finish_attempt(
    receipt: AttemptReceipt, path: Path, rotation: tuple[Path, int] | None, n_voices: int,
) -> bool:
    if receipt.status != "succeeded" or rotation is None:
        receipt.state_advanced = False
        return _save_receipt(path, receipt) and receipt.status == "succeeded"

    if not _save_receipt(path, receipt):
        return False
    state_path, index = rotation
    try:
        write_state(state_path, (index + 1) % n_voices, attempt_id=receipt.attempt_id)
    except OSError as exc:
        receipt.status = "state_error"
        receipt.state_advanced = False
        logger.error("[%s] Could not advance rotation for attempt %s: %s", receipt.voice, receipt.attempt_id, exc)
        _save_receipt(path, receipt)
        return False

    receipt.state_advanced = True
    if not _save_receipt(path, receipt):
        logger.warning("[%s] Rotation state was committed for attempt %s", receipt.voice, receipt.attempt_id)
    return True


def _spawn_beat_locked(
    voice: Voice, voices: list[Voice], *, lock_fd: int, rotation: tuple[Path, int] | None = None,
) -> tuple[bool, float, str]:
    attempt_id = str(uuid4())
    receipt = AttemptReceipt(
        attempt_id=attempt_id, voice=voice.name, session_id=f"beat:{attempt_id}",
        requested_backend=voice.launcher, requested_model=voice.model,
        requested_host_session_id=attempt_id if voice.launcher == "claude" else None,
    )
    receipt_path = PROJECT_ROOT / voice.transcripts / f"{attempt_id}.json"
    try:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("[%s] Could not create receipt directory %s: %s", voice.name, receipt_path.parent, exc)
        return False, 0.0, ""
    persisted = finalized = False
    try:
        with _defer_stop_signals():
            persisted = _save_receipt(receipt_path, receipt)
        if not persisted:
            return False, 0.0, ""
        _run_attempt(voice, voices, receipt, receipt_path, lock_fd)
        # A stop signal during the terminal writes must not interrupt the
        # transition between the receipt and its rotation witness.
        with _defer_stop_signals():
            success = _finish_attempt(receipt, receipt_path, rotation, len(voices))
            finalized = True
    except KeyboardInterrupt:
        if persisted and not finalized:
            receipt.status = "interrupted"
            receipt.completed_at = receipt.completed_at or _clock()
            receipt.duration_seconds = receipt.duration_seconds or 0.0
            receipt.state_advanced = False
            _save_receipt(receipt_path, receipt)
        raise

    duration = receipt.duration_seconds or 0.0
    if success:
        logger.info("[%s] Beat complete in %.1fs — transcript %s", voice.name, duration, Path(receipt.transcript).name)
    return success, duration, receipt.transcript or ""


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

class _WorkerTerminated(KeyboardInterrupt):
    """Route SIGTERM through the same cleanup as Ctrl-C."""


def _handle_sigterm(_signum, _frame) -> None:
    raise _WorkerTerminated


def run_beat_loop(*, once: bool = False) -> int:
    """Own rotation state while running one attempt or a scheduled loop."""
    _configure_logging()
    state_path = default_state_path().resolve()
    with worker_lock(state_path) as lock_fd:
        if lock_fd is None:
            logger.warning("A beat worker already owns rotation state %s", state_path)
            return 75

        config_path = _config_path()
        voices = load_voices(config_path)
        logger.info("Starting beat worker")
        logger.info(f"  Project root: {PROJECT_ROOT}")
        logger.info(f"  Config: {config_path}")
        if not once:
            logger.info(f"  Interval: {INTERVAL_SECONDS}s ({INTERVAL_SECONDS / 3600:.1f}h)")
        logger.info(f"  Beat timeout: {TIMEOUT_SECONDS}s")
        logger.info(f"  Rotation state: {state_path}")
        for v in voices:
            logger.info(f"  Voice [{v.name}]: prompt={v.prompt}, transcripts={v.transcripts}")
        logger.info(f"  Guard scope (SILL_BEAT_JOURNAL_DIRS for each child): {journal_dirs_for_voices(voices)!r}")

        try:
            while True:
                beat_start = time.monotonic()
                index = read_state(state_path) % len(voices)
                voice = voices[index]

                success, _duration, _transcript = _spawn_beat_locked(
                    voice, voices, lock_fd=lock_fd, rotation=(state_path, index),
                )
                if success and POST_HOOK:
                    _run_post_hook(POST_HOOK)
                if once:
                    return 0 if success else 1

                sleep_time = max(0, INTERVAL_SECONDS - (time.monotonic() - beat_start))
                if sleep_time > 0:
                    next_index = read_state(state_path) % len(voices)
                    logger.info(f"Sleeping {sleep_time:.0f}s until next beat [{voices[next_index].name}]...")
                    time.sleep(sleep_time)
        except KeyboardInterrupt as exc:
            reason = "SIGTERM" if isinstance(exc, _WorkerTerminated) else "Ctrl-C"
            next_index = read_state(state_path) % len(voices)
            logger.info(
                f"Interrupted ({reason}) — beat worker stopped. Rotation stays on "
                f"[{voices[next_index].name}]; starting the worker again resumes there."
            )
            return 1 if once else 0


def main(*, once: bool = False) -> int:
    """CLI boundary for signal handling and worker exit status."""
    previous = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        return run_beat_loop(once=once)
    except KeyboardInterrupt as exc:
        reason = "SIGTERM" if isinstance(exc, _WorkerTerminated) else "Ctrl-C"
        logger.info("Interrupted (%s) — beat worker stopped.", reason)
        return 1 if once else 0
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
