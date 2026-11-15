# Explicit embedding prompt profiles

Migration 014 preserves every existing vector and leaves query/document prompts
raw. It records historical model identity as **unknown**. Installing SQL never
activates the embeddinggemma prompts or downloads a model. Operator-selected
models and dimensions continue raw retrieval at their existing dimensions.

The optional operator supports **same model, immutable revision, dimension and
normalization** transitions only. Cross-model/revision/dimension changes need a
separately designed migration. Keep the configured service pinned throughout an
operation; `/info` must expose `model_id` and `model_sha`, and `/embed` must accept
TEI-style `inputs` and `normalize`. The operator verifies identity and a noncached
vector-dimension probe before preparing, resuming, activating or rolling back.
Current service identity is evidence about the current service, not proof of the
model that generated historical vectors.

## Configure the operation

Use the installed `sill-memory` Python environment. Supply the database URL through
an environment variable such as `SILL_DATABASE_URL`, never an argument. Use one
explicit host, database and user. URI and libpq key/value DSNs are accepted;
service indirection, hostaddr, options, multiple hosts and inherited `PG*`
settings are refused. Unset inherited `PG*` variables before running the command.
Password-file fallback is disabled; put any required credential in the selected
DSN environment variable. No command prints that variable or service response
bodies. Keep shell tracing disabled when setting credentials.

Create a profile JSON file from operator records, for example:

```json
{
  "source_attestation": {
    "model_id": "google/embeddinggemma-300m",
    "revision": "REPLACE_WITH_ACTUAL_IMMUTABLE_MODEL_SHA",
    "dimension": 768,
    "query_prefix": "",
    "document_prefix": "",
    "normalization": "l2"
  },
  "target": {
    "model_id": "google/embeddinggemma-300m",
    "revision": "REPLACE_WITH_THE_SAME_ACTUAL_MODEL_SHA",
    "dimension": 768,
    "query_prefix": "task: search result | query: ",
    "document_prefix": "title: none | text: ",
    "normalization": "l2"
  }
}
```

An unknown legacy baseline requires explicit source attestation. This declaration
is retained as `declared-unverified` historical provenance in the operation and
backup metadata; it does not rewrite the unknown active metadata during prepare.
If the historical model cannot be established sufficiently to make this
attestation, keep the current raw profile. Do not substitute a candidate new
revision. A known active profile already supplies its source identity.

`resolve_profile(model_id, dimension, revision=...)` in `embedding_profiles.py`
selects the documented prefixes only for `google/embeddinggemma-300m`. Other
models stay raw unless their prefixes are explicitly supplied. Dimensions are
validated against the actual columns and service, never forced to 768. Profile
fingerprints include model, revision, dimension, both prefixes and normalization.

## Back up, prepare, resume and activate

Host `pg_dump` and `pg_restore` compatible with the server are prerequisites for
this optional operation. Alternatively select `--pg-tools-container NAME`
explicitly on `prepare`. There is no default container or Docker context change.
That transport compares cluster system identifier, database name/OID and role
against the actual selected DSN before dumping. The selected role must be allowed
to read `pg_control_system()` and authenticate locally inside that container;
otherwise use host tools. Credentials are never passed in process arguments or
into the container. Host-tool environment variables are built from the selected
DSN after removing inherited `PG*` settings.

```sh
python -m scripts.reembed_documents status --dsn-env SILL_DATABASE_URL
python -m scripts.reembed_documents prepare --dsn-env SILL_DATABASE_URL \
  --profile embedding-profile.json --backup-dir ./embedding-backups
python -m scripts.reembed_documents resume --dsn-env SILL_DATABASE_URL \
  --batch-size 100 --max-batches 1
python -m scripts.reembed_documents resume --dsn-env SILL_DATABASE_URL
python -m scripts.reembed_documents activate --dsn-env SILL_DATABASE_URL
```

Prepare creates a private directory (0700), custom-format archive (0600), SHA-256
checksum and verified `pg_restore --list` catalog. It also retains profile,
historical-provenance and SQL-function metadata. Catalog/checksum verification
is **not** a restore rehearsal. Rehearse restoration separately when operational
assurance requires it. Backup failures stop preparation; retained incomplete
files are never treated as approved backups. No backup or staging data is deleted
automatically.

Resume commits each completed batch. Interrupt it and run it again. Matching
relation/row/source-text/role/profile hashes are skipped; changed text is
regenerated; new and archived records are included; deleted or expired-and-deleted
records need no target vector. Reads and writes keep the old active profile.

Activation takes an exclusive transaction advisory barrier, waits for in-flight
embedding users/writers, and rechecks complete current row/hash coverage. Changed
or new rows produce `resume required`, with no partial vector/profile update.
The switch contains no HTTP work. Writers use a shared transaction barrier;
old repeatable-read snapshots receive a serialization failure and must retry.
Normal clients should keep transactions short. Custom SQL that combines a query
embedding and vector comparison in one statement should acquire
`pg_advisory_xact_lock_shared(714014)` in a preceding statement in the same
transaction, as the shipped legacy Python query handlers do. Shipped PL/pgSQL
retrieval functions obtain their embedding and then read vectors under the same
barrier. Do not modify profile/stage tables or run concurrent schema changes.

## Dependency inventory and source roles

| Stored vector/state | Source and transition behavior |
| --- | --- |
| `memories.embedding` | Exact `content`, document role, including archived rows |
| `working_memory.embedding` | Exact `content`, document role, including rows awaiting expiry cleanup |
| `boundaries.trigger_embedding` | Explicit `embedding_text`, document role |
| `episodes.summary_embedding` | Exact nonblank `summary`, document role; absent text/vector remains NULL |
| `memory_clusters.centroid_embedding` | Recomputed from active member target vectors, existing `membership_strength > 0.3` rule; empty clusters become NULL |
| `embedding_cache.embedding` | Cleared atomically; new cache keys include full profile fingerprint, kind and text |
| `memory_neighborhoods` | Neighbor weights cleared and marked stale atomically; existing maintenance rebuilds from active vectors |
| `activation_cache` | Cleared atomically |

