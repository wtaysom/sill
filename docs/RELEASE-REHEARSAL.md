# v0.3.0 release candidate

**Status on 2026-11-13.** Candidate `7facec3` reports `0.3.0` in package,
plugin and MCP metadata. Publication is pending. The v0.2.0 rehearsal below
remains unchanged as a historical record.

## Evidence and measured revisions

CI exercised the PR merge `e278884` for head `7facec3`. Mac installation
and database-preservation checks used `799721a`; native client and worker
checks used `8380f2b`. The intervening installer, build and isolated-restore
changes left the packaged runtime, hooks and templates unchanged. The
changes from `799721a` to `7facec3` affect CI verification and tests only.
These are component comparisons, not claims that every rehearsal used the
same checkout.

| Check | Observed result |
|---|---|
| Python and packaging | All six jobs in [CI run 43447296240](https://github.com/wtaysom/sill/actions/runs/43447296240) passed. Python 3.12: 1,333 passed, 158 skipped; Python 3.10: 1,316 passed, 175 skipped. Both clean-wheel jobs passed. The 17 additional Python 3.10 skips are CI-verifier tests requiring `tomllib`. |
| Database contracts | Seven schema cases and 173 transactional tests passed in that run, including embedding interruption/resume, activation and rollback preserving newer records. The embedding tests use deterministic HTTP fixtures. |
| Mac fresh install | Native Apple Silicon with OrbStack, at `799721a`: a new Bookworm database image and empty model cache, first install in 1,002 seconds, repeat in 56 seconds. Some worker build layers were cached. PostgreSQL 16.16 / Debian 12 reported matching collation version 2.36. First inserted 22 seeds; repeat skipped all 22 and preserved the database, profile, seed rows and temporary client settings. |
| Linux fresh install | Native amd64 CI: cold install in 154.5 seconds, repeat in 16.9 seconds, real service readiness, 22 seeds skipped on repeat, and separate-process MCP remember/recall of the same UUID. No beat schedule was installed. |
| Embedding service | Both native platforms served the pinned `unsloth/embeddinggemma-300m` revision `bfa3c846ac738e62aa61806ef9112d34acb1dc5a`, with finite 768-dimensional, L2-normalized vectors and the selected search profile. Intel Mac installation remains unverified. |
| Tagged upgrades | Actual `v0.1.0` and `v0.2.0` stores passed dry runs, full upgrades and zero-pending repeats at `8380f2b`. Records, vectors, AGE edges, historical goals and operator files survived. All 52 release functions and 548 common schema objects matched fresh installation; seven historical, data-created `SUPPORTS` objects account for the full-dump difference. |
| Existing database preservation | Six further upgrade/repeat/installer operations at `799721a` retained each exact existing database container and image, storage, cluster, collation 2.31, data, model and operator files. Maintenance workers reported 0.3.0. |
| Backup restore | Both original pre-upgrade archives passed the explicit [isolated AGE restore procedure](RESTORE.md), including fresh-session Cypher and complete table/sequence, historical schema, record, goal and ledger comparisons. The initial failed restore was retained; acceptance used new isolated targets. Eleven real AGE regression cases passed on each Python version. |

An exact-source `7facec3` wheel was also installed in isolated local Python
3.12 and constrained Python 3.10 under Rosetta. Both discovered 39 MCP tools;
optional `stage` was absent by default. The wheel SHA-256 is
`2fd8f9185c05510a7b0d4591f5f7160060a7c4eeeacd105e491af41131b20c5f`.
Import and discovery checks do not establish native client behavior.

The earlier embedding operator rehearsal at `918316a` exercised actual CLI
prepare, interrupted staging, refusal of premature activation, resume,
activation and interrupted rollback using a deterministic HTTP fixture.
New and changed records survived rollback. Final CI repeats the relevant
transactional cases; real pinned-service coverage comes from the separate
installation rehearsals above.

## Native clients and supervised workers

At `8380f2b`, Claude Code 2.1.296 remembered and recalled in separate
processes, and Codex CLI 0.162.1 retrieved the same UUID with matching speaker
and full declared production. All four Codex prompt/guard/write/independent
write callback cases passed after normal hook review and trust. Claude's
four cases also passed across separate fixture runs. Its final independent
overwrite read the exact existing synthetic note before writing in a new
session. The recalled test value appeared in the callback context and model
reply, but not in the prompt, note or Read result. Per-result exposure counts
matched the hook output without refreshing explicit-use timestamps.

The installed worker completed one bounded native turn through each CLI.
Both produced the expected synthetic file and advanced rotation once.
Separate two-second native timeout checks left rotation unchanged, refused
a competing worker with exit 75, released the original locks and left no
process-group members. Two additional phases used explicitly synthetic CLI
output with the real worker auditors to verify findings and an unavailable
quote source. These are not native model or callback observations. No beat
schedule was installed.

## Remaining checks and limits

- Repeat the deliberate two-chat round trip in Claude Desktop with the final
  0.3 runtime. The earlier Desktop 5.11492.2 result at `f3233a5` reported
  Sill 0.2.1 and remains separate evidence.
- Observe the local Codex desktop app round trip manually. CLI success does
  not establish desktop behavior.
- Complete hosted ChatGPT sign-in, private tunnel/plugin setup and an actual
  hosted tool round trip. Local tunnel-client checks do not establish it.

Fresh builds use Bookworm. Managed upgrades and installer reruns preserve
the existing database container to avoid silently changing its collation
runtime. That also retains its OS/security state, ports and restart settings
until a separately planned, backed-up migration. Direct Compose operations
can bypass this preservation path. The frozen baseline's known generated
`dedupe_key` initialization error remains recorded; successful validation
is not a claim of an error-free initialization log.

See [client capabilities and verification](desktop-clients.md#client-capabilities-and-verification)
for each integration's scope. This candidate has not been tagged or published.

Sili

---

# Clean-Machine Acceptance Rehearsal — v0.2.0

**Date:** 2026-08-05
**Ref:** `275bd70` (branch `feat/v0.2-release`), rehearsed from a fresh clone
**Question:** does "please get this working" actually work, for someone who
has never run this?
**Amended 2026-08-05, same branch:** §4 originally listed eight unverified
items. Five of them turned out to be attemptable on this machine, and
attempting them found three more defects — a Ctrl-C that crashed, a smoke
check that could not see a dead server, and a mint path whose database
coordinates arrived through an unrelated module's import side effect. §4 is
now the four that genuinely cannot be closed here; §5 is the record of the
five that could.

Every plan in the v0.2 series verified its own phases against a development
machine that already had a live Sill stack, a populated tool-permission
allow-list, both agent harnesses configured, and containers running. None of
that is true of the machine this ships to. This document is the record of
walking `docs/onboarding/README.md` end to end with as little of that as
could be arranged, and it exists mainly for the failures it found.

**Bottom line.** A first real operator will not be the first person to walk
this path — nine of the nine phases were reached, and eight of them reached
their stated done-condition. But **four phases could not be completed as
documented** (1, 2, 4, 7) and a fifth printed output the doc does not match
(8); one of those was a defect no documentation change could fix — `sill-mcp`
did not start at all on a fresh install. All are fixed here. The
parts of the christening that need an actual person — a charter in their own
words, and a name they will be glad to type — were not rehearsed and cannot
be.

---

## 1. The environment, and exactly what leaked

Host: macOS 25.5.0, Apple Silicon (`arm64`). Docker via OrbStack, server
29.4.0. `python3` is Homebrew's **3.14.4**. **`pipx` is not installed**, so
`install.sh` took its documented venv fallback throughout.

**Isolated:**

| Thing | How |
|---|---|
| `HOME` | `/tmp/sill-rehearsal-home`, created empty. No pre-existing `.claude`, `.codex`, `.local`, or state directory. |
| Repo | `git clone` of the working repo into `/tmp/sill-rehearsal/sill`, checked out at `275bd70`. The development checkout was never installed from. |
| `PATH` | Rebuilt from scratch, **with `~/miniconda3/bin` removed** — that is where this machine's `python3.10` and an already-installed `sill`/`sill-mcp`/`sill-worker` live. Probe after isolation: `python3.10 -> NOT FOUND`, `pipx -> NOT FOUND`, `sill -> NOT FOUND`. |
| Console scripts | `install.sh`'s venv fallback put them in `/tmp/sill-rehearsal-home/.local/share/sill-venv`, symlinked into the throwaway `~/.local/bin`. |
| State dir | `$XDG_STATE_HOME` under the throwaway HOME; the identity card and backfill archive landed there. |
| Docker | Compose project `sillrehearsal`, containers `sillx_db` / `sillx_embeddings` / `sillx_rabbitmq` / `sillx_maintenance_worker`, host ports 55432 / 55672 / 45672. Nothing shared with the live stack. |
| Transcripts | `sill backfill` was only ever pointed at a synthetic fixture tree (`/tmp/sill-rehearsal/fixture-home`). No real history was scanned, planned against, or archived. |

**Leaked, named:**

1. **The docker daemon.** There is one per machine. A fresh `HOME` also loses
   OrbStack's docker *context* (it lives in the real `~/.docker`), so the CLI
   fell back to `unix:///var/run/docker.sock`, which OrbStack does not create,
   and `install.sh` stopped at preflight with `docker is installed but the
   daemon isn't responding` — a correct message for a condition the isolation
   created. Bridged with an explicit `DOCKER_HOST` pointing at the real
   socket.

2. **The `claude` CLI's login.** `claude` under the throwaway HOME answers
   `Not logged in · Please run /login`. The token is in the macOS Keychain but
   the CLI gates on the real `~/.claude.json`; copying the account-metadata
   keys across (no secret is in them) did not help. **So the two agent-CLI
   phases — 4 and 6 — ran with `HOME=/Users/…`, everything else still
   isolated.** Two mitigations kept that from contaminating the result:
   phase 4 uses `--setting-sources`, which the runbook already prescribes; and
   for phase 6 the worker's `SILL_BEAT_CLI` pointed at a shim injecting
   `--setting-sources project,local`, so no user-scope settings or hooks
   applied. A probe confirmed the beat children could not see the operator's
   ambient instructions file. The live memory store was counted immediately
   before and immediately after the beat window: **39,936 rows both times**,
   and it holds zero rows from any rehearsal voice. (That is a statement about
   the beats. For what the rehearsing *session* wrote there, see (4).)

3. **Residue the CLI wrote in the real HOME**, as a side effect of (2), none
   of it by hand: session transcripts under
   `~/.claude/projects/-private-tmp-sill-rehearsal-sill/` and
   `…-sill-permission-demo/`, and one `projects` entry in `~/.claude.json`
   recording the rehearsal directory as **untrusted**. `mcpServers` there is
   unchanged, and `~/.codex/config.toml` was not touched at all.

4. **The rehearsing session's own Stop hook.** Separate from the beats, and
   found only by checking afterward: the operator's `response-patterns` hook
   auto-stored six rows into the live store during the rehearsal window,
   because it attributes by working directory and this work happened in the
   Sill checkout rather than the one directory where that hook is configured
   log-only. Nothing the rehearsal *ran* did this — the beats wrote to the
   isolated store, and the live store holds zero rows from any rehearsal
   voice. It is recorded here because "the live store was not written to"
   would otherwise have been the natural thing to say, and it would have been
   false.

5. **The multi-instance config path.** A genuinely clean machine needs no
   `backend/.env`; this one does, because the live stack already owns the
   default container names and ports. So the rehearsal exercised the
   documented side-by-side configuration throughout — which is how findings
   F2, F8 and part of F9 below were found, and which means the *default* single-stack
   path got less exercise than the non-default one in exactly those places.

6. **Not attempted: the collision itself.** Proving that
   `SILL_DB_CONTAINER`-alone is insufficient by letting Compose try to create
   `sill_embeddings` would have aimed a `docker compose up` at the live
   stack's containers. The `.env.example` finding at the end of §3 is
   therefore argued from the compose file (four `container_name:` keys, one
   of them documented) rather than from a crash. *(Since narrowed: `docker
   compose config` — which starts nothing — resolves all four default names
   and three host ports, and three of the names and all three ports are
   occupied on this host. See §4, item 2.)*

---

## 2. The walk

Nine phases, in the runbook's order. "Done?" is the doc's own stated
done-condition, not a summary judgment.

| # | Phase | Command | What actually happened | Done? |
|---|---|---|---|---|
| 1a | Install | `./install.sh` | Steps 1–3 fine; step 4 exited 1: `db/embeddings not healthy after 600s`. The embeddings model was still loading. | **No** — see F1 |
| 1b | Install, retried | `./install.sh` | Exit 0. Steps 4–10 ran; seed reported `inserted=22, skipped=0, errors=0`. | Yes |
| 1c | `verify.sh` | `./verify.sh` | Checks 1–2 pass, then `FAIL: memories count is '0', expected >= 22`. It had queried a *different* container. | **No** — see F2 |
| 1d | `verify.sh`, fixed | `./verify.sh` | Six `pass:` lines, `All checks passed.`, exit 0. | Yes |
| 2a | Adapters — Codex | `codex mcp list` | `sill  sill-mcp  …  enabled`. | Yes |
| 2b | Adapters — Claude | `claude mcp list` | `No MCP servers configured.` after a successful install. | **No** — see F3 |
| 2c | Adapters, after F3 fix | `claude mcp list` | `sill: sill-mcp - ✘ Failed to connect`; the server itself was broken. | **No** — see F4 |
| 2d | Adapters, after F4 fix | `claude mcp list` | `sill: sill-mcp - ✔ Connected`. | Yes |
| 2e | Conformance | `… -m pytest tests/test_adapter_conformance.py -q` | `11 passed`. The doc's own command failed three ways first — see F5. | Yes |
| 2f | Project hooks | `./install.sh --hooks-for <project>` | Wrote `.codex/hooks.json`; merged 8 entries across 4 events into `.claude/settings.local.json`. | Yes |
| 3 | Identity card | `sill identity init` / `set` / `show` | Card printed with `name: null (not yet christened)` — matching the doc line for line. | Yes |
| 4a | Permissions, the demo | `claude --print … --setting-sources project` in a cold dir | `The write was denied … so the file was not created.` / `exit: 0` / no file. The documented pair, reproduced. | Yes |
| 4b | Permissions, rung 1 | `… --allowedTools Write` | `proof.txt` contains `hi`. | Yes |
| 4c | Permissions, rung 2 | the doc's `cat > .claude/settings.local.json` heredoc, then `--setting-sources project` | Clobbered phase 2f's hooks (8 entries → 0), then the write was refused anyway. | **No** — see F6, F7 |
| 4d | Rung 2, corrected | merge + `--setting-sources project,local` | `hook entries preserved: 8`, and `proof.txt` contains `hi`. | Yes |
| 5a | Backfill `plan` | `sill backfill plan --home <fixture>` | 3 files across 3 projects, `Nothing has been read, written, stored, or logged`. Narrowed scope dropped `codex` from `scanned=` as documented. | Yes |
| 5b | Consent gate | `sill backfill run --home <fixture>` | `refusing to read or archive anything without --confirm`, exit **64**. | Yes |
| 5c | Backfill `run` | `… --projects=-…-orrery --confirm` | Archived 1 file + manifest into the throwaway state dir, printed its own undo command. | Yes |
| 6 | Supervised beats | `SILL_BEAT_INTERVAL_SECONDS=1 sill-worker --mode beat` | Startup banner matched the doc including the derived guard scope. `[analyst] Beat complete in 238.4s`, `[reflector] Beat complete in 142.6s`. Both wrote real files; the analyst's mint spliced a real receipt (`7a2bef1d…`, verified present in the store). Both entries read. | Yes |
| 7a | Fault drill | the documented refusal payload | **Silence.** Store healthy, guard mute — the exact failure the drill exists to make impossible. | **No** — see F8 |
| 7b | Fault drill, resolved | same payload, guard's DB coordinates supplied | The full `permissionDecision: deny` JSON, byte-for-byte as the doc quotes it. | Yes |
| 7c | Positive control | placeholder-line payload | No output, exit 0 — allowed. | Yes |
| 7d | Second guard | `tool-type-witness.py` payload | Denied, naming the honest alternatives. | Yes |
| 8a | Charter | `date -u … >> charter.md` | Placeholder file stamped `2026-08-05T19:44:15Z`. **The charter itself was not rehearsed** — see §4. | Partly |
| 8b | Naming | `sill identity set --name … --charter charter.md` | `christened_at: 2026-08-05T19:44:15.454837+00:00`. | Yes |
| 8c | First mint | `sill notice … --receipt-to journal/christening-001.md` | First attempt **failed**, and the placeholder line stayed literal — the receipt architecture working from the honest side. Second attempt: `Receipt written by the store into … (line 5)`, placeholder gone, id has a row. | Yes |
| 8d | Cadence | recorded in `charter.md` | `SILL_BEAT_INTERVAL_SECONDS=21600`, written down, nothing started. | Yes |
| 9 | Nothing scheduled | `launchctl list`, `crontab -l`, `pgrep` | No `com.sill.*` job loaded, no plist installed, no cron entry, no worker running. | Yes |

---

## 3. What the documentation got wrong

Nine findings. Each was hit by running the documented command, and each fix
was re-verified by re-running that phase — the "Verified" column says with
what.

### F1 — `install.sh` gives up before the model is ready, and says nothing about that

`install.sh` waits 600 s for the embeddings healthcheck. Measured here:
container start `19:04:16Z`, first healthy probe `19:19:22Z` — **903 s**. The
compose file pins `platform: linux/amd64`, so on Apple Silicon the image runs
emulated, and the healthcheck's own `start_period` is also 600 s, so the
service passes *through* `unhealthy` on a normal first boot. The failure
message names a log command and does not mention that the install is
idempotent, that the download is still running, or that
`SILL_INSTALL_WAIT_HEALTHY_S` exists — that variable appeared in no `.md`
file in the repo.

**Fixed:** README and `01-install.md` now give the measured number, say the
retry is safe, and document the override.
**Verified:** re-ran `./install.sh` after the container went healthy — exit 0,
steps 4–10 complete.

### F2 — `verify.sh` and `upgrade.sh` ignored `backend/.env`, so they could check the wrong database

`verify.sh` check 1 runs Compose from `backend/`, so `.env` is loaded and the
check goes green. Check 3 shells `docker exec "${SILL_DB_CONTAINER:-sill_db}"`
straight from the process environment, which nothing sets. The result here was
`FAIL: memories count is '0'` — it had queried the *other* Sill on this
machine. `01-install.md`'s own remedy for a name collision is "set
`SILL_DB_CONTAINER` in `backend/.env`", which is precisely what breaks it.
Worse than the failure: with two installs sharing credentials, this reports a
**pass** off the wrong database.

**Fixed:** both scripts now load `backend/.env` with Compose's precedence
(exported shell variable wins).
**Verified:** `./verify.sh` → six passes, exit 0; new tests in
`test_env_and_mcp_wiring.py` (8 cases, both scripts) pin the parser, the
precedence, the missing-file case, and the ordering.

### F3 — `install.sh` registered the MCP server where Claude Code does not look

Step 7 wrote `~/.claude/.mcp.json` and reported
`added mcpServers.sill -> …`. Claude Code's user-scope registry is
`~/.claude.json`; that file does not exist on this machine's *working* install
either. `claude mcp list` after a clean install: `No MCP servers configured.`
Phase 2's remedy — "re-run `./install.sh` and read step 7's output" — is an
infinite loop, because step 7 reports success every time. The Codex half was
correct all along.

**Fixed:** step 7 now runs `claude mcp add --scope user sill -- sill-mcp` when
the CLI is present (idempotent by its own report), and merges into
`~/.claude.json` otherwise.
**Verified:** removed the entry, re-ran `./install.sh`, then
`claude mcp list` → `sill: sill-mcp - ✔ Connected`. Four tests pin the path,
the command, the entry shape, and that `--dry-run` writes nothing.

### F4 — the MCP server did not run on a fresh install, and `verify.sh` said it was fine

`backend/pyproject.toml` asked for `mcp>=1.0.0`. A fresh install resolves that
to **2.0.0**, which removed the `Server.list_tools()` API `sill_mcp_server.py`
registers through. An `initialize` handshake against the freshly installed
server returned, in full:

```
'Server' object has no attribute 'list_tools'
```

The development machine has mcp 1.25.0, which is why nothing caught it.
`verify.sh` check 2 passed the whole time, because `sill-mcp --help` exits
before touching the SDK. **This is the finding that most justifies the
exercise:** a green six-of-six verify on an MCP server that could not serve a
single tool.

**Fixed:** pinned `mcp>=1.0.0,<2`, with the reason in the dependency itself.
**Verified:** reinstalled (resolved 1.29.0); the handshake now returns
`{"jsonrpc":"2.0","id":1,"result":{…"serverInfo":{"name":"sill","version":"0.2.0"}}}`,
and `claude mcp list` reports Connected.
**Check 2 fixed afterwards:** it now performs a real `initialize` handshake
instead of `--help`. See §5, item 4.

### F5 — the docs told operators to run an interpreter that does not exist

Eight invocations of `python3.10` across `01-install.md`, `02-backfill.md`,
and `identity.md`. On a machine whose Python is 3.11/3.12/3.13/3.14 that is
`command not found` — observed on the first one run here. The two follow-on
commands fail too: `python3 -m pytest` gives `No module named pytest`, and the
doc's own remedy `pip install -e backend[dev]` gives `command not found: pip`
(Homebrew Python ships `pip3`, and there was no pipx).

**Fixed:** all eight now say `python3`; the pytest instruction resolves the
interpreter that actually has the backend, the same way `scheduling/README.md`
and `install.sh` already do.
**Verified:** every corrected command re-run — the identity-path one-liners,
the backfill fixture block, the argparse-gotcha demonstration (which still
fails exactly as documented, `expected one argument`), and the conformance
suite (`11 passed`). A new test refuses any `python3.N` in any shipped doc.

### F6 — phase 4 deleted phase 2's work

Rung 2's `cat > .claude/settings.local.json <<'EOF'` overwrites the file
`install.sh --hooks-for` writes its hooks into, two phases earlier — and phase
2 says phase 7's drill depends on those hooks. Measured: `hook entries: 8`
before, `hook entries: 0` after.

**Fixed:** rung 2 merges, and says why.
**Verified:** re-ran from the phase-2 state — `hook entries preserved: 8`.

### F7 — rung 2 could not pass as written, and the doc misdiagnosed why

The command pairs `.claude/settings.local.json` with
`--setting-sources project`. Claude Code has three sources — `user`, `project`,
`local` — and `settings.local.json` is the **local** one, so the file was
never read. The doc attributes the failure to workspace trust. Trust is a real
second cause and it looks different: with the grants in `settings.json`, the
CLI announces itself —

```
Ignoring 3 permissions.allow entries from .claude/settings.json: this workspace
has not been trusted. …
```

— whereas the source mismatch is silent. Reproduced both ways in one sitting,
with `hasTrustDialogAccepted: False` throughout: `--setting-sources project`
fails, `--setting-sources project,local` writes the file.

**Fixed:** the command uses `project,local`; the diagnosis lists the source
mismatch first, with the silence as its tell, and quotes the real trust
warning.
**Verified:** corrected command run from a clean phase-2 state — `proof.txt`
contains `hi`, workspace still untrusted.

### F8 — the drill's silence has a third cause the doc does not name

The documented refusal payload printed **nothing** on the first run, with the
stack healthy and `verify.sh` green. The doc's only account of silence is "the
store could not be reached", and its only diagnostic is `docker compose ps`,
which looked perfect. The guard does not hold a connection; it shells to
`docker exec` using `SILL_DB_CONTAINER` / `SILL_DB_USER` / `SILL_DB_NAME` from
its own environment, and does not read `backend/.env` — so a side-by-side
install silently fails open. A guard that has never fired is
indistinguishable from a guard that is not wired, which is the drill's whole
premise, and here the doc's remedy would have sent the operator to check a
healthy container.

**Fixed:** "What silence means" now gives the guard's own query as the first
diagnostic, ahead of `docker compose ps`, and names the variables.
**Verified:** the new diagnostic errors clearly with the default name and
answers `23` with the right one; the refusal then printed in full.

### F9 — three smaller output mismatches

- `03-first-beats.md`'s transcript header omitted the `# Model:` line the
  worker really writes. **Fixed** (and flagged as best-effort, unlike
  `# Spawned:`).
- `05-christening.md`'s sample receipt reads `[<name>/assertive]`; an unforced
  mint prints `[<name>/untagged]`. **Fixed**, with the `--force` values named.
  **Verified:** `[rehearsal/untagged]` without the flag, `[rehearsal/assertive]`
  with it.
- README's status line still said `v0.1.0`, and the "~300MB" model figure
  appears in four places against a measured **1.2 GB** on disk (1.1 GB in one
  blob). **Fixed** in README, `concepts.md`, `01-install.md`, and
  `.env.example`.

Also fixed, found the same way: `.env.example` documented one of the four
container-name overrides and no Compose project name, while README's advice
for running two instances named only `SILL_DB_CONTAINER`. Both now carry the
full set, and README has a worked side-by-side block.

---

## 4. Explicitly unverified

Four items. This list was eight. The other four were not unverifiable — they
were *unattempted*, which is a different thing, and shipping the second while
writing the first is the overclaim this project's receipt discipline exists to
prevent. §5 records what happened when they were attempted, including the
three defects they turned up.

What remains is here because of how the world is, not how much effort was
spent.

1. **A charter, and a name.** Phase 8's substance is prose in a person's own
   words and a name they chose. A placeholder file with a timestamp was
   produced to exercise the mechanism; the act itself is not rehearsable by
   anything, and no agent should simulate it.

2. **A default-named, default-ported install, end to end.** `docker compose
   config` with no `.env` present resolves the four shipped container names —
   `sill_db`, `sill_embeddings`, `sill_rabbitmq`, `sill_maintenance_worker` —
   and host ports 5432 / 5672 / 15672. On this host, three of those names and
   all three ports are already held by an unrelated running stack. (That is at
   least an improvement on §1's item 6, which could only argue the collision
   from the compose file: it is now observed, without aiming a `docker compose
   up` at anything live.) Freeing the defaults means stopping that stack — and
   it is the memory store behind a supervised worker that fires unattended
   every two hours under a restart-on-exit supervisor, so the window in which
   it is down is a window in which an unattended job runs against a store that
   is not there. That trade is not worth one observation, so this stays open,
   deliberately. The collision-avoiding configuration is the path that got
   exercised end to end; the default one did not.

3. **Linux, and any non-Apple-Silicon host — narrowed.** Every *measurement*
   in this document is from one macOS ARM machine running emulated
   `linux/amd64` containers, and F1's 903 s in particular should be much
   smaller elsewhere. But "nothing is tested on Linux" would be false: CI runs
   two `ubuntu-latest` jobs on every push — the full backend test suite on
   Python 3.10 and 3.12, and `schema-identity`, which builds the database
   image, boots a fresh container and a simulated-v0.1.0 baseline one, runs
   `./upgrade.sh` on the baseline, diffs the two `pg_dump` schemas, and
   exercises three live DB call shapes. So the container, schema, migration,
   upgrade and Python paths are exercised on Linux x86 continuously. What is
   untested there is the operator-facing shell: `install.sh` end to end, the
   hook wiring into real agent harnesses, and the beat worker.

4. **The systemd unit.** Its launchd counterpart is now rendered, loaded,
   confirmed and unloaded on every macOS test run (§5, item 2), and the same
   cannot be done for `sill-beat-worker.service.template` on a machine with no
   systemd. `test_scheduling_templates.py` covers its shape — `Restart`,
   `RestartSec`, an explicit `PATH`, an absolute `SILL_BEAT_CLI`, and that it
   parses — but no `systemctl` has ever been handed it.

---

## 5. Closed afterwards, and what closing them found

Five items came off the list above. Three of them were closed by fixing
something.

1. **A beat child does reach memory through MCP.** *(was item 4 — "the one
   remaining question that materially affects what a beat can do")* One real
   beat was spawned through `beat_worker.spawn_beat()` itself, with no
   `--setting-sources` shim, and asked to report what it could see. It
   reported 38 `mcp__sill__*` tools and called the read-only
   `mcp__sill__get_health`. The count is the child's own report; the call is
   not — `tool_use: mcp__sill__get_health` and its `tool_result` are in the
   child's session jsonl, which is why this counts as observed rather than
   claimed. The rehearsal's shim was the whole cause of the
   original negative: `--setting-sources project,local` excludes user scope,
   which is where the MCP server is registered. Pinned by
   `test_spawn_beat_does_not_restrict_the_child_to_project_settings`, because
   adding a `--setting-sources` flag to that spawn would cut every beat off
   from memory while leaving exit codes, transcripts and output files
   identical. *Bound:* on this host the user-scope `sill` entry points at the
   operator's own server rather than a freshly installed `sill-mcp`, so what
   is proven is the scope question. That the shipped server answers is item 3
   below; that `install.sh` registers it at user scope is pinned by
   `test_install_prefers_the_claude_cli_for_mcp_registration`.

2. **The launchd template loads.** *(was item 2)* Rendered through
   `scheduling/README.md`'s own `sed` invocation, with a throwaway `Label` and
   `/bin/echo` in place of the interpreter: no `{{TOKEN}}` survived,
   `plutil -lint` passed, `launchctl bootstrap gui/<uid>` returned 0,
   `launchctl print` showed the service with its five-element argument array,
   and `RunAtLoad` fired — `-m worker --mode beat` appeared in the
   `StandardOutPath` log, which is what proves the log-path token resolved to
   somewhere launchd can really write. `launchctl bootout` returned 0 and the
   subsequent `launchctl print` failed with *Could not find service*. Now a
   test (`test_the_rendered_plist_loads_and_unloads`, macOS-only, throwaway
   label, `try/finally` bootout, plist in a temp dir so nothing can survive a
   reboot).

3. **Ctrl-C now stops the worker cleanly — it did not.** *(was item 3;
   **defect found and fixed**)* Under a pty, with SIGINT delivered to the
   foreground process group the way a terminal's ^C does it, the worker
   printed an eleven-frame Python traceback ending in `KeyboardInterrupt` and
   died on the signal — both mid-beat and while sleeping between beats. The
   runbook's own way of stopping a supervised first run looked like a crash.
   (The other half of the old guess was wrong: it returned the terminal in
   0.05 s, not at the 30-minute beat timeout, because `subprocess.run()` kills
   its child on the way out.) `run_beat_loop()` now catches `KeyboardInterrupt`
   and logs `Interrupted (Ctrl-C) — beat worker stopped. Rotation stays on
   [<voice>]`; observed after the fix: exit 0, no traceback, no orphaned child.
   Three pty tests in `test_beat_worker_interrupt.py`; two of them fail on the
   pre-fix code.

4. **`verify.sh` check 2 speaks MCP now.** *(was item 5; **known gap, fixed**)*
   The check starts the server, sends a real `initialize` over stdio, requires
   a well-formed result naming this server, and kills it — with a timeout, so
   a server that accepts the request and hangs fails rather than hanging
   verify.sh. Observed both ways on this machine: against the installed
   `sill-mcp` pointed at a live database, `sill 0.2.0 (MCP 2024-11-05)`,
   exit 0; against the same binary with no reachable Sill database, exit 1
   printing the server's own words — `the server exited (1) without answering
   'initialize'` / `role "sill" does not exist`. Seven tests drive it against
   stub servers that each fail in one specific way, including F4's verbatim
   `'Server' object has no attribute 'list_tools'` and a stub that exits 0
   having said nothing — the shape the old `--help` check called a pass.

5. **Where the beat's mint found the right database.** *(was item 6; explained,
   and the fragility behind it **fixed**)* `sill-worker` imports `worker.py`,
   whose module-level `load_dotenv()` resolves `.env` against **worker.py's own
   directory** rather than the working directory — so `backend/.env` landed in
   the worker process's environment; `beat_worker.spawn_beat()` then hands the
   child `{**os.environ, …}` wholesale, so the beat's bare `sill notice` read
   `SILL_DB_CONTAINER` from a file nothing in the mint path had ever read.
   Confirmed by reproduction: importing a copy of `worker.py` from an
   unrelated working directory pulls a neighbouring `backend/.env` into
   `os.environ`. From the operator's shell nothing loads that file, so the same
   command fell back to `sill_db` — which on this host is a database with no
   `sill` role, exactly the error the rehearsal saw twice.

   That is an explanation, and it was also a defect: a worker started as
   `python -m beat_worker` imports `worker` never, and would have minted
   against the defaults — on a machine with a second Sill, into a different
   install's container. `sill.py` now loads `backend/.env` itself, with
   Compose's precedence (an exported variable still wins), the same way
   `verify.sh`, `upgrade.sh` and `memory_health.py` already do. Five tests.

**Historical 0.2 status (addressed by the 0.3 selected-instance adapter):** the guards
(`plugin/hooks/stored-slot-guard.py` and friends) reach the database by the
same `docker exec "$SILL_DB_CONTAINER"` route and still read only the process
environment — F8 fixed that with a diagnostic in the doc rather than a code
change, and this change does not revisit it.

---

## 6. If you are the first operator

Read §4 first, then `docs/onboarding/README.md`. If a phase behaves
differently than its doc says, that is worth reporting — the nine findings
above were all found by exactly that, and every one of them had been shipped
past by people who believed the document. The three in §5 were found by
distrusting this document's own word "unverified".


## 0.3 prompt/write callback rehearsal boundary

H2's offline tests exercise current Claude/Codex event shapes, synthetic prompt
text, real concurrent write-hook processes, quiet/zero/outage behavior, the
selected-instance adapter, and rendered registration. They do not establish
that a native host consumed the context. The sanitized captured event fixtures
omit prompt text; retain that limitation beside any evidence derived from them.

The release controller owns the remaining isolated native rehearsal:

1. Create a dedicated synthetic store, distinct from the live installation,
   with uniquely recognizable memory text and known IDs. Register its absolute
   env-file path using the installed interpreter and the rendered template.
   Set inherited DB coordinates deliberately to stale/nonexistent targets.
2. Record Claude Code/Codex CLI versions, exact temporary trusted project
   scope, command registration (including `SILL_INSTANCE_ENV_FILE`), and the
   captured callback's `hook_event_name`, session fields and supported tool.
   Do not infer callback trust from successful MCP connection.
3. On each host, observe a synthetic UserPromptSubmit recall, a PostToolUse
   write recall on its supported Write/Edit or apply_patch path, and a
   portable PreToolUse guard. Record visible/model context separately from
   process stdout. A second write to the same canonical path should stay
   quiet; another session should remain independent.
4. Inspect only the synthetic store's exposure/reuse evidence. Only injected
   rows increment exposure; hydration alone does not, and exposure never
   updates last_accessed. Record any unavailable/unchecked result explicitly.
5. Report callbacks actually observed for each named host. A CLI observation
   does not establish desktop or hosted callback support. Do not edit live
   app settings, trust entries, or the live Sili database for this rehearsal.

Sili
