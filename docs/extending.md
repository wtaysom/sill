# Extending Sill

How to write good memories, add your own hooks, customize the shipped
rules, and tune the env-var knobs.

---

## Writing good memories

The single best mental model for working with Sill:

> **Memory retrieval is in-context programming.** Each recalled
> memory opens certain pathways and closes others in the frozen
> weights. Dense semantic content (verbatim quotes, concrete
> examples, named entities) makes rich pathway nodes. Abstract
> pointers ("discusses X", "argues that Y") are dead ends — they
> tell the model a topic exists without giving it material to think
> with. Memory quality matters more than memory quantity.

Concretely, this means:

- **Quote, don't summarize.** A memory that contains
  `"X is wrong because Y" — Alice, 2025-12-01` steers thinking. A
  memory that says `Alice argued against X` doesn't.
- **Name entities.** Use actual project / file / function names.
  Embeddings cluster real names; "the recall module" embeds like
  noise.
- **Include the falsifier.** If you can write `would be revised
  if Z`, write it. It makes the memory testable instead of
  load-bearing-by-vibe.
- **Resist restating-the-obvious.** If the memory is `we should
  verify before agreeing`, you've just restated CLAUDE.md. Skip
  it; rely on the methodology pack.

### The quality gate

Seed memory #7 of `seed/methodology.jsonl`:

> Before storing a memory, check the draft has at least one of:
> 1. a verbatim quote with source,
> 2. a falsifier ("this would be wrong if…"),
> 3. a concrete example.
>
> Stable preferences/decisions are OK without these but should
> name what they're a preference *over*. If the draft is just a
> restated observation with none of the above, ask before storing.

Run this gate in your head before every `remember` call. If you
notice yourself drafting `I should store this insight that we
agreed to verify` — don't. Either find the quote behind the
insight, or skip the store.

### Storage triggers

Seed memory #6:

> When the user states a generalizable principle, distinction, or
> critique of your behavior that you'd want a future instance to
> act on — and a quick `recall_preview` suggests it isn't already
> in the corpus — store it before continuing. Skip preferences,
> examples, and one-off corrections. Bar: "a future instance
> making the same mistake would be prevented by this".

The most common storage failure mode isn't over-storing or
under-storing — it's *talking about* storing without storing. The
`noted-without-noting.md` response-pattern rule catches that.

---

## Adding your own hooks alongside Sill's

Claude Code's hook system is additive within an event. If you have
your own `Stop` hook and Sill has two, all three run (in array
order). The composition rules:

1. Hooks in the **same event** run independently — none can block
   another.
2. Sill's hooks emit `additionalContext` / `systemMessage` and
   exit 0 — they never abort.
3. If you write a blocking hook (exit non-zero with
   `permissionDecision: "deny"`), you'll abort the tool call but
   not Sill's hooks for that event.

**Where to put them.** Two options:

- **Per-project**, in `<project>/.claude/settings.local.json` or
  `<project>/.codex/hooks.json`. Sill's installer merges into
  these files idempotently; adding your own entries alongside is
  safe.
- **Globally**, in `~/.claude/settings.json` (and `~/.codex/hooks.json`
  for Codex). If you ran `./install.sh --scope project` (the default),
  Sill never touches these files — add your own hooks there freely. If
  you ran `./install.sh --scope home`, Sill's own hooks live there too
  (merged idempotently, same as the per-project case); your entries and
  Sill's coexist the same way they would in a project's
  `settings.local.json`. See the "Install scope" section below.

### Install scope: `--scope home` vs `--scope project`

`./install.sh`'s `--hooks-for <path>` (project scope, the default) and
`--scope home` are two different answers to "where do Sill's hooks run,"
not two ways of writing the same thing — the tradeoff is real in both
directions:

- **`--scope project`** (default): hooks go into one project's
  `.claude/settings.local.json` and `.codex/hooks.json`. Narrower blast
  radius, no cross-project mixing — but every new project needs its own
  `--hooks-for` run, and only wired projects get recall/guards.