Cluster membership and its metadata remain intact. Until neighborhoods rebuild,
retrieval falls back to direct current-profile vector seeds and full-text channels;
queries never compare old centroids or stale neighbor weights to new vectors.

Only the exact five known public seed boundary signatures receive their documented
original source strings automatically. A custom non-NULL boundary vector without
`embedding_text`, or an episode vector without summary text, blocks prepare/resume
with record IDs. Supply the actual original source; the description is not assumed
to be it. Editing stored source text directly also requires updating its active
vector, as before; migration coverage detects text changes but cannot infer a
vector's provenance from its numeric contents.

Query consumers are `fast_recall`, `fast_recall_hybrid`, `hybrid_recall`,
`hybrid_recall_perspective`, `search_similar_memories` (both overloads),
`search_working_memory`, `find_partial_activations`, `check_boundaries`, and the
three legacy Python cluster/procedure/strategy searches. Migration 014 patches
both public and preexisting AGE variants. One-argument `get_embedding(text)` and
`safe_get_embedding(text)` keep their identities and mean document. The two-
argument forms require an explicit `query` or `document`; neither has a default.
Permanent HTTP errors and malformed/dimension-invalid payloads fail immediately,
including through `safe_get_embedding`. Transport, 429 and server failures use a
bounded retry budget; no zero-vector substitution occurs.

## Roll back without deleting newer records

```sh
python -m scripts.reembed_documents rollback --dsn-env SILL_DATABASE_URL \
  --batch-size 100 --max-batches 1
python -m scripts.reembed_documents rollback --dsn-env SILL_DATABASE_URL
```

Rollback before activation cancels preparation and leaves the raw database
unchanged. After activation it stages the old profile for all current records.
Unchanged source hashes reuse retained original vectors. New/changed records get
old-profile embeddings from the same verified service; records, subtype data,
provenance, archives, and relationships stay in place. If old-profile generation
fails, the currently active regime remains usable. Repeated bounded rollback
calls resume old-profile staging before the atomic reverse cutover. Rollback
never restores the whole database, drops newer records, or replays old function
bodies. Old function definitions are retained as diagnostic metadata.

Precomputed ingest must provide `embedding_profile=<active fingerprint>` after
prompt activation, and vectors generated with that profile's document policy.
Omissions remain compatible only in raw mode; wrong fingerprints are rejected
before writes. `status` reports the current fingerprint.

## Explicit fresh installation

`./install.sh --embedding-profile embeddinggemma-search-v1` selects prompts for
one tested tuple only: `unsloth/embeddinggemma-300m`, revision
`bfa3c846ac738e62aa61806ef9112d34acb1dc5a`, dimension 768, normalization `l2`.
The installer supplies both prefixes explicitly, including their trailing spaces:
`task: search result | query: ` and `title: none | text: `. It does not treat the
unsloth model name as an automatic alias for Google's profile policy.

The default `--embedding-profile raw` never changes the active profile. Ordinary
upgrades and reruns also preserve the active profile. Custom models, revisions
and dimensions use the ordinary raw path; the explicit prompted choice refuses
incompatible configurations before starting the runtime.

A fresh database already generates raw boundary vectors during initialization.
Before starting that initialization, the prompted installer writes a private
`backend/.env.embedding-profile.json` receipt with the exact source attestation,
target fingerprint and creation nonce. It creates the previously absent local
PostgreSQL volume with that nonce as a label, verifies the label and records its
creation identity. Existing volumes without matching creation evidence are
refused, even if empty. Keep this receipt with the installation; copying an env
file alone does not transfer freshness evidence. No credential is stored in it.

Once every migration in the checkout's ledger is present and the services are
ready, the installer verifies the selected connection and mounted volume, binds
the receipt to the PostgreSQL system identifier/database name/OID, and uses the
same `ProfileMigration.prepare`, `resume` and `activate` operation described above.
Prepare uses the selected database container's verified PostgreSQL backup tools.
Backups stay under `backend/.env.embedding-profile-backups/`. The historical
attestation remains `declared-unverified`; installer evidence never promotes it to
verified historical model identity. Activation finishes before maintenance,
methodology seed import and client registration. No backup is restored or volume
deleted automatically on failure.

Rerun the same explicit command to resume an operation whose ID was saved. Its
source, target, database and volume must still match. An already active exact
fingerprint is a no-op on the same proven-created database. Keep the pinned
service unchanged and do not run independent embedding operations concurrently.

There is a deliberately conservative interruption boundary: if prepare was
started but no returned operation ID reached the receipt, the installer refuses
a second prepare. This includes failures before prepare committed. It never
adopts a latest operation just because its profiles look similar. Retain the
receipt, volume and backups. Using the installed Python environment and the
selected DSN environment variable, run `python -m scripts.reembed_documents status`
from `backend/`. Inspect the operation ID, source/target profiles, provenance and
backup against the receipt. If an operation was committed and you establish that
it is the intended one, explicitly run `resume` and `activate` as shown above,
then rerun the installer; it recognizes the exact active fingerprint without
claiming ownership of that operation. If no operation was committed, or another
operation intervened, stop for operator reconciliation rather than deleting the
receipt to bypass the guard. The raw store remains usable until activation;
`./install.sh --embedding-profile raw` does not perform a transition.
