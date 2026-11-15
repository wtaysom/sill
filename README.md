# Sill

Sill gives an AI assistant a memory that lasts between conversations. You
run it on your own computer. Your assistant gets tools to remember
something and to recall it later, in a new conversation or from a
different app. It works with Claude Desktop, Claude Code and Codex.

Sill stores and searches memories on your machine. The database, the model
that indexes them and the backups all stay there. What your assistant
recalls becomes part of that conversation, like anything else you show it.

A memory in Sill records more than its words. It also records who said it
and what kind of statement it was: a claim, a request, a promise. That lets
a later reader tell a fact from a plan, and its own conclusions from
someone else's. [Concepts](docs/concepts.md#a-memory-is-a-speech-act)
explains the idea.

Version 0.3.0. [Changelog](CHANGELOG.md)

## What you need

- macOS or Linux.
- Docker with Compose, running. On a Mac, Docker Desktop and OrbStack both
  provide it.
- Python 3.10 or later. A Mac's own `python3` is 3.9, so install a newer
  one from python.org or with `brew install python`, then open a new
  terminal. If the installer finds an older Python it stops and tells you
  the path and version it found.
- Git.
- One assistant to connect: Claude Desktop (macOS), Claude Code or Codex.
  Sign in to it first.

If you already use Sill, go to [Upgrading](#upgrading).

## Install

```bash
git clone https://github.com/wtaysom/sill.git
cd sill
./install.sh --client claude-desktop
./verify.sh
```

Choose the client with `--client`:

| Client | Flag |
|---|---|
| Claude Desktop on macOS | `--client claude-desktop` |
| Claude Code | `--client claude-code` |
| Codex CLI, or a local Codex desktop session | `--client codex` |

Repeat the flag to connect more than one. If you leave it out, the
installer connects the Claude Code and Codex CLIs it finds.

The first install builds the database image and downloads about 1.2 GB of
model weights, so how long it takes depends mostly on your connection. The
installer prints the state of each service while it waits, for up to 30
minutes. If the wait ends before the download does, run the same command
again. The installer also stores 22 starter memories about working method;
add `--no-seed` to skip them.

`./verify.sh` runs six checks and ends with `All checks passed.`

Then quit and reopen your client so that it loads the new tools. The
[client setup guide](docs/desktop-clients.md) covers each client's
settings file, permission prompts and what has been tested with it.

## Your first memory

Check that a memory survives into a new conversation.

1. In a first conversation, ask:

   > Use the Sill `remember` tool to store this synthetic test preference:
   > For imaginary moon maps, I prefer dotted orange gridlines. Return the
   > UUID from the tool result.

2. Note the UUID and close the conversation. Open a new one. Use a second
   client if you connected one.

3. Ask, without giving the answer:

   > Use the Sill `recall` tool to look up my synthetic preference for
   > imaginary moon maps. Report the content and the memory UUID from the
   > tool result.

The content and the UUID should match. If the assistant answers without
using Sill, select the Sill server in that client and ask again. The
[first-use runbook](docs/onboarding/README.md) has the diagnostics for
each step, and the optional steps that follow: an identity and charter for
the instance, a backfill from earlier conversations, and scheduled work.

After that, use it in ordinary language. Ask the assistant to recall what
it knows at the start of a task. Ask it to remember a decision when you
want one kept.

## How your assistant uses it

Sill adds 39 tools to the assistant. The two that matter day to day are
`remember`, which stores a memory, and `recall`, which searches by meaning
and by wording together. The rest read and link memories, track goals, and
record when a memory replaces an older one.

In Claude Desktop the assistant uses these tools when you ask, or when it
judges that a memory is relevant. In Claude Code and Codex CLI you can add
[hooks](#hooks-for-claude-code-and-codex) that recall on every prompt and
check what gets written.

You can also work from a terminal:

```bash
sill notice --help    # store one memory deliberately
sill show <memory-id> # read a memory by its ID
sill db psql          # open a SQL shell on the store
```

`sill notice` requires `--speaker`, because a memory with no speaker cannot
later be told apart from anyone else's.

## Where your memories live

The store is a PostgreSQL database in a Docker volume, named
`backend_postgres_data` by default. It listens on `127.0.0.1` only and has
no password, so programs on your computer can reach it and other machines
cannot.

To take a backup at any time, with the default names:

```bash
docker exec sill_db pg_dump -U sill -d sill | gzip > sill-backup.sql.gz
```

The file holds your memories as plain SQL, so keep it private.
[Restoring a backup](docs/RESTORE.md) has steps of its own; read it before
you need it.

## Upgrading

Pause any scheduled work and close the connected clients. Then, from your
existing checkout:

```bash
git pull
./upgrade.sh --dry-run
./upgrade.sh
```

The dry run changes nothing. It checks the running instance and says what
the upgrade will do. `upgrade.sh` then writes a private backup under
`backups/` before it changes the database or the services. It reinstalls
the package, applies the database migrations, restarts the services and
checks the result. Restart your clients afterwards and repeat the memory
check above.

Pass the same `--client` flags you want refreshed. If you are coming from
v0.1.0, check that Claude Code registers Sill in `~/.claude.json`. The
older `~/.claude/.mcp.json` is a file Claude Code never reads.

### Coming from v0.2

The dry run stops and tells you what it needs. Expect up to two flags,
which you add to both commands:

- `--adopt-legacy`, because v0.2 registered a bare `sill-mcp` that did not
  say which database it meant. Confirm that it is this one first.
- `--archive-duplicates`, if the same content was stored twice by the same
  speaker. v0.3 keeps one live copy. The dry run lists each pair. This flag
  archives the newer copy of each after the backup; archived memories stay
  in the database.

```bash
./upgrade.sh --adopt-legacy --archive-duplicates --dry-run
./upgrade.sh --adopt-legacy --archive-duplicates
```

Two more things happen without a flag:

- If you ran on defaults you have no `backend/.env`. The upgrade creates a
  private one and records which embedding model your memories were indexed
  with.
- A v0.2 database accepts connections from other machines on your network
  without a password. The upgrade closes that, as described next.

### Closing the database port

v0.2 published the database on every network interface, with no password
and no restart policy. That setting is fixed when a container is created,
so the upgrade replaces the container and nothing else. After the backup,
and before anything else changes, it:

1. stops the database;
2. copies its data to a second Docker volume named
   `<volume>_before_<backup timestamp>`;
3. starts a new container from the same image on the same data, reachable
   from this machine only, and waits for it to be healthy.

If any of that fails, the upgrade stops before migrating anything and says
whether the database is running again or how to start it.

The second volume is your database exactly as it was before the upgrade.
Keep it until you trust the upgraded store, then remove it:

```bash
docker volume ls --filter name=_before_
docker volume rm <that volume>
```

To keep the database reachable from other machines, set
`SILL_BIND_ADDR=0.0.0.0` in `backend/.env` before upgrading.

The [upgrade guide](docs/UPGRADING.md) has the rest: every step in order,
what to do if the upgrade stops, how the embedding model is identified,
and the case where the upgrade declines to replace the container.

## Hooks for Claude Code and Codex

Hooks are optional and apply to the two command-line clients. They recall
relevant memories when you submit a prompt, check the attribution of what
gets stored, and watch for a few patterns in replies.
[Hooks](docs/hooks.md) describes each one and which client it runs in.

You choose where they apply:

```bash
./install.sh --hooks-for /path/to/project   # that project only (default)
./install.sh --scope home                   # every project
```

With `--hooks-for`, hooks go into that project's
`.claude/settings.local.json` and `.codex/hooks.json`. Other projects are
untouched, and each new project needs its own run.

With `--scope home`, hooks go into `~/.claude/settings.json` and
`~/.codex/hooks.json`, and a short instructions block is added to
`~/.claude/CLAUDE.md` without touching what is already there. Every prompt
in every project then pays for a recall, and work in any project can reach
the one store.

You can use both in the same run. To refresh a project's hooks after an
upgrade:

```bash
./upgrade.sh --hooks-for /path/to/project
./upgrade.sh --hooks-for /path/to/project --hooks-only --dry-run
```

`--hooks-only` skips the rest of the upgrade. If you have edited a Sill
hook command, the upgrade shows the difference and waits for
`--force-hooks`. Your own hooks are left alone. In Codex CLI, open `/hooks`
afterwards to review and approve the updated commands.
[Harness adapters](docs/adapters.md) explains hook trust.

Hooks can also include conversation snippets from
[episodic-memory](https://github.com/obra/superpowers-plugins), a separate
Claude Code plugin. Install it and set `SILL_EPISODIC_MEMORY_PATH` to its
CLI. Without it, hooks recall from Sill alone.

## Configuration

The defaults suit one person on one computer. Settings live in a private
`backend/.env`, which the installer creates if it is missing. To choose
settings before installing:

```bash
umask 077
cp -n backend/.env.example backend/.env
```

The usual reason to edit it is a port that is already in use: set
`POSTGRES_PORT` if something else has 5432.

The embedding model and its dimension are fixed when the store is created.
Changing them later is a migration of its own; see
[embedding profiles](docs/EMBEDDING-PROFILES.md). A new store can opt in to
search prompts with `--embedding-profile embeddinggemma-search-v1` at
install.

[Extending Sill](docs/extending.md) lists every `SILL_*` setting.

### Run a second Sill side by side

The Compose file names four containers, binds three host ports, and takes
its project name, which prefixes the volume names, from the directory it
runs in. That is `backend` for every clone. A second copy needs all of
them changed:

```bash
# backend/.env for the second copy
COMPOSE_PROJECT_NAME=sill-second               # volume prefix; default is "backend"
SILL_DB_CONTAINER=sill2_db
SILL_EMBEDDINGS_CONTAINER=sill2_embeddings
SILL_RABBITMQ_CONTAINER=sill2_rabbitmq
SILL_MAINTENANCE_WORKER_CONTAINER=sill2_maintenance_worker
POSTGRES_PORT=55432                            # 5432 belongs to the first copy
RABBITMQ_PORT=55672
RABBITMQ_MANAGEMENT_PORT=45672                 # must be 65535 or lower
```

Put these in `backend/.env` before you connect a client. `verify.sh`,
`upgrade.sh`, the MCP server and the hooks all read that file to find
their database.

## What gets installed

Four services run in Docker:

| Service | What it does |
|---|---|
| `db` | PostgreSQL 16 with pgvector and Apache AGE. Holds the memories. Container `sill_db`. |
| `embeddings` | Runs the `unsloth/embeddinggemma-300m` model locally to index text for search. |
| `rabbitmq` | Carries events for the maintenance worker. |
| `maintenance_worker` | Housekeeping: importance decay and drift tracking. |

Three commands are installed, through pipx if you have it and otherwise
into `~/.local/share/sill-venv` with links in `~/.local/bin`:

- `sill`, the command-line tool.
- `sill-mcp`, the server your client starts. Each client entry names it by
  full path, together with the settings file of the instance it belongs to.
- `sill-worker`, which runs inside the worker container.

The `plugin/` directory holds 15 hooks, 8 rule files for reply patterns and
the templates for wiring them. It is linked at
`~/.claude/plugins/local/sill-plugin`.

## Reflective beats

A beat is a session your assistant runs by itself on a timer: it reads,
decides, acts, stores what it found and writes a log. This is off unless
you write a `beats.json` and start `sill-worker --mode beat`.

Read the Permissions section of [the beats guide](docs/beats.md) before
you schedule anything. A command-line agent with no tool permissions has
every tool call refused and still exits successfully, so a schedule can run
indefinitely while doing nothing, and the schedule will not tell you.

## Checking, resetting and removing

```bash
./verify.sh                  # six checks; fails loudly if one does not pass
./reset.sh                   # deletes the database and starts fresh (asks first)
./uninstall.sh               # removes containers, volumes, commands and the plugin link
./uninstall.sh --keep-data   # the same, but keeps the volumes
```

`uninstall.sh` leaves your clients' settings alone and prints how to remove
the Sill entry from each.

## If something goes wrong

| What you see | What to do |
|---|---|
| The installer stops at Python. | It names the `python3` it found and its version. Install Python 3.10 or later and open a new terminal. |
| It waits a long time on `embeddings`. | The first start downloads the model. If the wait ends first, run the same install command again. |
| Port 5432 is already in use. | Set `POSTGRES_PORT` in `backend/.env` before installing. |
| Your client does not show the tools. | Quit and reopen it, then check that client's section of the [client setup guide](docs/desktop-clients.md). |

To see the state of the services and what one of them logged:

```bash
docker compose -f backend/docker-compose.yml ps
docker compose -f backend/docker-compose.yml logs --tail 60 embeddings
```

## Documentation

- [Concepts](docs/concepts.md): what is in the database, what a memory
  records, how recall ranks results.
- [First-use runbook](docs/onboarding/README.md): the memory check above,
  then identity, backfill, first beats and scheduling.
- [Client setup](docs/desktop-clients.md): each client's settings file,
  what has been tested, and a private hosted route for ChatGPT.
- [Upgrade guide](docs/UPGRADING.md) and
  [restoring a backup](docs/RESTORE.md).
- [Hooks](docs/hooks.md) and [harness adapters](docs/adapters.md): what
  each hook does, and how support for a client is defined and tested.
- [Extending Sill](docs/extending.md): writing good memories, your own
  hooks and rule files, every setting.
- [Beats](docs/beats.md) and [identity](docs/identity.md): scheduled work
  and the instance's identity card.
- [Embedding profiles](docs/EMBEDDING-PROFILES.md): changing how text is
  indexed.
- [Release rehearsal](docs/RELEASE-REHEARSAL.md): what was tested for this
  release, and what was not.

## Credit

Sill was extracted from the agi-memory research project (William Taysom
and Sili). The schema and recall design have been in continuous use since
late 2025. Sill packages a sanitized core of it for reuse.

License: MIT. See `LICENSE`.
