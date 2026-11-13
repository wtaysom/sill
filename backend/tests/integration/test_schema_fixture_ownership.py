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
                        'Source': '/docker/volumes/anonymous-data-id/_data',
                        'Destination': '/var/lib/postgresql/data', 'Driver': 'local',
                        'Mode': 'z', 'RW': True, 'Propagation': ''},
                       {'Type': 'bind', 'Source': '/fixture/init',
                        'Destination': '/docker-entrypoint-initdb.d',
                        'Mode': 'ro', 'RW': False, 'Propagation': 'rprivate'}]},
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


@pytest.mark.parametrize('reverse_initial', [False, True])
def test_mount_order_only_permutation_keeps_same_immutable_cleanup(monkeypatch, reverse_initial):
    resources, data, calls = installed_resources(monkeypatch)
    initial = fixture.inspect_owned(resources, NONCE)
    name = resources['container'][0]
    if reverse_initial:
        initial[name]['mounts'].reverse()
    else:
        data[name]['Mounts'].reverse()
    saved = json.dumps(initial, sort_keys=True)
    current = json.dumps(data, sort_keys=True)
    fixture.cleanup_owned(resources, NONCE, initial)
    assert [call for call in calls if 'rm' in call] == [
        ('container','rm','-f','-v','container-id-owned'), ('network','rm','network-id-owned')]
    # The saved/raw inspection evidence must remain untouched.
    assert json.dumps(initial, sort_keys=True) == saved
    assert json.dumps(data, sort_keys=True) == current


@pytest.mark.parametrize('index,field,value', [
    (0, 'Type', 'bind'), (0, 'Name', 'different-volume'),
    (0, 'Source', '/different/volume'), (0, 'Destination', '/different/data'),
    (0, 'Driver', 'different-driver'), (0, 'Mode', 'ro'), (0, 'RW', False),
    (0, 'Propagation', 'shared'), (0, 'FutureField', 'must-not-be-dropped'),
    (1, 'Type', 'volume'), (1, 'Source', '/different/init'),
    (1, 'Destination', '/different/entrypoint'), (1, 'Mode', 'rw'),
    (1, 'RW', True), (1, 'Propagation', 'shared'),
])
def test_mount_field_change_still_refuses_cleanup(monkeypatch, index, field, value):
    resources, data, calls = installed_resources(monkeypatch)
    initial = fixture.inspect_owned(resources, NONCE)
    data[resources['container'][0]]['Mounts'][index][field] = value
    with pytest.raises(RuntimeError, match='identity changed'):
        fixture.cleanup_owned(resources, NONCE, initial)
    assert not any('rm' in call for call in calls)


@pytest.mark.parametrize('change', ['remove', 'duplicate'])
def test_mount_multiset_cardinality_change_still_refuses_cleanup(monkeypatch, change):
    resources, data, calls = installed_resources(monkeypatch)
    initial = fixture.inspect_owned(resources, NONCE)
    mounts = data[resources['container'][0]]['Mounts']
    if change == 'remove':
        mounts.pop()
    else:
        mounts.append(dict(mounts[0]))
    with pytest.raises(RuntimeError, match='identity changed'):
        fixture.cleanup_owned(resources, NONCE, initial)
    assert not any('rm' in call for call in calls)
