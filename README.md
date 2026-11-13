# Sill

Sill gives AI assistants a memory store they can use across sessions and
clients. It runs Postgres with pgvector and Apache AGE locally and exposes
memory tools through MCP. Optional hooks add automatic recall and checks in
Claude Code and Codex CLI.

This checkout is the v0.3.0 release candidate.
[Release rehearsal](docs/RELEASE-REHEARSAL.md)

## Quick start

Install Git, Python 3.10 or later, and a Docker engine with Compose.
Start Docker and sign in to the client you want to use. If you already
use Sill, follow [Upgrading](#upgrading).

For Claude Desktop on macOS, run the commands below. On macOS or Linux,
use `--client claude-code` for Claude Code or `--client codex` for Codex CLI
in the install command.

```bash
git clone https://github.com/wtaysom/sill.git
cd sill
./install.sh --client claude-desktop
./verify.sh
```

Use `--client codex` for a local Codex desktop session too. Repeat the
flag to connect more than one client. If you omit `--client`, the
installer connects the Claude Code and Codex CLIs it detects. See the
[client setup guide](docs/desktop-clients.md) for registries, restart
and permission steps, and client verification status.

**Restart your client**, then use the
[first-use runbook](docs/onboarding/README.md) to remember a synthetic
preference and recall the same UUID from a fresh session. The optional
identity, charter, backfill, and scheduling guides follow that exercise.

## Install scope

Hooks can be wired at two scopes — a real tradeoff, not a style choice:

```bash
./install.sh --scope project --hooks-for /path/to/project   # default
./install.sh --scope home
```

- **`project`** (default): today's behavior. Hooks go into that one
  project's `.claude/settings.local.json` and `.codex/hooks.json`.
  Narrower blast radius, no cross-project mixing — but only wired
  projects get recall/guards, and each new project needs its own
  `--hooks-for` run.
- **`home`**: hooks go into `~/.claude/settings.json` and
  `~/.codex/hooks.json` (Claude Code's and Codex's own user-scope
  locations), plus an ambient instructions file
  (`~/.claude/CLAUDE.md`, from `plugin/claude.home.md.template`,
  merged in idempotently — your own content is never touched) so every
  session in every directory carries the Sill background. In exchange:
  every prompt in every project pays the recall hook's latency, and
  any project's work can reach the one store.

`--scope home` and `--hooks-for <path>` are additive — you can register
home-scope hooks and also give one project its own project-scoped
entries in the same run. An invalid `--scope` value exits non-zero
naming the valid ones. Full flag reference: `./install.sh --help`.

## Upgrading

Pause any beat schedule and close connected clients, including any tunnel
process for this instance. From the existing checkout, update the source
and preview the upgrade before applying it.

```bash
git pull
./upgrade.sh --dry-run
./upgrade.sh
```

`upgrade.sh` checks the selected instance and writes a validated private
archive under `backups/preupgrade-*.sql.gz` before changing the runtime.
It rebuilds and reinstalls the runtime, applies pending migrations, and
verifies the running version. The migration temporarily fences new
connections and stops if other clients are still connected. If it stops,
read the last successful migration stamp and the backup path in the
output. If it reports a remaining fence, run the recovery command it
prints from the same checkout. That command checks the selected database
and restores its connection allowance. Retry after addressing the
reported failure. A backup can be used for recovery into an empty
database after confirming the target and keeping the original data.

Upgrade and installer reruns preflight the existing embedding service, stored
vector dimension and any active profile before restarting services. When the
instance dotenv omits the model, revision or dimension, a successful validation
pins those fields in `backend/.env` for later invocations and direct Compose use.
Only omitted fields are appended; explicit values and unrelated bytes remain
unchanged. A private `.env.embedding-identity.bak-*` preserves the prior file.
Upgrade writes this pin after the database backup; `--dry-run` only validates.
If identity cannot be proved or an explicit value conflicts, start the existing
service with its prior configuration and resolve the reported mismatch. Changing
embedding spaces requires the explicit [profile migration](docs/EMBEDDING-PROFILES.md),
not an installer rerun. This records the current service identity; it does not
attest unknown historical vectors or activate a profile. An omitted revision is
pinned from the running TEI `/info` immutable `model_sha`, including when the
old container used `main`. An explicit symbolic revision is left untouched, but
a restart refuses it: a matching label cannot prove the same loaded snapshot.
To retain that space, explicitly set the reported SHA in `EMBEDDING_REVISION`
and remove conflicting shell overrides before retrying. A stopped or unavailable
existing service must be started with its prior configuration so it can be
validated; the installer will not replace it on a guessed identity.

Pass the same `--client` targets you want to refresh. If you are coming
from v0.1.0, check that Claude Code registers Sill in `~/.claude.json`.
The older `~/.claude/.mcp.json` is a file Claude Code never reads. For a
bare `sill-mcp` entry in a current registry, confirm its database before
using `--adopt-legacy`. Restart the clients after upgrading and repeat
the memory check before resuming the schedule.

### Updating project hooks

```bash
./upgrade.sh --hooks-for /path/to/project
./upgrade.sh --hooks-for /path/to/project --hooks-only --dry-run
```

Use `--hooks-for` to refresh a project's hook commands. Use
`--hooks-only` to skip the runtime upgrade. If a Sill command has
been edited, review the reported changes and add `--force-hooks` to
replace that command. The hook merge preserves unrelated entries.
After the change, open `/hooks` in Codex CLI and review and re-approve
the updated commands. See [harness adapters](docs/adapters.md) for hook
trust and conformance details.

## Prerequisites

| Tool | Role |
|---|---|
| Docker with Compose | Runs Postgres, local embeddings, RabbitMQ, and the maintenance worker. Start the engine before installation. |
| Python 3.10+ | Runs the Sill package and hooks. |
| pipx (optional) | Installs the Sill console scripts. Otherwise the installer creates a venv in `~/.local/share/sill-venv` with links in `~/.local/bin`. |
| One connected client | See [client setup](docs/desktop-clients.md) for the CLI and desktop options. |

The installer selects a pinned CPU embeddings image for the Docker
engine's architecture. The default embedding model
(`unsloth/embeddinggemma-300m`) downloads its weights on first start
and reuses the cache on subsequent starts. The default health wait is
1,800 seconds. If startup times out, check the services and logs for the
named failure. Rerun the same install command after fixing the problem
or once the cold download finishes.

```bash
docker compose -f backend/docker-compose.yml ps
```

For explicit embedding image/platform overrides and direct Compose use,
see [the instance settings example](backend/.env.example). Keep the
embedding model and dimension consistent with the existing data.

A new install uses raw embedding inputs by default. To opt in to search
prompts on a new store with the pinned default model, add
`--embedding-profile embeddinggemma-search-v1` to the install command.
[Embedding profile requirements](docs/EMBEDDING-PROFILES.md#explicit-fresh-installation)

## What gets installed

**Docker stack** (`backend/docker-compose.yml`):

- `db` — Postgres 16 with `pgvector` and Apache AGE. Volume:
  `postgres_data`. Default container name: `sill_db`.
- `embeddings` — HuggingFace Text Embeddings Inference (CPU image),
  serving `unsloth/embeddinggemma-300m`. Volume: `embeddings_cache`.
- `rabbitmq` — Used by the maintenance worker for inter-process events.
- `maintenance_worker` — Default-on. Importance decay, drift tracking,
  housekeeping.

Reflective processing ships as the beat worker (see `docs/beats.md`), which is off
until you turn it on.

**Console scripts** (installed into pipx-managed bin, or
`~/.local/bin` if pipx isn't available):

- `sill` — top-level CLI (`sill seed import`, `sill notice` — the mint
  path, see below — and `sill connect` for client registration).
- `sill-mcp` — the MCP server. Local clients spawn it using an absolute
  executable and env-file path.
- `sill-worker` — the worker entry point used inside the Docker containers.

**Plugin** (`plugin/`):

- 14 hooks (recall, attribution checks, response patterns, a mint-path
  auto-store, a shell-idiom guard, `/clear` handoff, etc. — see
  `docs/hooks.md`, including which harnesses each one fires on)
- 8 generic response-pattern rule files
- `goodnight-triggers.txt` config
- Templates for Claude Code and Codex hook wiring

Symlinked to `~/.claude/plugins/local/sill-plugin` so Claude Code picks
it up.

---

## Configuration

To prepare a new checkout with custom settings, create a private
`backend/.env` from the example before installing:

```bash
umask 077
cp -n backend/.env.example backend/.env
```

The installer creates a private `backend/.env` if it is missing. The
defaults work for a single-host single-user install. Common reasons to edit:

- **Port collision**: bump `POSTGRES_PORT` if 5432 is already taken.
- **Run a second Sill side by side**: see below — `SILL_DB_CONTAINER`
  alone is not enough.
- **Episodic-memory integration**: set `SILL_EPISODIC_MEMORY_PATH`
  to the path of an installed
  [episodic-memory](https://github.com/obra/superpowers-plugins) CLI.

### Run a second Sill side by side

`backend/docker-compose.yml` names **four** containers, binds **three**
host ports, and takes its Compose project name (which prefixes the volume
names) from the directory it runs in — `backend`, for every clone. A
second stack needs all of it overridden, not just `SILL_DB_CONTAINER`,
or `docker compose up` collides with the first stack on the very first
container it tries to create:

```bash
# backend/.env for the second stack
COMPOSE_PROJECT_NAME=sill-second               # volume prefix; default is "backend"
SILL_DB_CONTAINER=sill2_db
SILL_EMBEDDINGS_CONTAINER=sill2_embeddings
SILL_RABBITMQ_CONTAINER=sill2_rabbitmq
SILL_MAINTENANCE_WORKER_CONTAINER=sill2_maintenance_worker
POSTGRES_PORT=55432                            # 5432 is taken by the first stack
RABBITMQ_PORT=55672
RABBITMQ_MANAGEMENT_PORT=45672                 # must be <= 65535
```

`verify.sh` and `upgrade.sh` read `backend/.env` themselves, so they act
on the stack that file describes; an exported shell variable still wins,
matching Compose's own precedence for runtime operations. Persist the
matching instance settings in `backend/.env` before registering clients.
The MCP launcher and configured hooks read the selected dotenv file for
database access. See [client configuration](docs/desktop-clients.md).

Embedding model and dimension are **locked at install time** — changing
them later requires a schema rebuild. See `docs/concepts.md`.

For the full list of `SILL_*` env vars (hook log dir, plugin dir,
project root, beat sessions dir, fake-embeddings test mode, …) see
`docs/extending.md`.

---

## The mint path

Recall is the read side; `sill notice` is the write side. It stores
one memory as a speech act — what it says, and who said it with what
illocutionary force (`assertive`/`directive`/`commissive`/
`expressive`/`declaration`; only `assertive` is truth-scored, the
rest succeed by being complied-with, kept, sincere, or felicitous).
`--speaker` is required — an unattributed mint is the store's main
hygiene hole. `--receipt-to <file>` has the store itself splice the
mint's receipt into a waiting placeholder line in a journal file,
instead of you pasting the id back in by hand. For bundling several
acts from one event into a single transaction, so they share one
`created_at` as the bundle key, see
`backend/scripts/decompose_event.py`. Full flag reference:

```bash
sill notice --help
```

This same path is what `response-patterns`' local-model insight
detector calls to auto-store a novel insight (off by default — see
`SILL_INSIGHT_DETECT` in `docs/extending.md`), which is also why
v0.1.0's auto-store never actually stored anything: the CLI it shelled
out to had no `notice` subcommand until this path was wired.

---

## Reflective beats

A config-driven rotation of headless agent-CLI sessions — each running
its own standing prompt through orient → decide → act → store → log, each
producing its own transcript — that fires on a timer instead of only when
you're at the keyboard. **Off by default, opt-in**: nothing runs until you
write a `beats.json` and start `sill-worker --mode beat` yourself, by hand
or under a schedule.

**Read `docs/beats.md` before turning this on** — specifically its
Permissions section, before its Scheduling section. A non-interactive
agent CLI with no tool permissions configured for the directory it runs in
gets every tool call denied rather than prompted and exits 0 having done
nothing; a beat worker with that problem ticks on a schedule forever while
accomplishing nothing, and nothing about the schedule itself will tell you
that's what's happening. `docs/beats.md` covers the voice config format, a
worked example, the full env-var surface, how to run one beat by hand and
confirm it actually did something, and the scheduling templates under
`scheduling/` for running it unattended once you have.

---

## Adding hooks to a project

If you want Sill's hooks to run in a specific project (recall on prompt
submit, attribution checks on `remember` calls, response-pattern checks
on Stop, etc.), point the installer at that project:

```bash
./install.sh --hooks-for /path/to/your/project
```

This writes two files:

- `/path/to/your/project/.codex/hooks.json` — rendered from
  `plugin/codex.hooks.json.template`, but only if that file doesn't
  already exist.
- `/path/to/your/project/.claude/settings.local.json` — same hooks
  merged into the existing `hooks` block (created if absent).

For existing hooks, use [the hook upgrade commands](#updating-project-hooks).
They report changes for review and preserve unrelated commands.

This is per-project wiring — `--scope project`, the default. See
"Install scope" above for `--scope home`, which registers hooks
user-wide instead of per-project.

---

## Verifying, resetting, uninstalling

```bash
./verify.sh        # six smoke checks; exits non-zero on failure
./reset.sh         # drops the postgres volume and re-seeds (asks first)
./uninstall.sh     # removes containers + volumes + backend + plugin symlink
./uninstall.sh --keep-data   # same, but keep the volumes
```

`uninstall.sh` leaves the client registries in place and prints instructions
for removing the Sill entries. See the [client guide](docs/desktop-clients.md)
for the registry paths.

For background on what's in the database, what shape memories should
take, and how recall is meant to flow, see `docs/concepts.md`.

---

## Soft dependency: episodic-memory

The `spontaneous-recall` hook can pull conversation snippets from
[episodic-memory](https://github.com/obra/superpowers-plugins) (a
separate Claude Code plugin) when it's installed. Without it, the hook
still works — it just falls back to sill-only recall.

To enable: install the episodic-memory plugin, then export
`SILL_EPISODIC_MEMORY_PATH` pointing at its CLI binary. The hook
detects it on each call.

---

## Documentation

- [Client setup](docs/desktop-clients.md) covers local app and CLI setup,
  a client capability and verification table, and the private hosted route.
- `docs/concepts.md` — what's in the database, memory shape rules,
  embedding dimension, recall patterns, workers, the AGE graph layer.
- `docs/hooks.md` — one section per shipped hook (event, env vars,
  how to disable, a canned test command), plus a harness-support table
  covering which of the two supported hosts each hook fires on.
- `docs/adapters.md` — the four-slot contract (inject/mint/capture/track)
  that makes "supports harness X" a checkable claim, the Claude Code /
  Codex divergence table, and how to add a third harness.
- `docs/extending.md` — writing good memories, the quality gate,
  adding your own hooks, customizing rule files and triggers,
  env-var cheat sheet.
- `docs/beats.md` — the reflective beat worker: voice config, the
  env-var surface, permissions (read this before scheduling anything),
  running one beat by hand, reading a transcript.

---

## Credit

Sill was extracted from the agi-memory research project (William
Taysom + Sili). The underlying schema and recall design have been
in continuous use since late 2025; this v0.1.0 release packages a
sanitized core for reuse.

License: MIT. See `LICENSE`.
