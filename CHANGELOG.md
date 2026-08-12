# Changelog

All notable changes to Sill are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/).

## Unreleased

### Fixed

- **The stack did not come back after an engine restart, and nothing said
  so.** None of the four services declared a `restart:` policy, so Docker
  defaulted to `no`. The failure that produces is invisible rather than
  loud: `spontaneous-recall.py` degrades gracefully when the store is
  unreachable — it still emits its `[TIME]` header and returns no
  memories — which is byte-for-byte what a healthy store looks like when
  a query genuinely matches nothing. Observed on a live install
  (2026-08-12): all four containers exited cleanly during an OrbStack
  restart and stayed down over an hour across two active work projects,
  with `docker inspect` reporting `RestartPolicy: no` and the host
  showing 29 days of uptime. All four now default to
  `${SILL_RESTART_POLICY:-unless-stopped}` — surviving reboots, engine
  restarts and crashes, while still honoring a deliberate `docker compose
  stop`, which matters in a project that asks operators to name a
  shutdown condition in their charter. Pinned by
  `backend/tests/test_restart_policy.py`, including a guard that fails if
  a service is added without a policy.

## v0.2.1 — 2026-08-06

### Upgrading from v0.2.0

**Breaking, if you read `recall`'s output programmatically.** The MCP
`recall` result's per-memory `similarity` key is now `score` — the field
is serialized by name (`asdict`), so the rename reaches the wire. Same
value, honest name; see the Fixed entry for why the old one was actively
misleading. Nothing inside Sill reads the old key, but a script or agent
of yours that does will see `None`/`KeyError` rather than a wrong number.

**Re-render your hook wiring.** Two fixes below are in
`plugin/codex.hooks.json.template`'s **matchers**, which are copied into
your project's config at install time — updating the code alone leaves
the old matchers in place, and a `shell` / `shell_command` call still
never reaches a hook:

```bash
./upgrade.sh --hooks-for <project> --hooks-only --force-hooks
```

On Codex this changes the hook commands' recorded config, so Codex will
ask you to re-approve them (it SHA-256-pins each one — see
`docs/adapters.md`).

**If v0.2.0's installer already corrupted `~/.codex/config.toml`**, this
release fixes the installer but cannot repair a file it can no longer
parse. Check for the damage and remove the duplicate table by hand:

```bash
grep -n '^\[features\]' ~/.codex/config.toml
```

Two or more hits means the second one and its keys need folding into the
first. Codex refuses to start on that file, so this is worth checking
even if you are not sure the installer ever touched it.

