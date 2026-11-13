#!/usr/bin/env python3
"""Recall once for the first eligible unseen written path in each callback."""
import json
import os
import sys
import time
from pathlib import Path
from _harness import hook_session_id, written_files
from _store import core, recall_min_similarity
from _recall_state import reserve_path, release_path, finish_path


def output(text):
    print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse',
                                          'additionalContext':text}}), flush=True)


def main():
    deadline = time.monotonic() + 25
    if (os.environ.get('SILL_WRITE_RECALL') == '0' or os.environ.get('SILL_DETACHED_BEAT')
            or os.environ.get('SILL_HEADLESS_TOOL') or
            (os.environ.get('CLAUDE_CODE_ENTRYPOINT','').startswith('sdk')
             and not os.environ.get('SILL_INTERACTIVE'))):
        return
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(data, dict) or data.get('hook_event_name') != 'PostToolUse':
        return
    sid = hook_session_id(data, env=os.environ)
    if not sid or not isinstance(data.get('tool_input'), dict):
        return
    if data.get('tool_name') not in ('Write','Edit','MultiEdit','apply_patch'):
        return
    for raw_path, text in written_files(data):
        if len(text.strip()) < 80:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            cwd = data.get('cwd')
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                continue
            path = Path(cwd) / path
        try:
            path = path.resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        if (set(path.parts) & {'logs','data','.git','node_modules','.venv','memory-backups'}
                or path.suffix.lower() in {'.log','.jsonl','.csv','.sqlite','.db'}):
            continue
        try:
            owner = reserve_path(sid, str(path))
        except (OSError, ValueError, TimeoutError):
            return
        if not owner:
            continue
        try:
            query = text.strip()[:1500]
            terms = core.extract_keywords(query)
            rows = core.fused_recall(query, keywords=terms, limit=10,
                                     fast_min_similarity=recall_min_similarity(),
                                     deadline=min(deadline-5, time.monotonic()+17))
            contents = core.full_contents([r['id'] for r in rows], deadline=deadline-2)
            rows = [dict(r, content=core.focus_snippet(contents.get(r['id'],r['content']), terms)) for r in rows]
            def emit(fresh):
                if fresh:
                    lines = [f'[WRITE RECALL] Context for {path}:']
                    for row in fresh:
                        metadata = ', '.join(f'{k}={row[k]}' for k in ('ref','source_store','speaker','production','force') if row.get(k))
                        lines.append(f"[{row['type']}, score={row['score']:.4f}, id={row['id']}" + (', '+metadata if metadata else '') + '] '+row['content'])
                    output('\n'.join(lines))
            fresh = finish_path(sid, str(path), owner, rows, emit)
            if fresh:
                try:
                    core.record_exposure([r['id'] for r in fresh], deadline=deadline)
                except core.RecallUnavailable:
                    pass  # Context already delivered; never emit a second JSON object.
        except core.RecallUnavailable:
            release_path(sid, str(path), owner)
            output('[sill] Write recall unavailable; this path can be retried.')
        except (OSError, ValueError, TimeoutError):
            release_path(sid, str(path), owner)
        return


if __name__ == '__main__':
    main()