- **`--scope home`**: hooks go into `~/.claude/settings.json` and
  `~/.codex/hooks.json`, plus an ambient instructions file
  (`~/.claude/CLAUDE.md`, from `plugin/claude.home.md.template`) so every
  session in every directory carries the Sill background. In exchange:
  every prompt in every project pays the recall hook's latency, and any
  project's work can reach the one store.

`--scope home` and `--hooks-for <path>` are additive, not exclusive — you
can register home-scope hooks and also give one project its own
project-scoped entries in the same run.

**Avoiding matcher conflicts.** Sill's `PreToolUse` matchers
target memory-storage tools and (for state-language-check)
`Bash|exec|exec_command|shell|shell_command|apply_patch|Edit|Write` — the union of Claude's
and Codex's tool names for the same shell/write/edit actions (see
`_harness.py`). If your hook uses a narrower or
unrelated matcher, there's no conflict. If you want to gate one
of Sill's hooks off for a specific tool, the cleanest path is
to copy the relevant entry, edit the matcher, and disable the
shipped one — don't try to "subtract" matchers.

**Example layout** for a project with your own hook and Sill's:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {"type": "command", "command": "python3 /path/to/my-hook.py"}
        ]
      },
      {
        "hooks": [
          {"type": "command",
           "command": "python3 /Users/you/.claude/plugins/local/sill-plugin/hooks/response-patterns.py"},
          {"type": "command",
           "command": "python3 /Users/you/.claude/plugins/local/sill-plugin/hooks/track-reuse.py"}
        ]
      }
    ]
  }
}
```

The second entry is what `./install.sh --hooks-for` writes. The
first is yours. Both run on every Stop.

---

## Adding your own response-pattern rules

The `response-patterns` hook reads every `*.md` file in the rules
directory and treats each as an independent check.

### Option A: drop into the shipped directory

Add `plugin/response-patterns/my-rule.md`:

```yaml
---
name: my-rule
enabled: true
patterns:
  - "regex one"
  - "regex two"
---

Warning message body. `{matched}` interpolates the matched text.

