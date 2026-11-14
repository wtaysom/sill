# Restoring a Sill SQL backup

Sill's upgrade backup is a gzip-compressed, plain `pg_dump` SQL file. Restore it
into a **new, isolated database**, verify it, and retain the archive and source
installation until recovery is accepted. Restoration does not upgrade the
backup's schema or invent provenance for historical records.

## AGE catalog boundary

With PostgreSQL 16 and Apache AGE 1.6.0, a logical dump can retain the source
namespace OID in `ag_graph.graphid` and `ag_label.graph`, although `namespace`
and label `relation` resolve correctly in the destination. `psql` can exit zero
while Cypher fails. See [Apache AGE issue 2503](https://github.com/apache/age/issues/2503).

The explicit [catalog step](../scripts/restore_age_catalog.sql) supports this
specific catalog layout and Sill's default `memory_graph`. It checks all graphs
in that database. It refuses other PostgreSQL/AGE versions, changed catalog/FK
layouts, invalid graph/label/sequence/inheritance mappings and OID collisions.
Stop on a refusal; retain the target and obtain a reviewed procedure for that
case. Do not remove guards, disable triggers, use `session_replication_role`, or
reconstruct the graph to make a check pass.

This is an operator-invoked post-restore step, never an installer migration or
an automatic repair of a running database. It updates only the two catalog OID
references. Label IDs, encoded vertex/edge IDs, properties and ordinary rows are
retained. It makes the existing validated FK temporarily deferrable, forces
its checks before restoring its original immediate/nondeferrable flags, and
commits atomically. Any SQL error rolls the transaction back, including FK flags.
A successful catalog step alone is **not** successful recovery.

## Restore and check

Use a separate PostgreSQL server/container with the matching PostgreSQL and
extension binaries (including AGE and pgvector), the archive's owner roles, no
application/worker connections, and no clients pointed at it. Do not use the
running source container. The commands below assume an already provisioned
isolated container and its superuser role. No host PostgreSQL installation is
needed. Run from the Sill checkout with Bash 3.2 or newer.

Choose the exact container after checking its image, Compose project, mounts
and published ports with `docker inspect`. Keep this destination isolated for
the whole procedure. The catalog step checks that no other target sessions are
present; this is an extra check, not a substitute for external isolation.

```bash
set -euo pipefail
umask 077
restore_container='YOUR_ISOLATED_RESTORE_CONTAINER'
restore_database='sill_restore_check'
restore_user='sill'
backup_archive='/absolute/path/to/your/backup.sql.gz'
evidence_dir='/absolute/path/to/new/private/restore-evidence'
mkdir "$evidence_dir"

docker inspect "$restore_container" > "$evidence_dir/container.json"
gzip -t "$backup_archive"
# Compare the archive's checksum with the one retained at backup creation.
# macOS: shasum -a 256 "$backup_archive"; Linux: sha256sum "$backup_archive"

# CREATE DATABASE fails if the destination already exists. Never drop/reuse it.
docker exec -i "$restore_container" psql -X -U "$restore_user" -d postgres \
  -v ON_ERROR_STOP=1 -v restore_database="$restore_database" <<'SQL'
SELECT format('CREATE DATABASE %I TEMPLATE template0', :'restore_database') \gexec
SQL

gzip -dc "$backup_archive" | docker exec -i "$restore_container" \
  psql -X -U "$restore_user" -d "$restore_database" \
  -v ON_ERROR_STOP=1 --single-transaction > "$evidence_dir/restore.log" 2>&1

# Preserve every data row/sequence outside the two remapped catalog tables.
docker exec "$restore_container" pg_dump -U "$restore_user" -d "$restore_database" \
  --data-only --exclude-table=ag_catalog.ag_graph --exclude-table=ag_catalog.ag_label \
  > "$evidence_dir/data-before.sql"

docker exec -i "$restore_container" psql -X -U "$restore_user" -d "$restore_database" \
  -v ON_ERROR_STOP=1 -v restore_database="$restore_database" -v isolated_restore=yes \
  < scripts/restore_age_catalog.sql > "$evidence_dir/catalog.log" 2>&1

# This MUST be a new psql process: AGE caches graph metadata per connection.
docker exec -i "$restore_container" psql -X -U "$restore_user" -d "$restore_database" \
  -v ON_ERROR_STOP=1 < scripts/verify_age_restore.sql \
  > "$evidence_dir/graph.log" 2>&1

docker exec "$restore_container" pg_dump -U "$restore_user" -d "$restore_database" \
  --data-only --exclude-table=ag_catalog.ag_graph --exclude-table=ag_catalog.ag_label \
  > "$evidence_dir/data-after.sql"
# pg_dump restrict tokens are randomized; remove only those lines for comparison.
sed '/^\\restrict /d; /^\\unrestrict /d' "$evidence_dir/data-before.sql" > "$evidence_dir/data-before-normalized.sql"
sed '/^\\restrict /d; /^\\unrestrict /d' "$evidence_dir/data-after.sql" > "$evidence_dir/data-after-normalized.sql"
cmp "$evidence_dir/data-before-normalized.sql" "$evidence_dir/data-after-normalized.sql"
```

The name must start with `sill_restore_` (the regression tests also reserve
`sill_test_restore...`). The exact name and `isolated_restore=yes` acknowledgement
are mandatory. Neither is inferred from `.env`, the default Sill instance, or a
connection's database name. Do not concatenate repair and verification into one
psql connection. Missing variables, SQL errors, unsupported layouts and failed
verification return nonzero; keep their logs and stop rather than reporting a
partial restore as passed. A verification failure after commit leaves the target
available for diagnosis; it does not undo the completed catalog step.

The verifier reads **every vertex and edge through Cypher** and compares their
IDs, properties and edge endpoints with the physical restored rows. It detects
untraversable/dangling edges as well as catalog errors. It uses a read-only,
repeatable-read transaction and a five-minute statement limit. It may require
substantial time and memory for a large graph; a timeout is an incomplete check,
not permission to skip verification.

## Accept recovery against the original backup

The before/after comparison proves the catalog step left noncatalog data intact;
it does not prove the archive was complete. Before using the restored instance:

- Compare the full restored schema with the schema captured at backup time,
  including functions, constraints, label tables and sequences. Compare against
  that historical version, before applying any newer migrations.
- Compare all ordinary tables with the backup-time inventory using complete
  rows/IDs and stable ordering, including subtype/source/vector data, goals,
  migration ledger and historical unknown/null provenance. Counts alone are
  insufficient. Preserve sequence values as well as rows.
- Check the known graph records and relationships from the backup-time evidence
  in a fresh connection. The supplied verifier establishes agreement with the
  restored physical rows; the source comparison establishes their identity.
- Retain the archive checksum, restore/correction/verification logs and comparison
  results. If any comparison fails, keep the isolated target for diagnosis and
  do not start applications against it.

After historical recovery passes, use the normal upgrade procedure on the
restored installation if needed, with its own backup and verification. This
runbook does not choose an automatic cutover, replace a live database, or delete
any source, backup or failed target.

Sili
