"""Model-specific policy and immutable regime identity (no service downloads)."""
import pytest
import embedding_profiles as profiles


def test_prompt_policy_is_model_specific():
    assert hasattr(profiles, 'resolve_profile'), 'explicit profiles are missing'
    known = profiles.resolve_profile('google/embeddinggemma-300m', 768, revision='abc')
    assert known.query_prefix == 'task: search result | query: '
    assert known.document_prefix == 'title: none | text: '
    custom = profiles.resolve_profile('operator/custom', 384, revision='xyz')
    assert (custom.query_prefix, custom.document_prefix, custom.dimension) == ('', '', 384)
    assert profiles.resolve_profile('operator/custom', 384, revision='xyz', query_prefix='Q: ').query_prefix == 'Q: '
    with pytest.raises(ValueError, match='dimension'):
        profiles.resolve_profile('operator/custom', 0, revision='xyz')


def test_profile_and_cache_identity_include_every_field_and_kind():
    from dataclasses import replace, FrozenInstanceError
    p = profiles.resolve_profile('operator/custom', 384, revision='xyz')
    for changes in ({'revision': 'other'}, {'dimension': 768}, {'query_prefix': 'Q'},
                    {'document_prefix': 'D'}, {'normalization': 'none'}, {'model_id': 'other'}):
        assert replace(p, **changes).fingerprint != p.fingerprint
    assert p.cache_key('query', 'same') != p.cache_key('document', 'same')
    assert p.cache_key('query', 'same') != p.cache_key('query', 'different')
    with pytest.raises(FrozenInstanceError):
        p.revision = 'changed'


def test_cross_model_or_dimension_transition_rejected_before_change():
    from dataclasses import replace
    p = profiles.resolve_profile('operator/custom', 384, revision='xyz')
    for changes in ({'revision': 'other'}, {'dimension': 768}, {'normalization': 'none'}, {'model_id': 'other'}):
        with pytest.raises(ValueError, match='same-model prompt migration only'):
            profiles.validate_transition(p, replace(p, **changes))
    profiles.validate_transition(p, replace(p, query_prefix='Q', document_prefix='D'))


def test_operator_rejects_ambiguous_routing_and_inherited_pg_redirects(monkeypatch):
    from scripts.reembed_documents import exclusive_dsn, pg_environment
    for dsn in ('service=other', 'host=one,two dbname=db user=role',
                'host=one hostaddr=127.0.0.2 dbname=db user=role',
                "host=one dbname='host=other' user=role"):
        with pytest.raises(ValueError,match='explicit|unsupported'):
            exclusive_dsn(dsn)
    monkeypatch.setenv('PGHOSTADDR','127.0.0.2')
    with pytest.raises(ValueError,match='inherited PG'):
        exclusive_dsn('host=one dbname=db user=role')
    env = pg_environment({'host':'one','user':'role','dbname':'db','password':'secret'})
    assert 'PGHOSTADDR' not in env
    assert env['PGHOST']=='one' and env['PGPASSWORD']=='secret' and env['PGDATABASE']=='db'


def test_profile_values_reject_invalid_normalization_and_malformed_fields():
    with pytest.raises(ValueError,match='normalization'):
        profiles.EmbeddingProfile('model','revision',384,normalization='auto')
    with pytest.raises(ValueError):
        profiles.EmbeddingProfile(42,'revision',384)


def test_cli_database_errors_do_not_disclose_credentials(monkeypatch,capsys):
    import psycopg2
    from scripts.reembed_documents import main
    monkeypatch.setenv('TEST_PRIVATE_DSN','host=example.invalid dbname=chosen user=role password=do-not-print-this')
    def unavailable(**kwargs):
        raise psycopg2.OperationalError('do-not-print-this from a driver diagnostic')
    monkeypatch.setattr(psycopg2,'connect',unavailable)
    assert main(['status','--dsn-env','TEST_PRIVATE_DSN']) == 1
    captured = capsys.readouterr()
    assert 'do-not-print-this' not in captured.err+captured.out
    assert 'Database/embedding operation failed' in captured.err
