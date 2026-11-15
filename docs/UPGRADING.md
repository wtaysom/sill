# Upgrading Sill

The [README](../README.md#upgrading) has the short version. This guide
covers every step in order, what to do when the upgrade stops, and three
things the upgrade decides for you: it keeps your database image, it closes
a database port that older versions left open, and it records which
embedding model your memories were indexed with.

## Before you start

Pause any beat schedule and close the connected clients, including any
tunnel process for this instance. Run everything from your existing
checkout:

```bash
git pull
./upgrade.sh --dry-run
./upgrade.sh
```

## Flags

| Flag | What it does |
|---|---|
| `--dry-run` | Runs every check and prints the plan. Changes nothing. |
| `--client TARGET` | Refreshes the entry for `claude-code`, `codex` or `claude-desktop`. Repeat it for more than one. Without it, the upgrade refreshes the Claude Code and Codex CLIs it finds. |
| `--adopt-legacy` | Replaces a bare `sill-mcp` client entry left by v0.2. |
| `--archive-duplicates` | After the backup, archives the newer copy of each memory that repeats the same content from the same speaker. |
| `--hooks-for PATH` | Also refreshes that project's hook commands. |
| `--hooks-only` | With `--hooks-for`, refreshes hooks and skips the rest. |
| `--force-hooks` | Replaces a Sill hook command that has been edited. |
| `--no-verify` | Skips the extra smoke checks at the end. The upgrade still rebuilds, restarts and checks versions. |
| `--schema-only` | Applies migrations to a database and nothing else. For test fixtures. |

## What the dry run checks

The dry run is the same set of checks the upgrade begins with. None of
them changes anything.

- Python is 3.10 or later.
- The database container is running, belongs to this checkout's Compose
  project, and is on the data volume the settings name.
- The migration history in the database is one this checkout can continue
  from.
- No speaker holds the same live content twice. If one does, the check
  lists each pair and stops; see `--archive-duplicates`.
- The running embedding service can be identified; see
  [the embedding model](#the-embedding-model-is-identified-not-changed).
- Whether the database container still publishes its port on every
  network interface; see
  [Closing the database port](#closing-the-database-port).
- Each selected client entry can be updated. A bare `sill-mcp` entry stops
  the check until you add `--adopt-legacy`.

It ends by printing how many migrations are pending.

## What the upgrade does, in order

1. Runs the checks above.
2. Creates an empty private `backend/.env` if there is none. An empty file
   means the same defaults as no file.
3. Writes a backup to `backups/preupgrade-<timestamp>-<id>.sql.gz`, private
   to you, and checks that the archive is complete.
4. Records the embedding model, revision and dimension in `backend/.env`.
5. Closes the database port if it is open, as described below.
6. Builds the new worker image and reinstalls the package.
7. Stops the worker and applies the pending migrations. While they run,
   the database refuses new connections. With `--archive-duplicates`, the
   reviewed copies are archived in this step.
8. Starts the embedding service and RabbitMQ with this version's settings,
   starts a new worker, and waits for each to be healthy.
9. Checks the running version, updates the selected client entries,
   refreshes hooks if you asked, and runs `./verify.sh`.

The last line names the version, the number of migrations applied and the
backup file. Restart your clients, repeat the
[memory check](../README.md#your-first-memory), then resume any schedule.

## If it stops

Every stop prints what it found and, once a backup exists, the path of the
backup.

- **Before the backup.** Nothing has changed. Fix what the message names
  and run it again.
- **During migrations.** The upgrade stops if other clients are still
  connected. Read the last successful migration and the backup path in the
  output. If it reports that connections are still refused, run the
  recovery command it prints, from the same checkout. That command checks
  the selected database and allows connections again. Then address the
  reported failure and retry.
- **While closing the database port.** See that section below.

A backup can be restored into an empty database after you have confirmed
the target and kept the original data. [Restoring a backup](RESTORE.md)
has the steps.

## Coming from v0.2

The [README](../README.md#coming-from-v02) lists what the dry run will
raise. Two things about a v0.2 install are worth knowing as well:

- Its maintenance worker may have been stopped since the day it was
  installed. v0.2 started the worker before the database had finished
  loading its schema, the worker exited, and nothing restarted it. The
  upgrade starts a new one.
- v0.2.0 can no longer be installed from its tag, because the Debian 11
  package sources its database image used have been retired. Your existing
  image is unaffected, and the upgrade keeps it.

## Your database image is kept

New installs build PostgreSQL 16 on Debian Bookworm. An upgrade, or a
rerun of the installer, keeps the database image and data volume you
already have. It checks their identity and their Compose and volume
ownership before it changes anything, and again at the end. It refuses a
database that is missing, replaced or stopped; start a stopped database
with its existing configuration and retry.

The reason is collation. Moving an existing database to a newer operating
system image can change how text sorts underneath indexes that already
exist. That move needs a separate, backed-up migration, which this command
does not perform.

Two consequences:

- The existing image receives no PostgreSQL or operating system security
  updates through this upgrade. Debian Bullseye's PostgreSQL packages have
  reached end of life.
- Do not use `docker compose build` or `docker compose up` yourself to
  upgrade an existing database. Those commands bypass these checks and can
  recreate the database on a newer image while keeping the old data.

`reset.sh` deletes the database and starts a fresh one. It is neither an
upgrade nor a migration.

## Closing the database port

v0.2 published PostgreSQL on every network interface, with trust
authentication and no restart policy. On a shared network, another machine
could read and change the store without a password, and the database did
not come back after a reboot. This version publishes on `127.0.0.1` and
restarts unless stopped. A port binding cannot be changed on an existing
container, so the upgrade replaces the container and nothing else.

The dry run says it will. After the backup, and before the package, the
images or the schema change, the upgrade:

1. stops the worker and the database;
2. copies the database's data volume, using the database's own image, to a
   second volume named `<volume>_before_<backup timestamp>`;
3. starts a new container from the same image on the same volume, with
   this checkout's port and restart settings, and waits for it to be
   healthy.

It does this only when the image Compose would start is the one the
database already runs on, and it checks that again after the copy.

### If this step fails

No migration has run, and the package and worker image are still the old
ones. What the upgrade does next depends on how far it got:

- If the database could not be stopped or copied, or Compose did not
  replace the container, the upgrade starts the old container again,
  confirms it is running and stops.
- If a new container was created but did not come up healthy with the
  planned settings, the upgrade stops that container. It then tells you
  that the database is stopped, where the copy is, how to see what the
  container logged and the command that starts it.
- If you interrupt it while the database is stopped for the copy, it
  starts the old container again on the way out.

A copy that did not finish is removed. If it cannot be removed, the
message names it and says not to rely on it.

### The second volume

The second volume is the complete database as it was before the upgrade,
at the old schema. The upgrade never reads it again. Keep it until you
trust the upgraded store, then remove it:

```bash
docker volume ls --filter name=_before_
docker volume rm <that volume>
```

Going back to it is not automated.

### When the container is left as it is

In two cases the upgrade sets the restart policy in place and does not
replace the container:

- You want other machines to reach the database. Set
  `SILL_BIND_ADDR=0.0.0.0` in `backend/.env` before upgrading.
- The upgrade cannot prove that the image Compose would start is the one
  the database runs on, for example after a direct `docker compose build`.
  It warns and names the image tag. To close the port, point that tag back
  at the running image and upgrade again:

  ```bash
  docker tag "$(docker inspect --format '{{.Image}}' sill_db)" <tag from the warning>
  ./upgrade.sh
  ```

A rerun of the installer does not close the port. It says that the port is
open and that `upgrade.sh` closes it. A container published on one
specific address other than `127.0.0.1` is left as it is without a
warning.

## The embedding model is identified, not changed

Memories are indexed by an embedding model, and vectors from one model
cannot be compared with vectors from another. Before it restarts any
service, the upgrade works out which model, revision and dimension the
running service uses and checks them against the stored vectors and any
active profile. A rerun of the installer does the same.

- When `backend/.env` omits the model, revision or dimension, the upgrade
  adds them after the backup, so that later runs and direct Compose use
  agree. It adds only what is missing. Values you set and the rest of the
  file stay as they are, and a private `.env.embedding-identity.bak-*`
  keeps the previous file. A dry run only validates.
- An omitted revision is recorded as the snapshot the running service
  loaded. The service reports that hash only when it was started with one,
  and a v0.2 stack was not. In that case the upgrade reads the hash from
  the service's model cache, which records the one snapshot that `main`
  resolved to when the model was downloaded.
- If the cache holds several snapshots, the message lists them and asks
  you to set `EMBEDDING_REVISION` to the one your vectors came from.
- A symbolic revision that you set yourself, such as a branch name, is
  left untouched, but the restart refuses it: a matching label cannot
  prove that the same snapshot was loaded. To keep that embedding space,
  set `EMBEDDING_REVISION` to the hash the message reports and remove any
  conflicting shell override.
- If the identity cannot be proved, or a value you set conflicts with the
  running service, start the existing service with its previous
  configuration and resolve the mismatch the message names. The upgrade
  will not replace the service on a guess.

This records the identity of the service that is running now. It does not
vouch for vectors written under some earlier configuration, and it does
not activate a profile. Changing the embedding space is a migration of its
own; see [embedding profiles](EMBEDDING-PROFILES.md).

## Hooks

`./upgrade.sh --hooks-for /path/to/project` refreshes one project's hook
commands, and `--hooks-only` does that alone. The
[README](../README.md#hooks-for-claude-code-and-codex) has the details.
