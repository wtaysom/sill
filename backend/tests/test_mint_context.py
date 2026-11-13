import pytest

import session_identity as s


def test_unknown_identity_stays_unknown(tmp_path):
    (tmp_path / 'newest.jsonl').write_text('{"session_id":"unrelated","model":"model-7"}')
    ctx = s.mint_context('test', env={}, cwd=str(tmp_path), when='W')
    assert ctx['session'] is None and ctx['model'] is None and ctx['transcript'] is None
    ambiguous = s.session_context(env={'CODEX_THREAD_ID':'one', 'CLAUDE_CODE_SESSION_ID':'two'})
    assert ambiguous['host_session_id'] is None and ambiguous['session_id'] is None
    resolved = s.session_context({'session_id':'two'}, env={'CODEX_THREAD_ID':'one','CLAUDE_CODE_SESSION_ID':'two'})
    assert resolved['host_session_id'] == 'two'
    assert resolved['session_id'] == 'two' and resolved['session_joinable'] is True


def test_attempt_and_host_are_distinct():
    env = {'SILL_SESSION_ID':'attempt:a', 'SILL_SESSION_ID_SOURCE':'worker_attempt',
           'SILL_ATTEMPT_ID':'a', 'CODEX_THREAD_ID':'host', 'SILL_VOICE':'Field Guide'}
    ctx = s.session_context(env=env)
    assert ctx['session_id'] == 'attempt:a' and ctx['host_session_id'] == 'host'
    stamp = s.mint_context('test', env=env, when='W', cwd='/synthetic')
    assert stamp['attempt'] == 'a' and stamp['session'] == 'attempt:a'
    assert stamp['host_session_id'] == 'host' and stamp['model'] is None
    assert stamp['as'] == 'Field Guide'


def test_attempt_is_never_used_to_derive_a_host_transcript(monkeypatch):
    calls = []
    monkeypatch.setattr(s, 'derive_transcript', lambda session, env, **kwargs: calls.append(session))
    s.mint_context('test', env={'SILL_SESSION_ID':'attempt:a',
                   'SILL_SESSION_ID_SOURCE':'worker_attempt'}, cwd='/synthetic', when='W')
    assert calls == [None]


@pytest.mark.parametrize('native_key', ['CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID'])
@pytest.mark.parametrize('scope', [None, 'worker_attempt', 'explicit'])
def test_conflicting_hook_and_native_id_stays_unknown(native_key, scope, monkeypatch):
    env = {native_key: 'inherited-parent'}
    if scope is not None:
        env.update(SILL_SESSION_ID='independent-scope', SILL_SESSION_ID_SOURCE=scope)
    payload = {'session_id': 'child-hook'}
    ctx = s.session_context(payload, env=env)
    assert ctx['host_session_id'] is None
    assert ctx['host_session_id_source'] == 'conflicting_host_evidence'
    assert ctx['hook_session_id'] == 'child-hook'
    assert ctx['session_id'] == ('independent-scope' if scope is not None else None)
    assert ctx['session_joinable'] is (scope is not None)
    assert ctx['session_scope'] == ('attempt' if scope == 'worker_attempt' else scope)

    # No inherited parent or child hook is sufficiently established to derive a
    # transcript. An independently supplied attempt/explicit scope is not a host.
    calls = []
    original = s.derive_transcript
    def derive(session, env, **kwargs):
        calls.append(session)
        return original(session, env, **kwargs)
    monkeypatch.setattr(s, 'derive_transcript', derive)
    monkeypatch.setattr(s, 'detect_harness', lambda env: 'unknown')
    stamp = s.mint_context('test', payload=payload, env=env, cwd='/synthetic', when='W')
    assert calls == [None]
    assert stamp['host_session_id'] is None and stamp['transcript'] is None
    assert stamp['session'] == ctx['session_id']