### Added
- **The embeddings image and platform are operator-overridable**
  (`EMBEDDING_IMAGE`, `EMBEDDING_PLATFORM`). Defaults are unchanged —
  `cpu-1.8` on `linux/amd64`, byte-for-byte what v0.2.0 shipped — so an
  operator who sets nothing sees no difference. Both were previously
  hardcoded, which silently committed every arm64 host to running the
  embeddings server under emulation for the life of the install; that is
  the 903 s first boot the README already warns about, and it had no
  remedy short of editing a tracked file. Upstream publishes a native
  arm64 line, and `backend/.env` can now point at it. Documented in
  `backend/.env.example` and under README → Prerequisites → "Apple
  Silicon: the native image", including the two things that are easy to
  get wrong: the arm64 line is built from main rather than tagged
  releases (so it trades a pinned version for a rolling one), and it does
  not buy Metal — containers on macOS have no GPU access, so the win is
  the emulation tax alone. Pinned by
  `backend/tests/test_embeddings_image_override.py`.
  Contributed by Adeh DeSandies ([@adevadeh](https://github.com/adevadeh)), branch `feat/arm64-embeddings`.
- **`SILL_BIND_ADDR` chooses which interface the published ports bind
  to.** Default `0.0.0.0` — exactly what an unprefixed Compose mapping
  already meant, so upgrading moves nobody's ports. The reason it is now
  a decision rather than a default: `db` runs with
  `POSTGRES_HOST_AUTH_METHOD=trust` and asks for no password, so reaching
  5432 *is* superuser access to the entire memory store — plus outbound
  HTTP from inside the container network, since the db image carries
  `pgsql-http` — and RabbitMQ's management UI is published on 15672 with
  the credentials from `.env.example`. Nothing in a single-host install
  needs any of it off-box: the hooks reach Postgres by `docker exec` and
  the MCP server connects to localhost. `SILL_BIND_ADDR=127.0.0.1` in
  `backend/.env` closes all three. Pinned by
  `backend/tests/test_bind_addr_override.py`, including a guard that
  fails if a service starts publishing a fourth port without deciding
  about its bind address.
  Contributed by Adeh DeSandies ([@adevadeh](https://github.com/adevadeh)), branch `feat/configurable-bind-addr`.

### Fixed
- **`install.sh` step 7 could corrupt `~/.codex/config.toml`.** When the
  file already carried a `[features]` section without a `hooks` key, the
  merge appended a *second* `[features]` table header. TOML forbids
  declaring a table twice, so the result was a config Codex could not
  parse — which doesn't degrade Codex, it stops it, taking every
  unrelated MCP server, plugin, and trusted-project entry in that file
  down with it. Step 7 printed `updated <path>` either way. Every Codex
  install that has ever set a feature flag was in range; the machine the
  code was written on had no `[features]` section at all, so the merge
  took the `else` branch and parsed fine. The key is now inserted into
  the existing section, and — as a backstop for any section layout the
  regexes read wrongly — the merged text is parsed before it is written,
  with a loud refusal and hand-edit instructions if it would not.
  Pinned by `backend/tests/test_codex_config_merge.py`.
  Contributed by Adeh DeSandies ([@adevadeh](https://github.com/adevadeh)), branch `fix/codex-features-table`.
- **Seven live references still sent readers to `~/.claude/.mcp.json`.**
  v0.2.0 fixed the code — install.sh now registers the MCP server in
  `~/.claude.json`, which is the file Claude Code actually reads — but the
  docs were never swept, and the survivors sat in exactly the places
  someone looks when debugging a server that isn't connected: README's
  harness table, "What gets installed", and uninstall note;
  `docs/adapters.md`'s four-slot contract table; the phase-2 remedy in
  `docs/onboarding/01-install.md`; `uninstall.sh`'s on-screen hand-edit
  instructions; and `plugin/claude.home.md.template`, which `--scope
  home` installs into `~/.claude/CLAUDE.md`, teaching the dead path to
  every session in every directory. All now name `~/.claude.json`. The
  four deliberate mentions — this changelog, `docs/RELEASE-REHEARSAL.md`,
  README's upgrade note contrasting the two paths, and install.sh's
  warning comment — are unchanged, and
  `backend/tests/test_mcp_registry_path_docs.py` sweeps the tree so the
  distinction survives the next edit.
  Contributed by Adeh DeSandies ([@adevadeh](https://github.com/adevadeh)), branch `docs/claude-json-path`.
- **`recall` reported rank position in a field named `similarity`.**
  `CognitiveMemory.recall()` selects `hybrid_recall`'s score — Reciprocal
  Rank Fusion at k=60, `1/(60+vector_rank) + 1/(60+fts_rank)` — and
  published it as `Memory.similarity`, which the MCP server serializes
  straight to the model. The `recall` tool meanwhile described itself as
  "semantic similarity (fast_recall)", naming a function it does not
  call. Observed live on a fresh install: a query whose best answer was
  an exact topical match returned `"similarity": 0.01639344262295082` —
  precisely 1/61, i.e. "ranked first", carrying nothing about how well it
  matched. An agent reading a *similarity* of 0.016 concludes the store
  found nothing and discards a bullseye; two results of very different
  quality at the same rank are indistinguishable. The field is now
  `score` on both `Memory` and `MemoryPreview`, and the tool description
  states the fusion used, that the number is not a cosine similarity, and
  the two landmark values (~0.0164 one list, ~0.0328 both) that make it
  legible. `format_context()` also printed it at two decimals, collapsing
  0.0164/0.0161/0.0159 to a single "0.02"; now four.
  `PartialActivation.cluster_similarity` and `best_memory_similarity` are
  untouched — those really are cosine. Pinned by
  `backend/tests/test_recall_score_semantics.py`, including a guard that
  fails if `hybrid_recall`'s k stops being 60 and the documented numbers
  go stale.

  *Wire-format change:* anything reading `similarity` off a `recall` or
  `recall_preview` result must read `score` instead.
  Contributed by Adeh DeSandies ([@adevadeh](https://github.com/adevadeh)), branch `fix/recall-score-not-similarity`.
- **A memory stored seconds ago was labelled `-1d ago`.**
  `spontaneous-recall.py` split the UTC offset off Postgres's timestamp
  (`.split('+')[0]`), leaving a naive datetime that still held the *UTC*
  wall clock, then subtracted it from a naive *local* `datetime.now()`.
  Anywhere west of UTC that goes negative for anything recent — 18:25Z
  minus 11:25 PDT is -7h, whose `.days` is `-1` — so the age fell through
  the `days < 30` branch and printed `-1d ago`. Users east of UTC saw
  ages silently inflated instead. This is the recall header the model
  reads on *every prompt*, so it was wrong data injected into context.
  Ages are now computed as instants in UTC and clamped at zero, so clock
  skew cannot reintroduce a negative by another route. Timestamp parsing
  moved into `parse_db_timestamp()`, which normalizes Postgres's
  two-digit offset (`+00`) itself rather than relying on
  `datetime.fromisoformat`, which did not accept it until 3.11 — below
  this project's 3.10 floor. Pinned by
  `backend/tests/test_recall_age_timezone.py`, including a
  characterization test that reproduces the old arithmetic at a fixed
  UTC-7 observer so the defect is demonstrated rather than asserted.
  Contributed by Adeh DeSandies ([@adevadeh](https://github.com/adevadeh)), branch `fix/recall-age-timezone`.
- **`verify.sh` check 6 never ran the conformance suite on a normal
  install.** It probed `python3 -c "import pytest"` and ran `python3 -m
  pytest` — but `install.sh` puts the backend in a pipx venv, or in
  `~/.local/share/sill-venv`, never in the system `python3`. So the probe
  failed on every ordinary install, check 6 always took its degraded
  capture-slot branch, and the full four-slot suite went unrun during
  verification while the check still printed `pass:`. Worse, the degraded
  branch's remedy told the operator to set `SILL_PYTHON` and install into
  it, and check 6 never read `SILL_PYTHON` — following the printed advice
  exactly could not change the outcome. Check 6 now resolves the
  interpreter the way `install.sh`'s `sill_python()` does, with an
  explicit `SILL_PYTHON` winning; it names the interpreter it chose, and
  the fallback advice names that same resolved path (offering `pipx
  inject` when the install is pipx-based) instead of a shell incantation
  that only reproduces the guess. Pinned by
  `backend/tests/test_verify_conformance_interpreter.py`, including a
  test that fails if verify.sh's resolution order drifts from
  install.sh's.
  Contributed by Adeh DeSandies ([@adevadeh](https://github.com/adevadeh)), branch `fix/verify-honors-sill-python`.
- **Five readers of the renamed `similarity` field were missed.**
  `backend/memory_tools.py` kept five `m.similarity` accesses through the
  rename above, each an `AttributeError` the moment that code runs. The
  module is dead code and a shipped `py-module` both, so the smoke test
  imported it — which never executes an attribute access — and nothing
  else called it. Now reads `m.score`, with a source sweep in
  `test_recall_score_semantics.py` that fails on any shipped module still
  reading the old attribute.
- **The changelog-freshness test forbade an `Unreleased` section.** It
  read everything above the v0.1.0 heading, so the standard Keep a Changelog
  place for landed-but-unshipped work — the format this file's own header
  claims to follow — failed CI. The next change to this repo could
  therefore either go undocumented or edit a released section; the author
  of the Codex-vocabulary fixes below hit exactly that bind and cut a
  version number to escape it. The assertion is now scoped to the v0.2.0
  section, which is what it was ever really about.
  Contributed by Adeh DeSandies ([@adevadeh](https://github.com/adevadeh)), branch `fix/changelog-unreleased-section`.

Codex's tool vocabulary was derived by hand for v0.2.0. A census of 309
real Codex rollout transcripts (`~/.codex/sessions`) falsified three parts
of it — the procedure is in `docs/adapters.md` ("What's version-fragile"),
and it is worth re-running against your own sessions after a Codex upgrade.
- **Two Codex shell tools were invisible.** `shell` (314 calls in the
  census) and `shell_command` (355) were in neither the hook matchers nor
  `_SHELL_NAMES`, so `shell-idiom-guard`, `attribution-check`, and
  `state-language-check` never saw them — the same "documented as
  supported, inert in practice" failure v0.2.0 fixed for `exec`/
  `exec_command`, recurring for two more names.
- **`exec_command`'s command was unreadable.** It carries the command
  under `cmd` (1,246/1,246 real calls), not `command`, so
  `_harness.shell_command` returned `None` for every real call — every
  shell-scanning hook saw an empty command on the most-used Codex shell
  tool.
- **`shell` passes an argv list**, not a string
  (`['bash','-lc','echo === && ls']`, 314/314). A list read as "no
  command" at all. It is now rendered one element per line, which keeps a
  `-c`/`-lc` script element in command position — space-joining it would
  fabricate a shell line nobody ran and hide a real `echo =` trap from
  `shell-idiom-guard`'s (correctly) position-anchored regex.
- **`join_mcp_name` doubled the separator** for namespaces that already
  end in one: `mcp__episodic_memory__` + `search` produced
  `mcp__episodic_memory____search`. Current consumers filter by substring,
  so nothing misbehaved; an exact-name match would have missed.

### Confirmed

- `custom_tool_call`'s field shape (`call_id`/`name`/`input`), carried as
  **unverified** since the adapter work began, is confirmed by the same
  census: 1,314 records, `input` always a string, names `exec` (1,188) and
  `apply_patch` (126). Also recorded, because it is not guessable:
  `exec`'s `input` is a JavaScript program that calls other tools, not a
  shell line.


## v0.2.0 — 2026-08-05

### Upgrading from v0.1.0

**Four things bite silently if you stop at `git pull` — read this before
you do.** In order: `git pull`; `./install.sh` (idempotent — reinstalls
the backend and fixes MCP registration, items 1–2 below); `./upgrade.sh
--hooks-for /path/to/project` (backs up, migrates the schema, and
refreshes a stale Codex hook set, items 3–4 below; it runs `verify.sh`
itself at the end).

1. **The MCP server did not run on a fresh v0.2.0 install.** An unpinned
   `mcp>=1.0.0` resolved to mcp 2.x, which removed the
   `Server.list_tools()` API `sill-mcp` registers through, so it died at
   startup with `'Server' object has no attribute 'list_tools'` — while
   `verify.sh` still reported green, because that check only ran
   `sill-mcp --help`, which exits before the MCP SDK loads. Now pinned
   `mcp>=1.0.0,<2`; `verify.sh` check 2 performs a real `initialize`
   handshake instead. Existing installs may already be broken —
   reinstalling the backend (`./install.sh`) and re-running `verify.sh`
   confirms it either way.
2. **`install.sh` registered the MCP server where Claude Code never
   looks.** It wrote `~/.claude/.mcp.json`; the real user-scope registry
   is `~/.claude.json`. An install that reported success may never have
   registered the server at all. `./install.sh` is idempotent and now
   runs `claude mcp add --scope user` (or merges `~/.claude.json`
   directly if the `claude` CLI isn't on `PATH`); confirm with `claude
   mcp list` → `sill … ✔ Connected`.
3. **`--hooks-for` has only ever written `.codex/hooks.json` once**, then
   silently left it alone on every later run. A project wired for Codex
   before this release can still be running the *old* hook set there —
   which, as of this release, means missing three guards
   (`shell-idiom-guard`, `tool-type-witness`, `stored-slot-guard`) that
   never fired on Codex at all until now (see Fixed, below). Run
   `./upgrade.sh --hooks-for /path/to/project` for every project you'd
   previously wired: it diffs the current template against what's there
   and shows the diff before touching anything; add `--force-hooks` to
   apply it.
4. **Schema changes now go through a versioned migration lane**
   (`backend/migrations/`) instead of one baked-in `schema.sql` — `git
   pull` alone never touches a running database. `./upgrade.sh` backs up
   first (`backups/<db>-preupgrade-<UTC>.sql.gz`), applies pending
   migrations in order, then runs `verify.sh`. Preview with `--dry-run`.
   A fresh install never needs this step — first boot already
   initializes at the current schema level.

Also true of this release, easy to miss because nothing forces you to
read it:

- **The insight auto-store path was inert in v0.1.0** — the CLI had no
  `notice` subcommand for it to call. Wired now, off by default
  (`SILL_INSIGHT_DETECT`; see `docs/extending.md`).
- **The dreaming heartbeat mode is gone.** `--mode heartbeat` and the
  `heartbeat` Compose profile no longer exist. The beat worker
  (`--mode beat`) replaces it and is off until you configure and start
  it yourself — see `docs/beats.md`. No automatic migration if you were
  relying on the old heartbeat.
- **An onboarding runbook and a christening now exist**
  (`docs/onboarding/README.md`) — a phased walk from a working install
  to a named instance with a human-authored charter and a chosen beat
  cadence. Nothing about it requires a fresh install; an upgraded
  v0.1.0 instance can walk it too.

The full defect list is under Fixed, below — every one of them found by
this release's own clean-machine acceptance rehearsal
(`docs/RELEASE-REHEARSAL.md`). Honest limits, including what that
rehearsal could not verify, are at the end of this entry.

### Added

- **Schema migration lane** — versioned migrations under `backend/migrations/`
  (ported from the upstream agi-memory research project, 020–026), a
  `schema_migrations` tracking table stamped at initdb for fresh installs,
  `./upgrade.sh` (backup-first, `--dry-run`), verify.sh check 5, and a CI job
  asserting fresh init == baseline + upgrade (pg_dump diff).
- **Speech-act columns** — nullable `force` and `speaker` on `memories`;
  reuse-event provenance table snapshotting both at detection time.
- **session_activity** — per-project activity clock for the spontaneous-recall
  time header (hook wiring follows in a later 0.2.0 change).
- **Guards** — archive-status invariant trigger (`archived_at` authoritative);
  connect phantom-node guard (`discover_relationship` refuses ids with no
  memories row).
- **Mint path** — `sill notice` (force/speaker-tagged memory writes, required
  `--speaker`), store-written receipts (`--receipt-to` splices a mint's
  receipt into a waiting journal placeholder instead of the caller pasting
  it back by hand), and `backend/scripts/decompose_event.py` (bundles every
  speech act from one event into a single transaction so they share one
  `created_at` as the bundle key).
- **spontaneous-recall hook wiring** — the `[TIME]` header (built on
  `session_activity` above) reporting wall-clock plus within-/across-session
  gaps; a headless/detached-beat gate (`SILL_DETACHED_BEAT`,
  `SILL_INTERACTIVE`, `SILL_HEADLESS_TOOL`) that silences recall for
  non-interactive `--print` invocations; a recall sidecar so non-MCP recall
  paths stay visible to reuse tracking; access telemetry; `src=` provenance
  refs pointing at a memory's origin address in recall output.
- **track-reuse provenance detector** — evidence-based reuse detection
  sampled from a memory's body rather than its head (the head is what a
  citation reproduces), three false-positive guards (body-only sampling,
  reject a phrase shared by ≥2 recalled memories as a title not evidence,
  zero a burst of more than 3 detections in one Stop event as a citation
  sweep), and a sidecar reader so recall paths that bypass MCP tool_results
  are still tracked.
- **response-patterns fog closure** — a fail-closed `SILL_HOME_PROJECT` gate
  (unset ⇒ every project reads as home ⇒ auto-store stays log-only);
  deliberate-mint suppression so a session that already minted a row (MCP
  `remember` or the CLI's `notice`/`decompose_event`) doesn't get an
  unhedged auto-store echo of itself; a carry-forward sidecar that delivers
  a Stop hook's warnings at the start of the next prompt; tagged,
  force/speaker-stamped auto-store; comment-tolerant frontmatter parsing;
  the `authorship-attribution` response-pattern rule.
- **New hooks** — `shell-idiom-guard` (PreToolUse/Bash; denies the zsh
  `echo =word` trap — the one hook in the suite that blocks rather than
  advises); `clear-handoff` (SessionStart; re-injects the prior session's
  final assistant message after `/clear`).
- **Beat worker** — `sill-worker --mode beat`: a config-driven, alternating
  rotation of headless agent-CLI sessions (`backend/beat_worker.py`,
  `beats.json`/`backend/beats.example.json`), durable rotation state (never
  `/tmp` — a reboot must not silently reset which voice is due next),
  per-voice output verification (exit 0 is not enough; a beat that produced
  nothing does not advance rotation — the guard for a headless CLI silently
  auto-denying its own tool calls), and a worker-written spawn-clock
  transcript header the child cannot fabricate.
- **Receipt guards** — `stored-slot-guard.py` (Write/Edit; denies a
  `Stored:` line naming an id that was never actually minted) and
  `tool-type-witness.py` (Write; denies an unquoted carrying-act claim like
  "arrived by Edit"). Both are opt-in via `SILL_BEAT_JOURNAL_DIRS`, which
  `beat_worker.spawn_beat()` now derives from the loaded voice config and
  exports to every beat child automatically — zero operator configuration,
  and zero cost to a non-beat install, which never sets the variable at
  all. `state-language-check.py` reads the same variable too, but there it
  *replaces* rather than adds to its existing `journals/`/`docs/` default —
  once a beat sets it, only the derived voice directories are in scope, so
  a beat child writing under `journals/` or `docs/` directly is no longer
  checked.
- **Starter prompts** — `prompts/analyst.md` and `prompts/reflector.md`
  (the two shipped voices, craft without house-specific citations) and a
  shared `prompts/_receipt-gate.md` fragment both voices include by
  reference, so the mint-receipt protocol can't drift between them.
- **Scheduling templates** — `scheduling/com.sill.beat-worker.plist.template`
  (launchd) and `scheduling/sill-beat-worker.service.template` (systemd
  `--user`), plus `docs/beats.md`: voice config format, the full env-var
  surface, and a mandatory permissions section — ordered before the
  scheduling section on purpose — covering the failure mode where a
  headless agent CLI with no tool permissions denies every call and exits
  0, and how to verify a real beat before scheduling any.
- **Harness normalization** — `plugin/hooks/_harness.py`: one vocabulary
  (`detect`, `tool_kind`, `mcp_tool_name`, `join_mcp_name`, `shell_command`,
  `written_path`/`written_text`/`written_files`, `assistant_text`,
  `iter_transcript_tool_uses`) that every hook now asks instead of
  string-matching a harness-specific tool name. Every function is total —
  no input crashes it, including a non-dict payload or a garbage transcript
  path.
- **Four-slot adapter contract** — `docs/adapters.md` documents what
  "supports a harness" means (inject/mint/capture/track), the Claude Code /
  Codex tool-name/event/payload divergence table, the two Codex behaviors
  that aren't guessable (SHA-256 hook-command pinning in `config.toml`;
  fail-closed `PermissionRequest` reserved fields), and a runbook for
  adding a third harness. `backend/tests/test_adapter_conformance.py`
  proves each slot against both harnesses' real payload/transcript shapes
  (fixtures under `backend/tests/fixtures/`), extending — not duplicating —
  `test_notice.py`'s mint-argv proof.
- **Install scope** — `./install.sh --scope home|project` (default
  `project`, unchanged behavior): `project` keeps today's per-project
  `--hooks-for` wiring; `home` registers hooks user-scope
  (`~/.claude/settings.json`, `~/.codex/hooks.json`) plus a new ambient
  instructions file (`plugin/claude.home.md.template` → `~/.claude/CLAUDE.md`,
  merged in idempotently) so every session in every directory carries the
  Sill background. The two scopes are additive, not exclusive. An invalid
  `--scope` exits non-zero naming the valid values.
- **`upgrade.sh --hooks-for`** — re-renders and diffs a project's Codex
  hook wiring independently of any database step (`--hooks-only` skips
  docker/db entirely); a stale `.codex/hooks.json` always shows its diff
  and is left untouched unless `--force-hooks` is also passed.
- **Identity card** — `sill identity show|init|set` writing
  `$SILL_STATE_DIR/identity.json` (default `$XDG_STATE_HOME/sill/identity.json`,
  else `~/.local/state/sill/identity.json` — never `/tmp`); `name: null` is
  an explicit *not yet christened* value rather than an absence; also
  records `charter_path`, `born_at`, `engine`, `scope`, `harnesses`,
  `christened_at`. A missing or corrupt identity file degrades to a plain
  report, never a traceback. `docs/identity.md`.
- **Consent-scoped episodic backfill** — `sill backfill plan|run`: `plan`
  always prints exactly what would be read (harnesses, projects, date
  range, file counts) and writes nothing; `run` refuses without an
  explicit confirmation flag, naming it. Reuses the harness detection
  from the adapter work rather than re-deriving transcript shapes.
  `docs/onboarding/02-backfill.md`.
- **Onboarding runbook and christening** — `docs/onboarding/` (a phased
  runbook: install → harness adapters → identity card → permissions
  verified → consent-scoped backfill → supervised first beats →
  seeded-fault drill → christening → schedule) and `onboarding/`
  (`charter-prompts.md`; `to-the-one-who-wakes-here.md`, the
  christening-register letter, scheduled for re-reading at the first live
  guard refusal and the first tending review). Permissions are verified
  before any beat runs — a denied headless run exits 0 having done
  nothing — and the schedule installs last, only after supervised beats
  have demonstrably produced output. The christening captures a charter
  verbatim with a timestamp, in the human's own words, opens by saying
  plainly that the person can point their Sill at anything, and
  deliberately never says what a Sill is for.
- **Contributor tools** — `backend/memory_health.py` (TwoNN intrinsic-dimension,
  distance-concentration, and near-duplicate metrics over the memory corpus,
  with run-to-run drift detection) and `backend/ingest_md_memories.py`
  (bridges the file-based `.md` memory store into the Postgres `memories`
  table, so the spontaneous-recall hook — which only queries the DB — can
  surface distilled memories, not just the seed corpus). Contributed by
  Paul Taysom ([@taysom](https://github.com/taysom)) via pull request #3
  (`feat/memory-tools`), filed 2026-06-12 and merged 2026-08-05.

### Fixed

**Found by the clean-machine acceptance rehearsal** — full transcript,
including what it could not verify, in `docs/RELEASE-REHEARSAL.md`:

- **The MCP server did not start on a fresh install.** `mcp>=1.0.0` resolved
  to mcp 2.x, which removed the `Server.list_tools()` API the server registers
  through; it died at startup with `'Server' object has no attribute
  'list_tools'` while `sill-mcp --help` — and therefore `verify.sh` check 2 —
  still exited 0. Now pinned `mcp>=1.0.0,<2`.
- **`install.sh` registered the MCP server where Claude Code does not look.**
  Step 7 wrote `~/.claude/.mcp.json`; the user-scope registry is
  `~/.claude.json`, so `claude mcp list` reported no servers after a
  successful install. Step 7 now runs `claude mcp add --scope user` when that
  CLI is present and merges into `~/.claude.json` otherwise. The Codex half
  was correct all along.
- **`verify.sh` and `upgrade.sh` ignored `backend/.env`.** Their Compose checks
  honored it (they run from `backend/`) while their `docker exec` checks read
  `SILL_DB_CONTAINER` from the process environment — so an install that
  followed this repo's own side-by-side advice had its database checks aimed
  at whatever container was named `sill_db`. Both now load `backend/.env` with
  Compose's precedence (an exported variable still wins).
- Onboarding corrections the rehearsal forced, each re-verified by re-running
  its phase: eight `python3.10` invocations that do not exist on a clean
  machine; a phase-4 step that deleted phase 2's hook wiring; a permission
  check pinned to `--setting-sources project` when the file it writes is the
  `local` source; a fault drill whose only account of silence omitted the
  cause that actually fired; `install.sh`'s 600 s embeddings wait against a
  measured 903 s first boot; the `~300MB` model figure (measured: 1.2 GB); and
  a stale `v0.1.0` status line.

**Found by closing that rehearsal's "explicitly unverified" list** — five of
its eight items were attemptable after all, and attempting them found three
more defects (§5 of the same document):

- **Ctrl-C crashed the beat worker.** `docs/onboarding/03-first-beats.md`
  tells an operator to stop a supervised run with Ctrl-C; under a pty that
  printed an eleven-frame traceback ending in `KeyboardInterrupt`, both
  mid-beat and between beats. `run_beat_loop()` now catches it, logs which
  voice rotation is holding on, and exits 0.
- **`verify.sh` check 2 could not see a broken MCP server.** It ran
  `sill-mcp --help`, which exits before the MCP SDK is imported — the reason
  the check above stayed green throughout the `list_tools` outage. It now
  starts the server, completes a real `initialize` handshake over stdio
  within a timeout, requires a result naming this server, and reports the
  server's own error when there isn't one.
- **`sill notice` never read `backend/.env`.** Its database coordinates
  reached a beat only because `worker.py`'s module-level `load_dotenv()`
  resolves `.env` against its own directory and `beat_worker.spawn_beat()`
  forwards the whole environment to the child — so the mint worked inside a
  beat and failed from the operator's shell, and a worker started as
  `python -m beat_worker` would have minted against the default container
  (on a two-install machine, someone else's). The mint path now loads
  `backend/.env` itself, with Compose's precedence.

- `get_embedding` no longer fails on content containing literal backslashes
  (encoding-safe `convert_to` replaces the escape-interpreting `::bytea`
  cast). Diagnosed independently by contributor Paul Taysom
  ([@taysom](https://github.com/taysom)) in PR #2, filed 2026-06-09 — a
  month before this project's own independent rediscovery (2026-07-09)
  shipped here as migration 004
  (`backend/migrations/004_get_embedding_backslash_fix.sql`). Same bug,
  same fix (`convert_to(text, 'UTF8')`), independent lineage — his
  diagnosis came first.
- Access telemetry decoupled from importance (removed the compounding
  importance-on-access trigger path; `touch_memory_access` is pure telemetry).
- v0.1.0's insight auto-store path was inert (the CLI had no `notice`
  subcommand); now wired and covered by a contract test.
- Three guards never fired on Codex. They matched Claude-only tool names
  (`Bash`, `Write`, `Edit`) that Codex does not emit — it emits `exec`,
  `exec_command`, and `apply_patch`. Codex was documented as a supported
  host while `shell-idiom-guard`, `tool-type-witness`, and
  `stored-slot-guard` were inert there, and `state-language-check`'s
  `apply_patch` branch read the wrong input key so it never worked either.
- `track-reuse` joined Codex transcript MCP names without the separator
  (`mcp__sillrecall_batch`), working only because a downstream filter used
  a substring test.
- `response-patterns`' deliberate-mint suppression never engaged on Codex.
  Both mint checks walked only Claude's `assistant`/`tool_use` transcript
  shape and recognized only the tool name `Bash`, so on a Codex session
  they returned "no mint" unconditionally — every Codex-side mint (MCP
  `remember`, or `exec`/`exec_command` running `sill notice`) was
  invisible, and insight auto-store could echo a row the session had
  already deliberately stored. Now normalized through `_harness.py`.
- `response-patterns`' turn-scoped mint check stopped at the last tool
  result instead of the typed prompt (Claude writes tool results as
  `user` entries too), so a mint made earlier in the same turn, behind any
  later tool call, read as "no mint".
- A multi-file `apply_patch` was inspected only for its first file, so
  content in later files passed guards unexamined.
- `upgrade.sh` had no Codex path, so upgraded installs silently kept the
  old hook set on that harness.
- Two more v0.1.0-era env var names that survived every prior sweep:
  `sill_mcp_server.py`'s `--dsn` default read `AGI_DB_DSN` (now
  `SILL_DB_DSN`); `worker.py`'s `--mode` default read `AGI_WORKER_MODE`
  (now `SILL_WORKER_MODE`).
- The two contributor tools above (2026-08-05) had two packaging gaps that
  did not ship: neither `memory_health` nor `ingest_md_memories` was listed
  in `pyproject.toml`'s `py-modules` (the same omission `beat_worker.py` hit
  before it, commit `8be59b4`), so a `pip install` of this project would not
  have installed either module; and `memory_health.py` imports
  `numpy`, which was declared nowhere in `pyproject.toml` and is not pulled
  in transitively, so a clean install would have failed on import. Both
  modules are now in `py-modules`; `numpy` is declared under a new optional
  `diagnostics` extra (`pip install -e '.[diagnostics]'`) rather than a hard
  dependency, since `memory_health.py` is a diagnostic tool, not on the core
  memory-store path; `memory_health.py` now raises a clear install-hint
  `ImportError` instead of a bare one when numpy is absent.
- `sill-mcp` had no `--version` flag — a known issue open since v0.1.0 (see
  that release's own "Known issues" below). `sill-mcp --version` now exits
  0 and prints the installed `sill-memory` distribution's version (`dev`
  for an uninstalled checkout), sharing one lookup with the MCP
  `initialize` handshake's `server_version` so the two can't drift apart.
- `sill`'s top-level argv dispatch used `parse_known_args()` (needed so the
  `notice`/`identity`/`backfill` passthrough subcommands can forward flags
  to their own nested parsers untouched), which had the side effect of
  silently swallowing a typo'd flag on every *other* subcommand too — e.g.
  `sill seed import file.jsonl --wrogn-flag` ran as if the flag weren't
  there instead of reporting "unrecognized arguments". Extra arguments are
  now rejected the way `parse_args()` itself would, for every subcommand
  except the three documented passthroughs.
- Nine hooks (`attribution-check`, `check-agreement`, `check-corrections`,
  `goodnight-checkpoint`, `response-patterns`, `spontaneous-recall`,
  `state-language-check`, `track-reuse`, `track-verification`) called
  `.get(...)` (or, for `check-agreement`, `"key" in data`) on the parsed
  JSON payload without checking it was actually an object first. Valid
  JSON that isn't an object — a bare array, string, number, `null`, or
  bool — crashed each of them with an unhandled `AttributeError` or
  `TypeError` instead of the exit-0-and-do-nothing every hook is supposed
  to fall back to on unusable input.
- `shell-idiom-guard` — the one hook in this suite that blocks rather than
  advises — missed the zsh `echo =word` trap when it followed a leading
  per-command environment assignment (`x=1 echo =y`; bash/zsh put `echo`
  in command position there exactly as much as at the start of a line).
- A voice's `output_glob` using a recursive glob segment (`journals/**/*.md`)
  made `beat_worker.py`'s `SILL_BEAT_JOURNAL_DIRS` derivation compute a
  scope fragment containing the literal characters `**`, which the guards'
  plain substring check can never match against a real file path — silently
  disabling `stored-slot-guard`/`tool-type-witness`/`state-language-check`'s
  beat-aware scope for that voice. The shipped `beats.example.json` voices
  don't use `**`, so this was live but dormant, not visibly broken, in the
  default config.

### Changed

- response-patterns no longer emits the discarded `reason` field.

### Removed

- The legacy heartbeat worker and its Compose profile. Reflection without a
  reader goes stale; the beat worker replaces it.
- Internal `.history/` scratch migrations from the v0.1.0 extraction
  (house-internal drafts; never executed at runtime). The shipped image now
  contains only the baseline schema and the numbered migrations.
- `plugin/codex.toml.template` — dead code; `install.sh` has always inlined
  the same content as an idempotent heredoc merge instead of reading it (a
  static template can't safely replace that merge, since
  `~/.codex/config.toml` may pre-exist with unrelated content).

### Honest limits

No test in this release can prove a stranger will succeed. The
clean-machine acceptance rehearsal (`docs/RELEASE-REHEARSAL.md`) proves
the documented path works for an agent that already has this repo's
context — walked start to finish, nine documentation defects found and
fixed. What it could not do, and why, is itemized in its own §4 rather
than restated here in full; in short:

- **A charter and a name cannot be rehearsed by anything.** The
  christening's substance is prose in a person's own words. A
  placeholder exercised the mechanism; the act itself was not simulated
  and cannot be.
- **A default-named, default-ported install was not exercised end to
  end.** This project's own development machine already held the four
  default container names and three default ports, so both the
  rehearsal and this release's own testing exercised the documented
  side-by-side configuration, not the uncollided default path.
- **The systemd scheduling template has never been loaded by
  `systemctl`.** Its shape is unit-tested and its launchd counterpart is
  loaded, confirmed, and unloaded on every macOS test run, but no Linux
  machine available to this project has run it live. More generally: CI
  runs the full backend suite plus a schema/migration-identity job on
  Linux (`ubuntu-latest`, Python 3.10 and 3.12) on every push, but the
  operator-facing shell — `install.sh` end to end, hook wiring into a
  live agent harness, the beat worker — has so far only run on macOS.
- **Codex support is newer and narrower than Claude Code's.** The
  tool-name/event/payload schemas in `docs/adapters.md` were read from
  one CLI build (0.144.1); the conformance fixtures are hand-written
  against those documented schemas, not captured from a live session;
  Codex Desktop, as opposed to the CLI, is unverified entirely.
- **Cursor is out of scope for this release** (Q16.1) — the
  session-peer kit was not ported. Desktop Claude and the ChatGPT app
  keep conversations server-side with no local hook surface to wire
  into; for both, the memory store itself is the only cross-surface
  bridge.

Full transcript, every phase's done-condition, and the complete list of
what remains unverified and why: `docs/RELEASE-REHEARSAL.md`.
Phase-scoped limits not repeated above are in each plan's own PR
(#4–#9).

### Credit

- The `get_embedding` backslash fix above was diagnosed and submitted first
  by external contributor Paul Taysom in PR #2 (2026-06-09) — one week after
  receiving the v0.1.0 install, and one month before this project
  rediscovered the identical bug independently and shipped its own fix as
  `backend/migrations/004_get_embedding_backslash_fix.sql`. His `schema.sql`
  hunk (the baseline fix, not just the upgrade migration) is absorbed here
  with authorship preserved.

## v0.1.0 — 2026-06-03

Initial extraction from the agi-memory research project.

### Added

- **Backend** — Postgres 16 + pgvector + Apache AGE behind a single
  squashed `schema.sql` (43 tables, no migration history). Built into
  a Docker image that bakes the schema in at
  `/docker-entrypoint-initdb.d/`.
- **Embeddings** — HuggingFace Text Embeddings Inference container
  serving `unsloth/embeddinggemma-300m` by default. 768-dim vectors;
  dimension locked at install via `app.embedding_dimension` GUC.
- **RabbitMQ** — for inter-process messaging used by the maintenance
  worker.
- **Workers** — maintenance worker default-on; heartbeat worker
  opt-in via the `heartbeat` Compose profile (requires an LLM
  provider).
- **MCP server** — `sill-mcp` console script exposes recall,
  recall_preview, recall_batch, remember, hydrate, get_goals,
  get_drives, get_identity, get_worldview, and the chorus stage
  endpoint.
- **Console scripts** — `sill`, `sill-mcp`, `sill-worker` via
  pyproject entry points; pipx-installable.
- **Chorus library** — `chorus.stage` for multi-actor model
  orchestration via litellm (gemini, deepseek, anthropic, openai,
  ollama, …). MCP-registered as `mcp__sill__stage`.
- **Claude Code / Codex plugin** with 10 hooks:
  - `spontaneous-recall` — UserPromptSubmit; injects relevant memories
    plus optional episodic-memory excerpts.
  - `track-reuse` — Stop; updates last_reused / reuse_count when
    recalled memories appear in responses.
  - `track-verification` — PostToolUse; supports check-agreement.
  - `attribution-check` — PreToolUse on memory writes; flags
    speaker-boundary drift.
  - `check-agreement` — Stop; flags "you're right" without
    verification.
  - `check-corrections` — UserPromptSubmit; primes verify-before-agree.
  - `response-patterns` — Stop; configurable rule-file detection.
  - `state-language-check` — PreToolUse + Write/Edit; flags borrowed
    embodied-state language in persisted text.
  - `precompact-snapshot` — PreCompact; materializes orientation
    before context compaction.
  - `goodnight-checkpoint` — UserPromptSubmit; writes daily log on
    sleep-related messages. Trigger phrases configurable.
- **Response-pattern rule files** — 7 generic detection patterns
  (agreement, block-hedge, hedging, meta-deflection,
  noted-without-noting, state-language, storage-deference).
- **Seed methodology pack** — 22 probe-validated procedural/semantic
  memories across six categories (inquiry method, epistemic
  discipline, memory hygiene, recall patterns, tool selection,
  identity-shape). Auto-imported on first install via
  `sill seed import`.
- **Install / verify / reset / uninstall scripts** — `./install.sh`
  with `--no-seed`, `--hooks-for <project>`, `--dry-run` flags;
  smoke tests via `./verify.sh`; rollback via `./reset.sh` and
  `./uninstall.sh --keep-data`.
- **Documentation** — `README.md`, `docs/concepts.md`,
  `docs/hooks.md`, `docs/extending.md`.

### Environment

All hooks read `SILL_*` env vars with sensible defaults:
`SILL_DB_CONTAINER`, `SILL_DB_USER`, `SILL_DB_NAME`,
`SILL_PROJECT_ROOT`, `SILL_PLUGIN_DIR`, `SILL_EPISODIC_MEMORY_PATH`,
`SILL_GOODNIGHT_FOCUS_DRIVE`, `SILL_FAKE_EMBEDDINGS` (test only).

### Soft dependencies

- [episodic-memory](https://github.com/obra/superpowers-plugins) —
  conversation-transcript archive; `spontaneous-recall` falls back
  to sill-only if absent.

### Known issues

See `extraction/known-issues.md` in the upstream agi-memory worktree
for the v0.1.0 punch list. Summary:

- `sill` CLI does not auto-load `backend/.env` (workaround: run
  from `backend/` or source `.env` first).
- `sill-mcp` has no `--version` flag.
- Embeddings cold boot logs "404 Not Found" lines from the Candle
  backend — alarming but harmless.

### Credit

Sill v0.1.0 is extracted from the agi-memory research project
(William Taysom + Sili). Schema and recall design have been in
continuous use since late 2025; this release packages a sanitized
core suitable for fresh installs.