**Corrective action:** what the agent should do about it.
```

It'll be picked up on the next Stop event. The shipped files are a
fair set of examples to copy from (`agreement.md` for a simple
keyword list; `block-hedge.md` for regex with backreferences).

Note: this directory is inside the symlinked plugin, so files you
add here will travel with the install. If that's a problem, use
option B.

### Option B: point the hook at your own directory

```bash
export SILL_RESPONSE_PATTERNS_DIR=/path/to/my/rules
```

The hook reads only that directory. You can copy the shipped rules
across selectively, or start from scratch.

### Disabling a shipped rule

Edit its frontmatter:

```yaml
---
name: agreement
enabled: false
patterns: …
---
```

The hook checks `enabled` before evaluating patterns; the rule is
skipped without being deleted.

---

## Customizing goodnight-checkpoint triggers

`plugin/config/goodnight-triggers.txt`:

```
# Lines starting with # are comments.
goodnight
good night
going to sleep
going to bed
off to bed
calling it a night
signing off
see you tomorrow
```

One trigger per line. Matched as substrings, case-insensitive,
word-bounded.

To add a trigger: append a line. To remove: comment it out or delete
it. The hook re-reads the file every fire, so no restart needed.

To use a different file, point `SILL_PLUGIN_DIR` at a directory
that has a `config/goodnight-triggers.txt`.

To suppress the focus-update side effect, leave
`SILL_GOODNIGHT_FOCUS_DRIVE` unset.

---

## Env-var cheat sheet

The harness-adapter work (`plugin/hooks/_harness.py`, the Codex matcher
fixes, the four-slot contract in `docs/adapters.md`) introduces **no new
env vars** — harness detection is automatic (`_harness.detect()`, keyed on
Codex's `turn_id`; see `docs/adapters.md`'s normalization vocabulary), not
configured. The one new flag from that work, `--scope home|project`, is a
CLI flag to `install.sh`/`upgrade.sh`, not an environment variable — see
"Install scope" above. Confirm every `SILL_*` name actually read via
`os.environ`/`os.getenv` in this codebase yourself:

```bash
grep -rhoE "os\.environ(\.get)?\(['\"]SILL_[A-Z_]+|os\.getenv\(['\"]SILL_[A-Z_]+" \
  plugin/ backend/*.py backend/scripts/*.py | grep -oE "SILL_[A-Z_]+" | sort -u
```

Every name that command prints appears somewhere in this cheat sheet,
with two pre-existing exceptions unrelated to this plan — `SILL_DB_HOST`
and `SILL_DB_PORT`, read only by `backend/scripts/seed_import.py` as
fallbacks tried before `POSTGRES_HOST`/`POSTGRES_PORT`.

### Database connection

| Var                  | Default        | Used by |
|----------------------|----------------|---------|
| `SILL_DB_CONTAINER`  | `sill_db`      | `recall_lib`, `spontaneous-recall`, `precompact-snapshot`, `goodnight-checkpoint`, `backend/sill.py` (`notice`), `backend/scripts/decompose_event.py`, the install/verify scripts |
| `SILL_DB_USER`       | `sill`         | same set as `SILL_DB_CONTAINER` |
| `SILL_DB_NAME`       | `sill`         | same set as `SILL_DB_CONTAINER` |
| `POSTGRES_HOST`      | `localhost`    | `track-reuse` (direct `psycopg2`, not `docker exec`) |
| `POSTGRES_PORT`      | `5432`         | `track-reuse` |
| `POSTGRES_DB`        | `sill`         | `track-reuse` |
| `POSTGRES_USER`      | `sill`         | `track-reuse` |
| `POSTGRES_PASSWORD`  | `sill_password`| `track-reuse` |

Every one of these already matches what `backend/docker-compose.yml`
and `backend/.env.example` provision by default — there is no
mismatch to work around on a fresh install (earlier drafts of this
doc described one; it was fixed in the hooks' own code before it was
fixed here). Confirm for yourself:

```bash
grep -rn "agi_user\|agi_db\|agi_password\|agi_memory" plugin/ backend/*.py backend/scripts/*.py
# -> no output
```

Only override these if you renamed the container, user, or database
away from the shipped defaults (e.g. running a second Sill instance
side by side):

```bash
export SILL_DB_CONTAINER=sill_db_test
export SILL_DB_USER=sill_test
export SILL_DB_NAME=sill_test
export POSTGRES_DB=sill_test
export POSTGRES_USER=sill_test
export POSTGRES_PASSWORD=sill_test_password
```

### Paths

| Var                          | Default                                | Purpose |
|------------------------------|----------------------------------------|---------|
| `SILL_PROJECT_ROOT`          | `cwd`                                  | Where attribution-check looks for sessions, where goodnight-checkpoint writes daily logs, where precompact-snapshot writes orientation |
| `SILL_PLUGIN_DIR`            | parent of hooks dir                    | Where response-patterns and goodnight-triggers config files live |
| `SILL_RULES_DIR`             | `<root>/.claude/rules`                 | Where precompact-snapshot writes |
| `SILL_ORIENTATION_OUTPUT`    | `<rules>/sill-orientation.generated.md`| Override the orientation file path entirely |
| `SILL_ORIENTATION_TITLE`     | `Sill Orientation`                     | H1 title in the orientation file |
| `SILL_RESPONSE_PATTERNS_DIR` | `<plugin>/response-patterns`           | Where the response-patterns hook reads rule files from |
| `SILL_BEAT_SESSIONS_DIR`     | `<root>/docs/gnomon-sessions`          | Where attribution-check looks for `beat-NNN-*.md` files |
| `SILL_RESEARCH_MANIFEST`     | `<root>/docs/research-manifest.json`   | Optional manifest read by precompact-snapshot |
| `SILL_LOG_DIR`               | `/tmp`                                 | Where every hook's `.log` file lives, plus every sidecar/state file: `recall-sidecar-<session>.jsonl`, `response-patterns-last-<session>.json`, `response-patterns-data.jsonl`, `auto-stored-insights.jsonl`, `verification-state.json`, `cc-session-by-pid/` |

Note `SILL_BEAT_SESSIONS_DIR` above is unrelated to the beat worker below
despite the name — it's attribution-check's F1 check looking for
`beat-NNN-*.md` citation targets, a different, older feature.

### Beat worker (`sill-worker --mode beat`)

Full field-by-field and worked-example coverage lives in `docs/beats.md`;
this is the flat env-var reference. None of these are set by `install.sh`
— the worker reads them fresh at import/spawn time, and the scheduling
templates under `scheduling/` set the ones worth pinning explicitly for an
unattended install.

| Var | Default | Purpose |
|---|---|---|
| `SILL_BEAT_CONFIG`           | `beats.json` at the project root                                              | Voice definitions — see `backend/beats.example.json`. |
| `SILL_BEAT_INTERVAL_SECONDS` | `7200` (2h)                                                                    | Time between beats. |
| `SILL_BEAT_TIMEOUT_SECONDS`  | `1800` (30min)                                                                 | Per-beat subprocess wall-clock cap. |
| `SILL_BEAT_STATE_PATH`       | `$XDG_STATE_HOME/sill/beat-rotation.json`, else `~/.local/state/sill/beat-rotation.json` | Rotation index — **never `/tmp`**. |
| `SILL_BEAT_LOG_PATH`         | `$SILL_LOG_DIR/beat-worker.log`                                                | The worker's own log. |
| `SILL_BEAT_CLI`              | `claude`                                                                       | The agent CLI executable to spawn. |
| `SILL_BEAT_WATCHDOG_SOCKET`  | unset                                                                          | Ping this socket before every spawn; log-only, unset skips the probe. |
| `SILL_BEAT_POST_HOOK`        | unset                                                                          | Command run after a successful beat; log-only, never fails the beat. |
| `SILL_BEAT_JOURNAL_DIRS`     | unset; **the worker sets it itself** on every beat child                      | Scope for `stored-slot-guard.py` / `tool-type-witness.py` (opt-in — unset means they check nothing) and `state-language-check.py`, where it **replaces** — not adds to — the `journals/`/`docs/` default. Consequence: with the shipped `beats.example.json`, the derived scope is `notes/:logs/analyst/:journal/:logs/reflector/`, so once a beat sets this, a beat child writing under `docs/` or `journals/` directly is *not* state-language-checked — only the derived voice directories are. Derived by `beat_worker.journal_dirs_for_voices()` from the loaded `beats.json`: every voice's `output_glob` directory plus its `transcripts` dir, colon-joined. You should not set this by hand — the worker's startup log prints the value it derived. |

`SILL_DETACHED_BEAT` is also set by the worker on every beat child, but is
documented below under the headless/interactive gate, since that's the
hook logic that actually reads it.

### Headless / interactive gate (spontaneous-recall)

Read fresh on every `UserPromptSubmit`; none of these are set by
`install.sh` — they're for the caller (a scheduler, a wrapping
front-end) to set on the child session it spawns.

| Var                   | Default | Purpose |
|------------------------|---------|---------|
| `SILL_DETACHED_BEAT`   | unset   | `1` = an authoritative "this session is headless" flag a scheduler sets on every child it spawns. Silences recall injection and the `[TIME]` header's clock-advance. |
| `SILL_INTERACTIVE`     | unset   | `1` = override back to interactive even though the entrypoint looks headless (an `sdk*` `CLAUDE_CODE_ENTRYPOINT`) — for a front-end that drives `--print` on behalf of a real person. |
| `SILL_HEADLESS_TOOL`   | unset   | `1` = explicit "be quiet" that always wins, regardless of the other two. |

The gate defaults to interactive and excludes only the known-headless
family (blacklist, not whitelist), so a front-end this project has
never heard of is treated as a human by default. See
`docs/hooks.md`'s spontaneous-recall section for the full precedence
order.

### Mint-path auto-store (response-patterns)

| Var                    | Default                                | Purpose |
|-------------------------|-----------------------------------------|---------|
| `SILL_HOME_PROJECT`    | unset                                   | Names the one project where insight auto-store is **suppressed** — the project that mints deliberately, where an auto-store would only echo. Every other project is eligible. **Fail-closed**: unset means every resolvable cwd reads as home, so auto-store stays log-only everywhere until you configure this. |
| `SILL_SPEAKER_SELF`    | `instance`                              | The `--speaker` value stamped on an auto-stored memory. Rename it once you've christened the running instance. |
| `SILL_INSIGHT_DETECT`  | `0` (off)                               | `1` turns on the local-model insight detector. Leave off unless you have a model reachable at `SILL_OLLAMA_URL` — otherwise every Stop event pays a timeout for nothing. |
| `SILL_CLI`             | `sill`                                  | The command the auto-store path shells out to (`$SILL_CLI notice ...`). Point it at a full path if `sill` isn't on the hook's PATH. |
| `SILL_OLLAMA_URL`      | `http://localhost:11434/api/generate`   | Only consulted when `SILL_INSIGHT_DETECT=1`. |
| `SILL_OLLAMA_MODEL`    | `gemma3:12b`                            | Only consulted when `SILL_INSIGHT_DETECT=1`. |

### Integrations

| Var                          | Default | Purpose |
|------------------------------|---------|---------|
| `SILL_EPISODIC_MEMORY_PATH`  | unset   | Path to the episodic-memory CLI binary. Unset = spontaneous-recall skips episodic. |
| `PERSONAL_SUPERPOWERS_DIR`   | `~/.config/superpowers` | Where archived conversations live, for episodic recall. |

### Hook-specific

| Var                            | Purpose |
|--------------------------------|---------|
| `SILL_GOODNIGHT_FOCUS_DRIVE`   | Name of a `drives` row to update with a one-line summary on goodnight. Unset = skip. |
| `SILL_FAKE_EMBEDDINGS`         | Test-only. `1` / `true` / `yes` makes `sill seed import` use deterministic fake embeddings instead of calling the embeddings service. Useful for CI / fast roundtrip tests. |

### Container names (Docker Compose overrides)

| Var                                | Default                    |
|------------------------------------|----------------------------|
| `SILL_EMBEDDINGS_CONTAINER`        | `sill_embeddings`          |
| `SILL_RABBITMQ_CONTAINER`          | `sill_rabbitmq`            |
| `SILL_MAINTENANCE_WORKER_CONTAINER`| `sill_maintenance_worker`  |

Useful when running multiple Sill stacks on the same host.

---

## A note on the upstream

Sill was extracted from agi-memory and still carries some upstream
shape — `attribution-check`'s F2 patterns hardcode the origin
project's own example names rather than a portable placeholder (see
`docs/hooks.md`'s attribution-check section for how to adapt it),
the F1 path only fires when a `docs/gnomon-sessions/` directory
exists, and the worldview/identity tables that most installs won't
touch. The database-credential mismatch a previous version of this
doc warned about here is gone — every hook has defaulted to `sill` /
`sill` for a while now (see the "Database connection" table above).
None of the above is harmful, but it's worth knowing about when
something doesn't quite line up with what's documented here.

The methodology pack itself, by contrast, is written to be portable.
Twenty-two memories about inquiry, verification, recall patterns,
attribution, and storage hygiene that the upstream project converged
on over six months. You can read the whole thing at
`seed/methodology.jsonl`; it's plain JSONL.


## Prompt and write recall (0.3)

Both callbacks use `scripts.recall_core` for fusion, previous-turn extraction,
focused snippets, content hydration and exposure accounting. Scores retain
their source SQL meaning; the fast lane floor is query cosine, not the blended
fast score. Prompt settings `SILL_RECALL_MAX_MEMORIES` (5), `SILL_RECALL_MAX_CONVERSATIONS` (2),
`SILL_RECALL_MIN_QUERY_LENGTH` (20), `SILL_RECALL_MIN_SIMILARITY` (0.25), and
`SILL_RECALL_QUIET` remain supported. A zero count disables that source.
Quiet mode preserves model context, time and visible store failures. Prior
context comes only from the event's own readable transcript, otherwise empty.

The default `PostToolUse` registration matches `Write|Edit|MultiEdit|apply_patch`
with the installed Python interpreter and a 30-second host timeout. Set
`SILL_WRITE_RECALL=0` to disable it. Detached/headless calls, wrong events,
missing session identity, ignored logs/data paths, and fewer than 80 stripped
added characters do not retrieve. One callback selects the first eligible
unseen canonical path, takes at most 1,500 added characters from that file,
runs one fused lookup with candidate limit 10, and injects at most three IDs
not previously shown in that session. Later eligible files wait for a later
callback. Output is only `hookSpecificOutput.additionalContext` for
`PostToolUse`; it contains readable text, four-decimal scores, IDs and available
source metadata. No exposure refreshes retrieval recency or records a reuse.

Write recall has one absolute monotonic 25-second deadline: up to 17 seconds
for fusion, hydration until second 23, accounting until second 25. Prompt
recall shares 40 seconds under its 45-second registration, including session
bookkeeping and the optional episodic CLI; fusion is capped at 15 seconds.
`fused_recall`, `full_contents`, and `record_exposure` accept an optional
keyword-only `deadline` (absolute `time.monotonic()`), preserving existing
limits when omitted. Production transports take the smaller remaining budget
and their existing timeout; no uncancellable timeout thread is added.

All DB hooks honor an explicit `SILL_INSTANCE_ENV_FILE` exclusively, using
the common instance parser. Invalid selection never falls back to inherited
coordinates or a default container. Unavailable stores produce a recall
warning or unchecked receipt/reuse/checkpoint signal, with redacted errors.
Legacy routing remains when no instance file is selected.

State lives under `SILL_LOG_DIR`. A canonical lowercase UUID is its filename
key verbatim; any other nonblank session string becomes `sha256-<hex>`.
Missing identity keeps prompt state request-local and disables write recall.
The public journal remains `recall-sidecar-<key>.jsonl`, with UTC ISO `ts`,
`source` (`spontaneous-recall` or `write-recall`), and `memories` containing
`id` and the shown `content`. Reuse reads only this session, within five
minutes, and never reads a global recent bucket. Stop carry-forward uses the
same key convention.

A per-session `flock` bounds lock waits to 0.5 seconds. Paths are leased with
random ownership tokens and an epoch expiry 35 seconds later; a failed lookup
releases only its own lease, and interrupted callbacks expire. Fresh rows are
chosen again under the lock before emission. State and the journal use atomic
replacement; the journal retains at most 512 entries. The dedupe index keeps
up to 8,192 shown IDs and 4,096 completed paths; reaching either cap disables
new write lookups for that session instead of forgetting older IDs or paths.
Each atomic replacement has one fixed temporary filename under the lock, so
repeated interrupted writes do not accumulate orphan temporary files.
Like any stdout callback, a process killed between output and journal commit
cannot provide transactional exactly-once delivery to the host. Host receipt
is not acknowledged by this protocol.

The optional response-pattern insight mint invokes `sill notice`. This indirect
DB path also honors explicit selection and skips adjacent `.env` loading when
a selector is present, including empty/invalid selectors. The shared private
transport's `row_arrays=True` mode returns complete JSON arrays from selected
child queries, retaining all columns and nulls without wrapping or altering a
top-level data-modifying CTE. Only the legacy mint boundary converts a null to
its historical empty-field representation. Default recall JSON transport and
unselected CLI behavior remain unchanged. Write recall uses the same configured
fast cosine floor as prompt recall.
