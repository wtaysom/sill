"""Explicit, resumable same-model prompt transitions; credentials come from an env var.

See docs/EMBEDDING-PROFILES.md. SQL installation alone never activates prompts.
"""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from psycopg2.extras import Json, RealDictCursor

from embedding_profiles import EmbeddingProfile, validate_transition

LOCK = 714014
OPERATION_LOCK = 714015
SOURCE_COLUMNS = {'memories': 'embedding', 'working_memory': 'embedding',
                  'boundaries': 'trigger_embedding', 'episodes': 'summary_embedding'}
_ALLOWED_DSN = {'host', 'port', 'dbname', 'user', 'password', 'sslmode', 'sslrootcert',
                'sslcert', 'sslkey', 'sslcrl', 'connect_timeout', 'application_name'}


def exclusive_dsn(dsn):
    """Reject libpq indirection/multiple targets before connecting or dumping."""
    if any(key.startswith('PG') and value for key,value in os.environ.items()):
        raise ValueError('Unset inherited PG settings: the selected DSN environment variable must exclusively define database routing/authentication')
    try:
        parts = parse_dsn(dsn)
    except psycopg2.Error:
        raise ValueError('Invalid database DSN in selected environment variable') from None
    if (set(parts) - _ALLOWED_DSN or not all(parts.get(k) for k in ('host','dbname','user'))
            or ',' in parts['host'] or ',' in parts.get('port','')
            or '=' in parts['dbname'] or parts['dbname'].startswith(('postgres://','postgresql://'))):
        raise ValueError('Use one explicit host/database/user DSN; service, hostaddr, options and multi-host routing are unsupported for verified backup')
    parts.setdefault('port','5432')
    parts.setdefault('connect_timeout','10')
    return parts


def pg_environment(parts):
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    env.update({{'dbname':'PGDATABASE','application_name':'PGAPPNAME'}.get(key,'PG'+key.upper()): value for key, value in parts.items()})
    # No inherited password/service file may select a different credential/route.
    env['PGPASSFILE'] = os.devnull
    return env


def _run(args, **kwargs):
    try:
        result = subprocess.run(args, timeout=600, stderr=subprocess.PIPE, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('PostgreSQL backup tool failed or timed out; check selected tools/transport and permissions') from None
    if result.returncode:
        raise ValueError('PostgreSQL backup tool failed; check selected tools/transport and permissions (diagnostics suppressed to protect credentials)')
    return result


def verified_backup(conn, parts, directory, container=None):
    """Archive, checksum, and pg_restore catalog verification (not a restore rehearsal)."""
    env = pg_environment(parts)
    if container:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', container):
            raise ValueError('Invalid explicit pg-tools container name')
        # No mutation or credential transfer to an unverified container. Cluster
        # system id AND database oid/name AND role must agree with actual DSN.
        identity_sql = "SELECT json_build_array(system_identifier::text,current_database(),(SELECT oid::text FROM pg_database WHERE datname=current_database()),current_user)::text FROM pg_control_system()"
        with conn.cursor() as cur:
            cur.execute(identity_sql)
            identity = cur.fetchone()[0]
        prefix = ['docker','exec',container]
        result = _run(prefix+['psql','-X','-qAt','-U',parts['user'],'-d',parts['dbname'],'-v','ON_ERROR_STOP=1','-c',identity_sql],
                      env=env,stdout=subprocess.PIPE)
        try:
            actual = json.loads(result.stdout)
        except (ValueError,TypeError):
            raise ValueError('Backup container identity could not be verified') from None
        if actual != json.loads(identity):
            raise ValueError('Backup container does not match selected DSN cluster/database/role')
        dump = prefix+['pg_dump','-U',parts['user'],'-d',parts['dbname'],'--format=custom']
        listing = ['docker','exec','-i',container,'pg_restore','--list']
    else:
        if not shutil.which('pg_dump') or not shutil.which('pg_restore'):
            raise ValueError('Optional re-embedding requires pg_dump and pg_restore on PATH, or explicit --pg-tools-container NAME with verified database identity')
        dump, listing = ['pg_dump','--format=custom'], ['pg_restore','--list']
    directory = Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix='sill-embedding-',dir=directory))
    archive = private/'database.dump'
    with archive.open('xb') as stream:
        os.chmod(archive,0o600)
        _run(dump,env=env,stdout=stream)
        stream.flush()
        os.fsync(stream.fileno())
    with archive.open('rb') as stream:
        result = _run(listing,env=env,stdin=stream,stdout=subprocess.PIPE)
    if not archive.stat().st_size or b'TOC Entries:' not in result.stdout:
        raise ValueError('Backup archive catalog verification failed; retained archive is not approved for prepare')
    checksum = hashlib.sha256()
    with archive.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    digest = checksum.hexdigest()
    metadata = {'path': str(archive),'sha256':digest,'bytes':archive.stat().st_size,
                'verification':'pg_restore --list; restore rehearsal not performed',
                'transport': 'explicit-container' if container else 'host'}
    manifest = private/'manifest.json'
    manifest.write_text(json.dumps(metadata,indent=2)+'\n')
    os.chmod(manifest,0o600)
    return metadata


