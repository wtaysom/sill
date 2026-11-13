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

## Release schema contract (actual Docker initdb)

The required CI job also runs `test_schema_contract.py`. It creates only the
`sill_v03_m7_<nonce>` Compose project with matching
`sill_test_m7_<nonce>_*` containers, refuses pre-existing exact container/network
names regardless of labels, and checks loopback ports 15461–15463 are unused.
Database volumes are anonymous: there are no named-volume references to adopt.
Before startup `resources.json` records the concrete manifest. `ownership.json`
then records verified container/network IDs, nonce labels and actual container
Mounts. Cleanup checks that ownership and identities still match, removes only
those immutable container IDs with `docker container rm -f -v` (including their
anonymous volumes), and removes the verified network by ID. It never runs
Compose down or deletes a volume by name. Missing, ambiguous or changed ownership
preserves resources and fails for operator inspection. It does not connect to
the ordinary `SILL_TEST_DSN` fixture.

```sh
docker build -t sill-test-db backend
docker pull python:3.12-slim
SILL_SCHEMA_REQUIRED=1 \
SILL_SCHEMA_IMAGE=sill-test-db \
SILL_SCHEMA_HTTP_IMAGE=python:3.12-slim \
python -m pytest backend/tests/integration/test_schema_contract.py -v \
  --basetemp=/tmp/sill-schema-evidence
```

The repository must contain the annotated `v0.1.0` and `v0.2.0` tags. The helper
verifies their peeled commit IDs against `fixtures/function-contracts.json`.
`SILL_SCHEMA_TAG_REPOSITORY` can select a separate local tag repository;
`SILL_SCHEMA_DOCKER_CONTEXT` selects an explicit Docker context when needed.
There is no download or model inference in the HTTP fixture. All SQL function
bodies remain real, including both embedding overloads. This proves schema
lineage against current extension binaries, not historical container runtime
compatibility or real-model retrieval quality.

The fixture retains exact initialization logs, raw schema dumps, callable bodies,
source/image identities and SQL hashes in the evidence directory. Comparison
removes only pg_dump's randomized `\restrict`/`\unrestrict` transport tokens.
The frozen baseline logs its known generated `goals.dedupe_key` UPDATE error
(v0.1 line 7624; current/v0.2 line 7628) and continues because that section has
`ON_ERROR_STOP` disabled. A separate assertion rejects any other error; these are
not clean initialization logs. Migration 012 fixes the independent legacy goal
constructor enum cast, and the callable tests exercise first goal creation.

Actual v0.1 init has no migration ledger; v0.2 has `ag_catalog.schema_migrations`
001–007. The transaction-aware runner upgrades these without creating a second
ledger. Fresh and both upgraded routes must contain exactly 001–015 in the same
namespace, identical schema definitions and affected callable contracts, and
preserve synthetic identity/charter references, historical data and RRF scores.
Missing image configuration skips an explicitly local run. With
`SILL_SCHEMA_REQUIRED=1`, absent configuration, unavailable images, missing tags,
failed initdb or failed connections are errors, never a green skip.

The independent `clean-wheel` CI job builds and installs the wheel in a new venv,
then runs `tests/wheel_smoke.py` from outside the checkout with Python `-I`.
It verifies installed imports and MCP tool discovery, including optional Chorus
stage absence. This is registry discovery; live MCP transport/client rehearsal
remains a separate release check.
