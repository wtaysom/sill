# Disposable database tests

These tests never read the application's database configuration. Build the public
image and start an isolated test server (use an unused loopback port):

```sh
docker build -t sill-test-db backend
docker run -d --name sill_test_db -p 127.0.0.1:15434:5432 \
  -e POSTGRES_USER=sill_test -e POSTGRES_PASSWORD=fixture_only \
  -e POSTGRES_DB=sill_test_control sill-test-db
```

Wait for the container's `PostgreSQL init process complete` log before using it;
`pg_isready` alone can observe the temporary init server. The public image's
baseline seed may wait for its configured embeddings service. The subsequent
isolated databases created by `bootstrap_fixture.py` use deterministic local SQL
embeddings from the start and require no external embedding service.

```sh
export SILL_TEST_DSN='postgresql://sill_test:fixture_only@127.0.0.1:15434/sill_test_control'
export SILL_TEST_ALLOW_DATABASE=1
export SILL_TEST_CONTAINER=sill_test_db
python backend/tests/integration/bootstrap_fixture.py --name sill_test_contracts
export SILL_TEST_DSN='postgresql://sill_test:fixture_only@127.0.0.1:15434/sill_test_contracts'
python -m pytest backend/tests/integration backend/tests/test_migrations_files.py -q
```

No configured DSN means local integration tests skip. `SILL_TEST_REQUIRED=1`
turns missing DSN or container into a test failure for required CI jobs. A supplied
but unsafe/unavailable DSN always fails. Guards require a loopback host, explicit
non-production port (5432 and the desktop probe's 15432 are refused), user
`sill_test`, and database name beginning `sill_test_` or `sill_v03_test`.

Each ordinary test rolls its transaction back, including its temporary embedding
function replacement. Bootstrap/parity/concurrency tests create uniquely named
`sill_test_*` databases, commit only synthetic data, then drop those databases.
They never drop a pre-existing database or terminate someone else's connection.
The bootstrap can also leave a specifically named new database for inspection;
it refuses to replace an existing database. The server/container remains under
the caller's control.

The ordinary fixture substitutes the network-facing `public.get_embedding(text)`
body and, after migration 014, its `(text,text)` overload. Profile transition tests
use their own deterministic local HTTP server and retain the real SQL HTTP paths. Supersession, subtype, concept, graph and trigger paths run unchanged.
The namespace manifest records the actual public and AGE entry points before and
after migration; parity tests compare their bodies as well as callable signatures
and execute both legacy and new overloads under `ag_catalog,public`.
