"""Bounded per-session exposure journal and crash-recoverable path leases.

Keys are H1 filesystem keys, timestamps UTC ISO (journal) / epoch seconds
(leases). The raw hook identity is never a durable source attribution.
"""
import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from _harness import hook_session_path_key

MAX_PATHS = 4096
MAX_IDS = 8192
MAX_ENTRIES = 512
LEASE_SECONDS = 35


def _paths(sid):
    key = hook_session_path_key(sid)
    if key is None:
        raise ValueError('stable session identity required')
    root = Path(os.environ.get('SILL_LOG_DIR', '/tmp'))
    root.mkdir(parents=True, exist_ok=True)
    return (root / f'recall-state-{key}.json', root / f'recall-sidecar-{key}.jsonl',
            root / f'recall-state-{key}.lock')


def _atomic(path, text):
    # All callers hold this session's lock. A fixed temporary name bounds
    # orphan files after SIGKILL; the next write replaces the partial file.
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(sid):
    state_path, journal, lock_path = _paths(sid)
    with lock_path.open('a') as lock:
        until = time.monotonic() + .5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise TimeoutError('recall state busy')
                time.sleep(.01)
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            if not isinstance(state, dict):
                raise ValueError('invalid recall state')
            state.setdefault('paths', {})
            state.setdefault('shown', [])
            if not isinstance(state['paths'], dict) or not isinstance(state['shown'], list):
                raise ValueError('invalid recall state')
            # Read the public prompt/write journal as well, including older
            # installations that had no state index. Never consult recent.
            entries = []
            if journal.exists():
                for line in journal.read_text().splitlines()[-MAX_ENTRIES:]:
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                        for row in entry.get('memories', []):
                            if row.get('id') and row['id'] not in state['shown'] and len(state['shown']) < MAX_IDS:
                                state['shown'].append(row['id'])
                    except (ValueError, AttributeError, TypeError):
                        continue
            yield state, entries
            _atomic(state_path, json.dumps(state))
            if entries:
                _atomic(journal, ''.join(json.dumps(e)+'\n' for e in entries[-MAX_ENTRIES:]))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def reserve_path(sid, path, *, now=None):
    now = time.time() if now is None else now
    with locked(sid) as (state, _):
        paths = state['paths']
        for key, lease in list(paths.items()):
            if lease.get('owner') and lease['expires'] <= now:
                del paths[key]
        if path in paths or len(paths) >= MAX_PATHS or len(state['shown']) >= MAX_IDS:
            return None
        owner = uuid.uuid4().hex
        paths[path] = {'owner':owner, 'expires':now + LEASE_SECONDS}
        return owner


def release_path(sid, path, owner):
    try:
        with locked(sid) as (state, _):
            if state['paths'].get(path, {}).get('owner') == owner:
                del state['paths'][path]
    except (OSError, ValueError, TimeoutError):
        # A busy/unavailable journal cannot hide the recall outage. The lease
        # still expires, and ownership checks prevent releasing a newer lease.
        pass


def _append(state, entries, rows, source):
    for row in rows:
        if row['id'] not in state['shown'] and len(state['shown']) < MAX_IDS:
            state['shown'].append(row['id'])
    if rows:
        entries.append({'ts':datetime.now(timezone.utc).isoformat(), 'source':source,
                        'memories':[{'id':r['id'], 'content':r['content']} for r in rows]})


def record_shown(sid, rows, source, emit=None):
    if not sid:
        if emit:
            emit(rows)
        return
    with locked(sid) as (state, entries):
        if emit:
            emit(rows)
        _append(state, entries, rows, source)


def finish_path(sid, path, owner, rows, emit):
    with locked(sid) as (state, entries):
        lease = state['paths'].get(path, {})
        if lease.get('owner') != owner or lease['expires'] <= time.time():
            return []
        shown = set(state['shown'])
        fresh = []
        for row in rows:
            if row['id'] not in shown and len(shown) < MAX_IDS:
                fresh.append(row)
                shown.add(row['id'])
            if len(fresh) == 3:
                break
        emit(fresh)
        _append(state, entries, fresh, 'write-recall')
        state['paths'][path] = {'completed':time.time()}
        return fresh
