"""Offline host-shaped callbacks; retrieval transport is an explicit test double."""
import importlib.util
import io
import json
import os
import runpy
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest
from scripts import recall_core as core

HOOKS = Path(__file__).resolve().parents[2] / 'plugin/hooks'
TEXT = 'Cedar meadow survey records describe flowering trees and seasonal water retention. ' * 30
ROWS = [dict(id=str(UUID(int=i)), type='semantic', content=f'Synthetic cedar finding {i}', score=.0164,
             speaker='Inez', production='observation', source_store='fixture') for i in range(1, 8)]

@pytest.fixture
def callback(monkeypatch, capsys, tmp_path):
    monkeypatch.syspath_prepend(str(HOOKS))
    for name in tuple(os.environ):
        if name.startswith(('SILL_', 'CLAUDE_')):
            monkeypatch.delenv(name)
    monkeypatch.setenv('SILL_LOG_DIR', str(tmp_path))
    calls = []
    exposed = []
    def fetch(text, previous='', **kwargs):
        calls.append((text, previous, kwargs))
        return [dict(r) for r in ROWS]
    monkeypatch.setattr(core, 'fused_recall', fetch)
    monkeypatch.setattr(core, 'full_contents', lambda ids, **kw: {r['id']:r['content'] for r in ROWS if r['id'] in ids})
    monkeypatch.setattr(core, 'record_exposure', lambda ids, **kw: exposed.extend(ids))
    def run(payload, hook='write-recall.py'):
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
        try:
            runpy.run_path(str(HOOKS / hook), run_name='__main__')
        except SystemExit as exc:
            assert exc.code in (0, None)
        out = capsys.readouterr()
        assert not out.err
        return json.loads(out.out) if out.out.strip() else None
    return run, calls, exposed


def event(tool='Write', **fields):
    return dict(hook_event_name='PostToolUse', session_id='session-A', cwd='/tmp/synthetic',
                tool_name=tool, tool_input=fields or {'file_path':'notes.md','content':TEXT})


def test_write_recall_matches_only_supported_written_text(callback):
    run, calls, exposed = callback
    assert run(event('Bash', command='cat notes.md')) is None
    assert run(event('Write', file_path='small.md', content='short')) is None
    assert run(event('Write', file_path='logs/activity.log', content=TEXT)) is None
    out = run(event())
    assert out['hookSpecificOutput']['hookEventName'] == 'PostToolUse'
    assert 'systemMessage' not in out
    assert len(calls) == 1 and len(calls[0][0]) == 1500
    assert calls[0][2]['limit'] == 10
    assert len(exposed) == 3
    assert 'score=0.0164' in out['hookSpecificOutput']['additionalContext']


def test_multifile_patch_has_at_most_three_fresh_rows_and_keeps_per_file_scope(callback):
    run, calls, exposed = callback
    patch = f'*** Begin Patch\n*** Add File: logs/skip.log\n+{TEXT}\n*** Add File: a.md\n+{TEXT}\n*** Add File: b.md\n+Different text {TEXT}\n*** End Patch'
    out = run(event('apply_patch', command=patch))
    assert len(calls) == 1 and len(exposed) == 3
    assert 'a.md' in out['hookSpecificOutput']['additionalContext']
    assert 'Different text' not in calls[0][0]


def test_write_recall_dedupes_path_and_prompt_exposure_by_session(callback, tmp_path):
    run, calls, exposed = callback
    from _recall_state import record_shown
    record_shown('session-A', ROWS[:2], 'spontaneous-recall')
    assert run(event())
    assert exposed == [r['id'] for r in ROWS[2:5]]
    assert run(event(file_path='./notes.md', content=TEXT)) is None
    assert len(calls) == 1
    other = event(); other['session_id'] = 'session-B'
    assert run(other)
    assert len(calls) == 2


def test_write_recall_does_not_consume_session_path_on_outage(callback, monkeypatch):
    run, calls, exposed = callback
    fetch = core.fused_recall
    def failed(*args, **kw):
        raise core.RecallUnavailable('store unavailable')
    monkeypatch.setattr(core, 'fused_recall', failed)
    out = run(event())
    assert 'unavailable' in out['hookSpecificOutput']['additionalContext'].lower()
    assert not exposed
    monkeypatch.setattr(core, 'fused_recall', fetch)
    assert run(event()) and len(calls) == 1