class ProfileMigration:
    def __init__(self, dsn):
        self.parts = exclusive_dsn(dsn)

    @contextmanager
    def connection(self, *, operation=False):
        # Explicit kwargs eliminate inherited PG routing settings for connections.
        # libpq still accepts PGOPTIONS/etc from the process: empty options and
        # explicit sslmode defeat those supported connection defaults.
        args = dict(self.parts)
        args.setdefault('sslmode','prefer')
        args['options'] = ''
        args['passfile'] = os.devnull
        with closing(psycopg2.connect(**args)) as conn:
            if operation:
                with conn.cursor() as cur:
                    cur.execute('SELECT pg_try_advisory_lock(%s)',(OPERATION_LOCK,))
                    if not cur.fetchone()[0]:
                        raise ValueError('Another embedding operation is running; retry after it finishes')
                conn.commit()
            try:
                yield conn
            finally:
                conn.rollback()
                # Closing the dedicated connection releases the operation lock.

    @staticmethod
    def _latest(conn, required=True):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM public.embedding_operations ORDER BY created_at DESC,id DESC LIMIT 1')
            row = cur.fetchone()
        if required and row is None:
            raise ValueError('No prepared embedding operation; run prepare first')
        return row

    @staticmethod
    def _source_checks(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT relation_name,row_id FROM public.embedding_sources WHERE embedding IS NOT NULL AND (source_text IS NULL OR btrim(source_text)='') LIMIT 10")
            missing = cur.fetchall()
        if missing:
            raise ValueError('Missing embedding source text; populate boundaries.embedding_text / episodes.summary for these records before resume: '+str(missing))

    @staticmethod
    def _shape(conn, profile):
        with conn.cursor() as cur:
            cur.execute("SELECT c.relname,a.attname,a.atttypmod FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND a.atttypid='vector'::regtype AND a.attnum>0 AND NOT a.attisdropped AND c.relname=ANY(%s)",
                        (list(SOURCE_COLUMNS)+['memory_clusters','embedding_cache'],))
            columns = cur.fetchall()
            cur.execute('SELECT public.embedding_dimension()')
            dimension = cur.fetchone()[0]
        if len(columns)!=6 or dimension != profile.dimension or any(c[2]!=profile.dimension for c in columns):
            raise ValueError('Existing vector column dimensions do not match profile; v0.3 supports same-model prompt migration only; retain current profile or design a separate migration')

    @staticmethod
    def _verify_service(conn, profile):
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM public.embedding_config WHERE key='service_url'")
            url = urlsplit(cur.fetchone()[0])
            info_url = urlunsplit((url.scheme,url.netloc,url.path.rsplit('/',1)[0]+'/info','',''))
            cur.execute("SELECT http_set_curlopt('CURLOPT_TIMEOUT_MS','10000')")
            cur.execute('SELECT status,content FROM http_get(%s)',(info_url,))
            status, body = cur.fetchone()
            try:
                info = json.loads(body)
            except (TypeError,ValueError):
                raise ValueError('Configured embedding service /info did not return model identity') from None
            if not isinstance(info,dict) or status != 200 or info.get('model_id')!=profile.model_id or info.get('model_sha')!=profile.revision:
                raise ValueError('Configured live service model_id/model_sha differs from declared profile; retain current profile or configure the attested same model/revision')
            # Noncached probe checks the service vector shape without changing data.
            cur.execute("SELECT vector_dims(public._embedding_request('embedding profile verification','document',%s))",(Json(profile.to_dict()),))
            if cur.fetchone()[0] != profile.dimension:
                raise ValueError('Configured embedding service dimension differs from profile')

    @staticmethod
    def _active(conn):
        with conn.cursor() as cur:
            cur.execute('SELECT profile,provenance FROM public.embedding_regime WHERE singleton')
            return cur.fetchone()

    @staticmethod
    def _assert_active(conn, operation, rollback=False):
        active, _ = ProfileMigration._active(conn)
        expected = operation['target_profile'] if rollback else operation['source_profile']
        if active is None and not rollback and operation['provenance'].get('historical_identity')=='declared-unverified':
            return
        if active != expected:
            raise ValueError('Active profile changed since prepare; operation cannot safely continue')

    def prepare(self, target, *, source_attestation=None, backup_dir, pg_tools_container=None):
        with self.connection(operation=True) as conn:
            active, provenance = self._active(conn)
            if active is None:
                if source_attestation is None:
                    raise ValueError('Legacy model/revision is unknown: supply source_attestation from operator records; current service identity is not proof of historical vectors')
                source = source_attestation
                if source.query_prefix or source.document_prefix:
                    raise ValueError('Legacy source attestation must describe raw query/document prefixes')
                provenance = {'historical_identity':'declared-unverified','source_attestation':source.to_dict(),
                              'previous_provenance':provenance}
            else:
                source = EmbeddingProfile(**active)
                if source_attestation and source_attestation != source:
                    raise ValueError('Source attestation disagrees with active profile')
            validate_transition(source,target)
            if source == target:
                raise ValueError('Source and target profiles are identical; no transition needed')
            latest = self._latest(conn,False)
            if latest and latest['state']=='preparing':
                raise ValueError('An operation is already prepared; resume or rollback it first')
            self._shape(conn,source)
            self._source_checks(conn)
            self._verify_service(conn,source)
            conn.commit()
            backup = verified_backup(conn,self.parts,backup_dir,pg_tools_container)
            with conn.cursor() as cur:
                cur.execute("SELECT jsonb_object_agg(n.nspname||'.'||p.oid::regprocedure::text,pg_get_functiondef(p.oid)) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('public','ag_catalog') AND p.prokind='f' AND (p.prosrc ILIKE '%%embedding%%' OR p.prosrc ILIKE '%%neighborhood%%')")
                functions = cur.fetchone()[0]
                metadata_path = Path(backup['path']).parent/'profile-and-functions.json'
                with metadata_path.open('x') as metadata:
                    os.chmod(metadata_path,0o600)
                    json.dump({'source_profile':source.to_dict(),'target_profile':target.to_dict(),
                               'provenance':provenance,'function_snapshot':functions},metadata,indent=2)
                    metadata.flush()
                    os.fsync(metadata.fileno())
                backup['metadata_path'] = str(metadata_path)
                backup['metadata_sha256'] = hashlib.sha256(metadata_path.read_bytes()).hexdigest()
                cur.execute("INSERT INTO public.embedding_operations(source_profile,target_profile,provenance,backup,function_snapshot,state) VALUES(%s,%s,%s,%s,%s,'preparing') RETURNING id",
                            (Json(source.to_dict()),Json(target.to_dict()),Json(provenance),Json(backup),Json(functions)))
                operation_id = str(cur.fetchone()[0])
            conn.commit()
            return {'id':operation_id,'state':'preparing','backup':backup}

    @staticmethod
    def _pending(conn, operation, profile, limit=None, *, count=False):
        query = """SELECT s.relation_name,s.row_id,s.source_text,s.kind,
                    public.embedding_source_hash(s.source_text,s.kind) content_hash
                   FROM public.embedding_sources s LEFT JOIN public.embedding_stage t
                   ON t.operation_id=%s AND t.relation_name=s.relation_name AND t.row_id=s.row_id
                     AND t.profile_fingerprint=%s
                   WHERE t.row_id IS NULL OR t.content_hash<>public.embedding_source_hash(s.source_text,s.kind)
                   ORDER BY s.relation_name,s.row_id"""
        if count:
            query = 'SELECT count(*) AS remaining FROM ('+query+') pending'
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query+(' LIMIT %s' if limit else ''),
                        (operation['id'],profile.fingerprint)+((limit,) if limit else ()))
            return cur.fetchone()['remaining'] if count else cur.fetchall()

    @staticmethod
    def _capture_source(conn, operation):
        source = EmbeddingProfile(**operation['source_profile'])
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.embedding_stage(operation_id,relation_name,row_id,content_hash,profile_fingerprint,embedding)
                SELECT %s,relation_name,row_id,public.embedding_source_hash(source_text,kind),%s,embedding FROM public.embedding_sources
                ON CONFLICT(operation_id,relation_name,row_id,profile_fingerprint) DO UPDATE
                SET content_hash=excluded.content_hash,embedding=excluded.embedding
                WHERE embedding_stage.content_hash<>excluded.content_hash""",(operation['id'],source.fingerprint))

    def _resume(self, conn, operation, *, rollback=False, batch_size=100,max_batches=None):
        if batch_size<1 or max_batches is not None and max_batches<1:
            raise ValueError('batch size and max batches must be positive')
        profile = EmbeddingProfile(**operation['source_profile' if rollback else 'target_profile'])
        self._assert_active(conn,operation,rollback)
        self._source_checks(conn)
        self._shape(conn,profile)
        self._verify_service(conn,profile)
        if not rollback:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock_shared(%s)',(LOCK,))
            self._assert_active(conn,operation)
            self._capture_source(conn,operation)
        conn.commit()
        generated = batches = 0
        while max_batches is None or batches<max_batches:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock_shared(%s)',(LOCK,))
            self._assert_active(conn,operation,rollback)
            self._source_checks(conn)
            rows = self._pending(conn,operation,profile,batch_size)
            if not rows:
                conn.commit()
                break
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute("""INSERT INTO public.embedding_stage(operation_id,relation_name,row_id,content_hash,profile_fingerprint,embedding)
                        VALUES(%s,%s,%s,%s,%s,CASE WHEN %s IS NULL THEN NULL ELSE public._embedding_request(%s,%s,%s) END)
                        ON CONFLICT(operation_id,relation_name,row_id,profile_fingerprint) DO UPDATE
                        SET content_hash=excluded.content_hash,embedding=excluded.embedding""",
                        (operation['id'],row['relation_name'],row['row_id'],row['content_hash'],profile.fingerprint,
                         row['source_text'],row['source_text'],row['kind'],Json(profile.to_dict())))
            conn.commit() # the interruption boundary: each completed batch is durable
            generated += len(rows)
            batches += 1
        remaining = self._pending(conn,operation,profile,count=True)
        conn.commit()
        return {'generated':generated,'remaining':remaining,'batches':batches}

    def resume(self, *, batch_size=100,max_batches=None):
        with self.connection(operation=True) as conn:
            operation = self._latest(conn)
            if operation['state']!='preparing':
                raise ValueError('resume requires a preparing operation; use rollback to stage the old regime after activation')
            return self._resume(conn,operation,batch_size=batch_size,max_batches=max_batches)

    def _cutover(self, conn, operation, *, rollback=False):
        profile = EmbeddingProfile(**operation['source_profile' if rollback else 'target_profile'])
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='10s'")
            cur.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,))
        self._assert_active(conn,operation,rollback)
        self._shape(conn,profile)
        self._source_checks(conn)
        if self._pending(conn,operation,profile,1):
            raise ValueError('Staging does not cover current rows/content/profile; resume required before atomic cutover')
        if not rollback:
            self._capture_source(conn,operation)
        with conn.cursor() as cur:
            for relation,column in SOURCE_COLUMNS.items():
                cur.execute(sql.SQL('UPDATE public.{} r SET {}=s.embedding FROM public.embedding_stage s WHERE s.operation_id=%s AND s.relation_name=%s AND s.row_id=r.id AND s.profile_fingerprint=%s').format(sql.Identifier(relation),sql.Identifier(column)),
                            (operation['id'],relation,profile.fingerprint))
            # Same membership/weighting as recalculate_cluster_centroid; an empty
            # or no-longer-active cluster gets NULL, never an old-regime centroid.
            cur.execute("""UPDATE public.memory_clusters c SET centroid_embedding=(
                SELECT avg(m.embedding)::vector FROM public.memories m JOIN public.memory_cluster_members cm ON cm.memory_id=m.id
                WHERE cm.cluster_id=c.id AND m.status='active' AND cm.membership_strength>0.3),updated_at=now()""")
            # Query functions already filter is_stale. Direct current vector seeds
            # and FTS remain immediately available; maintenance rebuilds neighbors.
            cur.execute("UPDATE public.memory_neighborhoods SET neighbors='{}',is_stale=true")
            cur.execute('DELETE FROM public.activation_cache')
            cur.execute('DELETE FROM public.embedding_cache')
            cur.execute('UPDATE public.embedding_regime SET profile=%s,provenance=%s WHERE singleton',
                        (Json(profile.to_dict()),Json(operation['provenance'])))
            if rollback:
                cur.execute("UPDATE public.embedding_operations SET state='rolled_back',rolled_back_at=now() WHERE id=%s",(operation['id'],))
            else:
                cur.execute("UPDATE public.embedding_operations SET state='active',activated_at=now() WHERE id=%s",(operation['id'],))
        conn.commit()
        return {'state':'rolled_back' if rollback else 'active','fingerprint':profile.fingerprint}

    def activate(self):
        with self.connection(operation=True) as conn:
            operation = self._latest(conn)
            if operation['state']!='preparing':
                raise ValueError('activate requires a preparing operation')
            self._verify_service(conn,EmbeddingProfile(**operation['target_profile']))
            conn.commit()
            return self._cutover(conn,operation)

    def rollback(self, *, batch_size=100,max_batches=None):
        with self.connection(operation=True) as conn:
            operation = self._latest(conn)
            if operation['state']=='rolled_back':
                return {'state':'rolled_back'}
            if operation['state']=='preparing':
                with conn.cursor() as cur:
                    cur.execute("UPDATE public.embedding_operations SET state='rolled_back',rolled_back_at=now() WHERE id=%s",(operation['id'],))
                conn.commit()
                return {'state':'rolled_back','note':'Preparation cancelled; active profile/vectors unchanged'}
            staged = self._resume(conn,operation,rollback=True,batch_size=batch_size,max_batches=max_batches)
            if staged['remaining']:
                return dict(staged,state='active',note='Rollback staging incomplete; repeat rollback')
            return self._cutover(conn,operation,rollback=True)

    def status(self):
        with self.connection() as conn:
            active, provenance = self._active(conn)
            operation = self._latest(conn,False)
            if operation:
                operation = {k:v for k,v in operation.items() if k!='function_snapshot'}
                operation['id'] = str(operation['id'])
                with conn.cursor() as cur:
                    cur.execute('SELECT relation_name,profile_fingerprint,count(*) FROM public.embedding_stage WHERE operation_id=%s GROUP BY 1,2 ORDER BY 1,2',(operation['id'],))
                    operation['stage_counts'] = cur.fetchall()
            return {'active_profile':active,'active_fingerprint':EmbeddingProfile(**active).fingerprint if active else 'legacy-raw-unknown',
                    'provenance':provenance,'operation':operation}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('status','prepare','resume','activate','rollback'))
    parser.add_argument('--dsn-env',default='SILL_DATABASE_URL')
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--backup-dir',type=Path)
    parser.add_argument('--pg-tools-container')
    parser.add_argument('--batch-size',type=int,default=100)
    parser.add_argument('--max-batches',type=int)
    args = parser.parse_args(argv)
    try:
        if not os.environ.get(args.dsn_env):
            raise ValueError('Selected DSN environment variable is missing')
        operator = ProfileMigration(os.environ[args.dsn_env])
        if args.command=='prepare':
            if not args.profile or not args.backup_dir:
                raise ValueError('prepare requires --profile FILE and --backup-dir DIRECTORY')
            config = json.loads(args.profile.read_text())
            target = EmbeddingProfile(**config['target'])
            source = EmbeddingProfile(**config['source_attestation']) if config.get('source_attestation') else None
            result = operator.prepare(target,source_attestation=source,backup_dir=args.backup_dir,pg_tools_container=args.pg_tools_container)
        elif args.command in ('resume','rollback'):
            result = getattr(operator,args.command)(batch_size=args.batch_size,max_batches=args.max_batches)
        else:
            result = getattr(operator,args.command)()
        print(json.dumps(result,default=str,indent=2))
        return 0
    except (ValueError,KeyError,TypeError,OSError) as error:
        # Our ValueErrors are redacted and actionable. File/JSON/key diagnostics
        # can contain operator content and therefore are intentionally generic.
        print(str(error) if type(error) is ValueError else 'Invalid profile file or inaccessible operator path',file=__import__('sys').stderr)
        return 1
    except psycopg2.Error as error:
        print('Database/embedding operation failed; current transaction rolled back (SQLSTATE '+str(error.pgcode)+'). Correct service/database configuration and resume.',file=__import__('sys').stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
