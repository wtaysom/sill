import session_identity as s


def test_unknown_identity_stays_unknown(tmp_path):
    (tmp_path / 'newest.jsonl').write_text('{"session_id":"unrelated","model":"model-7"}')
    ctx = s.mint_context('test', env={}, cwd=str(tmp_path), when='W')
    assert ctx['session'] is None and ctx['model'] is None and ctx['transcript'] is None
    ambiguous = s.session_context(env={'CODEX_THREAD_ID':'one', 'CLAUDE_CODE_SESSION_ID':'two'})
    assert ambiguous['host_session_id'] is None and ambiguous['session_id'] is None
    resolved = s.session_context({'session_id':'two'}, env={'CODEX_THREAD_ID':'one','CLAUDE_CODE_SESSION_ID':'two'})
    assert resolved['host_session_id'] == 'two'


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
