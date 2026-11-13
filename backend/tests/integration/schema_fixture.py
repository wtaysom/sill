"""Owned Docker initdb routes for the release gate; never uses application DSNs.

Run from pytest via SILL_SCHEMA_IMAGE and SILL_SCHEMA_HTTP_IMAGE. The current
image's extension binaries serve all routes; historical init SQL comes from
verified release commits, unchanged. This tests schema lineage, not old images.
"""
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / 'backend/tests/fixtures/function-contracts.json'
PROJECT = 'sill_v03_m7'
NAMES = ('fresh', 'from01', 'from02')


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True, **kwargs).stdout


def docker(*args, **kwargs):
    context = os.environ.get('SILL_SCHEMA_DOCKER_CONTEXT')
    return run(['docker', *(['--context', context] if context else []), *args], **kwargs)


def sql(container, text):
    return docker('exec', '-i', container, 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                  '-U', 'sill_test', '-d', 'sill_test_m7_contract', input=text)


def dump(container, raw_path=None):
    value = docker('exec', container, 'pg_dump', '-U', 'sill_test', '-d', 'sill_test_m7_contract',
                   '--schema-only', '--no-owner', '--no-privileges')
    if raw_path is not None:
        raw_path.write_text(value)
    # PG16 minor releases add random psql restrict tokens; these are transport
    # guards, not schema. Preserve every other byte, including comments/order.
    return '\n'.join(line for line in value.splitlines()
                     if not line.startswith(('\\restrict ', '\\unrestrict '))) + '\n'


def prepare(directory):
    manifest = json.loads(MANIFEST.read_text())
    repository = os.environ.get('SILL_SCHEMA_TAG_REPOSITORY', str(ROOT))
    for lane in NAMES:
        target = directory / lane
        target.mkdir(mode=0o755)
        if lane == 'fresh':
            shutil.copyfile(ROOT / 'backend/schema.sql', target / '000_schema.sql')
            for path in (ROOT / 'backend/migrations').glob('*.sql'):
                shutil.copyfile(path, target / path.name)
        else:
            tag = 'v0.1.0' if lane == 'from01' else 'v0.2.0'
            commit = run(['git', '-C', repository, 'rev-parse', tag + '^{commit}']).strip()
            if commit != manifest['release_tags'][tag]:
                raise ValueError('historical release identity mismatch: ' + tag)
            (target / ('init.sql' if lane == 'from01' else '000_schema.sql')).write_text(
                run(['git', '-C', repository, 'show', commit + ':backend/schema.sql']))
            if lane == 'from02':
                files = run(['git', '-C', repository, 'ls-tree', '--name-only', commit + ':backend/migrations']).splitlines()
                for name in files:
                    if name.endswith('.sql'):
                        (target / name).write_text(run(['git', '-C', repository, 'show', commit + ':backend/migrations/' + name]))
        for path in target.iterdir():
            path.chmod(0o644)
    server = directory / 'embedding_http.py'
    server.write_text('''from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        data = json.dumps({'embedding': [0.01] * 768}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
HTTPServer(('0.0.0.0', 80), Handler).serve_forever()
''')
    server.chmod(0o644)
    evidence = {'source_head': run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip(),
                'historical_tags': manifest['release_tags'],
                'images': {key: docker('image', 'inspect', '--format', '{{.Id}} {{.Architecture}}',
                                      os.environ[key]).strip()
                           for key in ('SILL_SCHEMA_IMAGE', 'SILL_SCHEMA_HTTP_IMAGE')},
                'init_sha256': {lane: {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((directory / lane).iterdir())} for lane in NAMES}}
    (directory / 'lineage.json').write_text(json.dumps(evidence, indent=2))
    services = {'embeddings': {
        'image': os.environ['SILL_SCHEMA_HTTP_IMAGE'],
        'container_name': 'sill_test_m7_embeddings',
        'entrypoint': ['python3', '/fixture/embedding_http.py'],
        'volumes': [str(server) + ':/fixture/embedding_http.py:ro'],
    }}
    for i, lane in enumerate(NAMES):
        services[lane] = {
            'image': os.environ['SILL_SCHEMA_IMAGE'],
            'container_name': 'sill_test_m7_' + lane,
            'environment': {'POSTGRES_USER': 'sill_test', 'POSTGRES_PASSWORD': 'fixture_only',
                            'POSTGRES_DB': 'sill_test_m7_contract'},
            'command': ['postgres', '-c', 'search_path=ag_catalog,public'],
            'extra_hosts': ['host.docker.internal:host-gateway'],
            'ports': ['127.0.0.1:' + str(15461 + i) + ':5432'],
            'volumes': [str(directory / lane) + ':/docker-entrypoint-initdb.d:ro',
                        lane + ':/var/lib/postgresql/data'],
            'depends_on': ['embeddings'],
        }
    compose = directory / 'compose.json'
    compose.write_text(json.dumps({'services': services, 'volumes': {lane: {} for lane in NAMES}}, indent=2))
    return compose


@contextmanager
def routes(directory):
    directory.chmod(0o755)
    compose = prepare(directory)
    for port in (15461, 15462, 15463):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    # Refuse a prior run's containers/volumes instead of adopting or deleting.
    for kind in ('container', 'volume', 'network'):
        if docker(kind, 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + PROJECT).strip():
            raise RuntimeError('pre-existing M7 project resources; operator must inspect them')
    command = ('compose', '-p', PROJECT, '-f', str(compose))
    try:
        docker(*command, 'up', '-d', '--pull', 'never')
        for lane in NAMES:
            container = 'sill_test_m7_' + lane
            deadline = time.monotonic() + 420
            while True:
                result = subprocess.run(['docker', *(['--context', os.environ['SILL_SCHEMA_DOCKER_CONTEXT']]
                    if os.environ.get('SILL_SCHEMA_DOCKER_CONTEXT') else []), 'logs', container],
                    text=True, capture_output=True, check=True)
                logs = result.stdout + result.stderr
                (directory / (lane + '-init.log')).write_text(logs)
                if 'PostgreSQL init process complete' in logs:
                    try:
                        sql(container, 'SELECT 1;')
                        break
                    except subprocess.CalledProcessError:
                        pass
                if time.monotonic() > deadline:
                    raise RuntimeError('initdb did not complete: ' + container + '\n' + logs[-6000:])
                time.sleep(1)
        yield {lane: {'container': 'sill_test_m7_' + lane,
                      'dsn': 'host=127.0.0.1 port=' + str(15461+i) +
                      ' user=sill_test password=fixture_only dbname=sill_test_m7_contract'}
               for i, lane in enumerate(NAMES)}
    finally:
        docker(*command, 'down', '--volumes')


def upgrade(container, directory):
    env = dict(os.environ)
    if env.get('SILL_SCHEMA_DOCKER_CONTEXT'):
        env['DOCKER_CONTEXT'] = env['SILL_SCHEMA_DOCKER_CONTEXT']
    import sys
    return run([sys.executable, str(ROOT / 'backend/scripts/migrate.py'), 'apply',
                '--container', container, '--user', 'sill_test', '--database', 'sill_test_m7_contract',
                '--migrations', str(ROOT / 'backend/migrations'),
                '--recovery-file', str(directory / (container + '-recovery.json'))], env=env)
