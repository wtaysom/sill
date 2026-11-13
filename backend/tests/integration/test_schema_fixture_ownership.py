"""Ownership regression tests use Docker doubles, never a real daemon."""
import json
from types import SimpleNamespace

import pytest

import schema_fixture as fixture

NONCE = '0123456789ab'
NAMES = fixture.resource_names(NONCE)


@pytest.mark.parametrize('kind', ['container', 'network'])
def test_unlabeled_exact_name_collision_never_starts_or_tears_down(monkeypatch, tmp_path, kind):
    calls = []
    name = NAMES[kind][0]
    def docker(*args):
        calls.append(args)
        assert args[1] == 'ls', 'refusal reached startup or teardown'
        assert '--filter' not in args, 'labels must not hide exact-name collisions'
        return name + '\n' if args[0] == kind else ''
    monkeypatch.setattr(fixture, 'docker', docker)
    monkeypatch.setattr(fixture, 'uuid4', lambda: SimpleNamespace(hex=NONCE))
    monkeypatch.setattr(fixture, 'prepare', lambda directory, nonce: directory / 'compose.json')
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, address): pass
    monkeypatch.setattr(fixture.socket, 'socket', Probe)
    with pytest.raises(RuntimeError, match='pre-existing'):
        with fixture.routes(tmp_path):
            pytest.fail('foreign resource allowed startup')
    assert not any('compose' in call or 'rm' in call for call in calls)


def test_generated_compose_cannot_reference_foreign_fixed_named_volume(monkeypatch, tmp_path):
    manifest = json.loads(fixture.MANIFEST.read_text())
    foreign_volume = 'sill_v03_m7_fresh'
    calls = []
    def docker(*args):
        calls.append(args)
        if args[:2] == ('volume', 'ls'):
            return foreign_volume  # Existing volume without any ownership labels.
        assert args[:2] == ('image', 'inspect')
        return 'sha256:fixture arm64'
    def git(args, **kwargs):
        if args[-1] == 'HEAD': return 'fixture-head'
        if args[-1].endswith('^{commit}'):
            return manifest['release_tags'][args[-1].removesuffix('^{commit}')]
        if 'ls-tree' in args: return ''
        if 'show' in args: return 'SELECT 1;\n'
        raise AssertionError(args)
    monkeypatch.setattr(fixture, 'docker', docker)
    monkeypatch.setattr(fixture, 'run', git)
    monkeypatch.setenv('SILL_SCHEMA_IMAGE', 'fixture-db')
    monkeypatch.setenv('SILL_SCHEMA_HTTP_IMAGE', 'fixture-http')
    config = json.loads(fixture.prepare(tmp_path, NONCE).read_text())
    assert 'volumes' not in config
    assert foreign_volume not in json.dumps(config)
    for lane in fixture.NAMES:
        service = config['services'][lane]
        assert service['volumes'][-1] == '/var/lib/postgresql/data'
        assert service['labels'][fixture.OWNER_LABEL] == NONCE
        assert service['container_name'] in NAMES['container']
    assert config['networks']['default']['labels'][fixture.OWNER_LABEL] == NONCE
    assert not any('rm' in call for call in calls)


def installed_resources(monkeypatch):
    resources = {'container': [NAMES['container'][0]], 'network': NAMES['network']}
    data = {
        resources['container'][0]: {'Id': 'container-id-owned',
            'Config': {'Labels': {fixture.OWNER_LABEL: NONCE}},
            'Mounts': [{'Type': 'volume', 'Name': 'anonymous-data-id',
                        'Destination': '/var/lib/postgresql/data'}]},
        resources['network'][0]: {'Id': 'network-id-owned', 'Labels': {fixture.OWNER_LABEL: NONCE}},
    }
    calls = []
    def docker(*args):
        calls.append(args)
        kind, operation = args[:2]
        if operation == 'ls': return '\n'.join(n for n in resources[kind] if n in data)
        if operation == 'inspect': return json.dumps([data[args[2]]])
        assert operation == 'rm'
        # IDs are the only accepted deletion targets. No volume rm/down/name rm.
        assert kind in ('container','network') and args[-1] == kind + '-id-owned'
        return ''
    monkeypatch.setattr(fixture, 'docker', docker)
    return resources, data, calls


@pytest.mark.parametrize('change', ['unlabeled', 'foreign_nonce', 'replaced_id', 'missing'])
def test_changed_or_unowned_resources_block_all_cleanup(monkeypatch, change):
    resources, data, calls = installed_resources(monkeypatch)
    initial = fixture.inspect_owned(resources, NONCE)
    name = resources['container'][0]
    if change == 'unlabeled': data[name]['Config']['Labels'] = {}
    elif change == 'foreign_nonce': data[name]['Config']['Labels'][fixture.OWNER_LABEL] = 'other-run'
    elif change == 'replaced_id': data[name]['Id'] = 'replacement-id'
    else: del data[name]
    with pytest.raises(RuntimeError, match='preserving resources'):
        fixture.cleanup_owned(resources, NONCE, initial)
    assert not any('rm' in call for call in calls)


def test_cleanup_uses_owned_immutable_ids_and_container_anonymous_volumes(monkeypatch):
    resources, data, calls = installed_resources(monkeypatch)
    initial = fixture.inspect_owned(resources, NONCE)
    assert initial[resources['container'][0]]['mounts'][0]['Name'] == 'anonymous-data-id'
    fixture.cleanup_owned(resources, NONCE, initial)
    assert [call for call in calls if 'rm' in call] == [
        ('container','rm','-f','-v','container-id-owned'), ('network','rm','network-id-owned')]
    assert not any(call[0] == 'volume' or 'down' in call for call in calls)


def test_failed_start_cleanup_refuses_a_raced_foreign_resource(monkeypatch):
    resources, data, calls = installed_resources(monkeypatch)
    data[resources['container'][0]]['Config']['Labels'] = {}
    with pytest.raises(RuntimeError, match='unowned'):
        fixture.cleanup_owned(resources, NONCE, initial=None)
    assert not any('rm' in call for call in calls)


def test_ambiguous_inspection_preserves_all_resources(monkeypatch):
    resources, data, calls = installed_resources(monkeypatch)
    initial = fixture.inspect_owned(resources, NONCE)
    original = fixture.docker
    def ambiguous(*args):
        if args[:2] == ('container', 'inspect'):
            return json.dumps([data[args[2]], data[args[2]]])
        return original(*args)
    monkeypatch.setattr(fixture, 'docker', ambiguous)
    with pytest.raises(RuntimeError, match='ambiguous'):
        fixture.cleanup_owned(resources, NONCE, initial)
    assert not any('rm' in call for call in calls)