@pytest.mark.parametrize('payload', [[], {}, {'hook_event_name':'PreToolUse'}, event() | {'session_id':None}, event() | {'tool_input':[] }])
def test_malformed_events_do_not_retrieve(callback, payload):
    run, calls, exposed = callback
    assert run(payload) is None
    assert not calls and not exposed


def test_detached_beat_and_malformed_events_do_not_retrieve(callback, monkeypatch):
    run, calls, exposed = callback
    monkeypatch.setenv('SILL_DETACHED_BEAT','1')
    monkeypatch.setenv('SILL_INTERACTIVE','1')
    assert run(event()) is None
    assert not calls


def test_reservation_ownership_and_expiry(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HOOKS))
    monkeypatch.setenv('SILL_LOG_DIR',str(tmp_path))
    from _recall_state import reserve_path, release_path
    first = reserve_path('s','/a', now=10)
    assert first and reserve_path('s','/a', now=11) is None
    second = reserve_path('s','/a', now=50)
    assert second and second != first
    release_path('s','/a', first)
    assert reserve_path('s','/a', now=51) is None
    release_path('s','/a', second)
    assert reserve_path('s','/a', now=51)


def test_concurrent_callbacks_dedupe_paths_and_ids_across_processes(tmp_path):
    import subprocess
    script = '''import os, sys, time, runpy
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from scripts import recall_core as core
rows = ''' + repr(ROWS) + '''
def fetch(*a, **kw):
    with open(os.environ['SILL_LOG_DIR']+'/queries','a') as log: log.write('query\\n')
    time.sleep(.08)
    return rows
core.fused_recall=fetch
core.full_contents=lambda *a, **kw: {}
core.record_exposure=lambda *a, **kw: None
runpy.run_path(sys.argv[1]+'/write-recall.py',run_name='__main__')
'''
    env = {k:v for k,v in os.environ.items() if not k.startswith(('SILL_','CLAUDE_'))}
    env['SILL_LOG_DIR'] = str(tmp_path)
    procs=[]
    for name in ['a.md','a.md','b.md','c.md','d.md','d.md']:
        proc=subprocess.Popen([sys.executable,'-c',script,str(HOOKS),str(HOOKS.parents[1]/'backend')],
                              stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        proc.stdin.write(json.dumps(event(file_path=name,content=TEXT))); proc.stdin.close()
        procs.append(proc)
    outputs=[]
    for proc in procs:
        stdout=proc.stdout.read(); stderr=proc.stderr.read()
        assert proc.wait(timeout=10) == 0 and not stderr
        if stdout.strip():
            outputs.append(json.loads(stdout)['hookSpecificOutput']['additionalContext'])
    joined='\n'.join(outputs)
    assert all(joined.count(r['id']) == 1 for r in ROWS)
    assert len((tmp_path/'queries').read_text().splitlines()) == 4
    assert all(text.count('score=') <= 3 for text in outputs)


def test_state_capacity_stops_new_paths_without_forgetting_shown_ids(tmp_path,monkeypatch):
    from _recall_state import locked, reserve_path, MAX_PATHS
    monkeypatch.setenv('SILL_LOG_DIR',str(tmp_path))
    with locked('bounded') as (state, entries):
        state['paths'] = {str(i):{'completed':1} for i in range(MAX_PATHS)}
        state['shown'] = ['keep-me']
    assert reserve_path('bounded','another') is None
    with locked('bounded') as (state, entries):
        assert state['shown'] == ['keep-me']


def test_write_callback_uses_one_deadline_when_hydration_and_accounting_are_slow(callback,monkeypatch):
    run,calls,exposed=callback
    clock=[100.0]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    budgets=[]
    def fetch(*a,**kw):
        budgets.append(kw['deadline']); clock[0]=kw['deadline']
        return ROWS
    def hydrate(*a,**kw):
        budgets.append(kw['deadline']); clock[0]=kw['deadline']
        return {}
    def account(*a,**kw):
        budgets.append(kw['deadline']); clock[0]=kw['deadline']
    monkeypatch.setattr(core,'fused_recall',fetch)
    monkeypatch.setattr(core,'full_contents',hydrate)
    monkeypatch.setattr(core,'record_exposure',account)
    assert run(event())
    assert budgets == [117,123,125]


def test_registration_renders_write_callback_with_installed_interpreter():
    template=json.loads((HOOKS.parent/'codex.hooks.json.template').read_text()
                        .replace('{{SILL_PYTHON}}','/fixture/venv/bin/python')
                        .replace('{{SILL_PLUGIN_DIR}}','/fixture/plugin'))
    groups=template['hooks']['PostToolUse']
    assert groups[0]['matcher'] == 'Write|Edit|MultiEdit|apply_patch'
    assert groups[0]['hooks'][0]['timeout'] == 30
    assert groups[0]['hooks'][0]['command'] == '/fixture/venv/bin/python /fixture/plugin/hooks/write-recall.py'


def test_invalid_filesystem_path_is_a_malformed_event(callback):
    run,calls,exposed=callback
    assert run(event(file_path='bad\x00name.md',content=TEXT)) is None
    assert not calls and not exposed


def test_healthy_zero_consumes_path_but_accounting_failure_does_not_duplicate_context(callback,monkeypatch):
    run,calls,exposed=callback
    fetch=core.fused_recall
    monkeypatch.setattr(core,'fused_recall',lambda *a,**kw: [])
    assert run(event()) is None
    monkeypatch.setattr(core,'fused_recall',fetch)
    assert run(event()) is None
    def failed(*a,**kw):
        raise core.RecallUnavailable('private DSN must not escape')
    monkeypatch.setattr(core,'record_exposure',failed)
    out=run(event(file_path='second.md',content=TEXT))
    assert 'private DSN' not in json.dumps(out)
    assert out['hookSpecificOutput']['additionalContext'].count('score=') == 3
    assert run(event(file_path='second.md',content=TEXT)) is None


def test_codex_session_identity_wins_over_claude_environment_for_reuse(callback,monkeypatch):
    run,calls,exposed=callback
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID','unrelated-claude-session')
    payload=event() | {'turn_id':'codex-turn'}
    assert run(payload)
    import importlib.util
    spec=importlib.util.spec_from_file_location('write_reuse_reader',HOOKS/'track-reuse.py')
    reuse=importlib.util.module_from_spec(spec); spec.loader.exec_module(reuse)
    rows=reuse.read_recall_sidecars('session-A')
    assert [r['id'] for r in rows] == exposed
    assert all(r['channel'] == 'write-recall' for r in rows)
    assert reuse.read_recall_sidecars('unrelated-claude-session') == []


def test_interrupted_atomic_writes_leave_bounded_temporary_files(tmp_path):
    import subprocess
    script='''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import _recall_state as state
state.os.replace=lambda *args: os._exit(7)
state._atomic(Path(sys.argv[2]), '{}')
'''
    for _ in range(3):
        result=subprocess.run([sys.executable,'-c',script,str(HOOKS),str(tmp_path/'state.json')],timeout=5)
        assert result.returncode == 7
    assert len(list(tmp_path.glob('*.tmp'))) == 1


@pytest.mark.parametrize('value,expected',[('0.75',.75),('NaN',.25),('0',0)])
def test_write_recall_honors_the_shared_fast_cosine_floor(callback,monkeypatch,value,expected):
    run,calls,exposed=callback
    monkeypatch.setenv('SILL_RECALL_MIN_SIMILARITY',value)
    assert run(event())
    assert calls[0][2]['fast_min_similarity'] == expected


def test_outage_remains_visible_when_reservation_cleanup_lock_is_busy(callback,monkeypatch):
    import _recall_state
    run,calls,exposed=callback
    def busy(*args,**kwargs):
        raise TimeoutError('another callback holds the lock')
    def failed(*args,**kwargs):
        monkeypatch.setattr(_recall_state,'locked',busy)
        raise core.RecallUnavailable('store unavailable')
    monkeypatch.setattr(core,'fused_recall',failed)
    output=run(event())
    assert 'unavailable' in output['hookSpecificOutput']['additionalContext']
    assert not exposed
