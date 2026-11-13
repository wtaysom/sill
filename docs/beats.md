# Beats

The beat worker is Sill's reflective loop: a config-driven rotation of
headless agent-CLI sessions, each running its own standing prompt, each
producing its own record of what it did. It ships **off by default** — see
"Permissions" below before you turn it on, and "Scheduling" at the end for
running it unattended.

---

## What a beat is

A beat is one bounded unit of work: `backend/beat_worker.py` spawns a
single `claude --print -p "<standing prompt>"` subprocess for the next
voice due in the rotation, waits for it to exit (or time out), checks
whether it actually produced anything, records a transcript, and — only on
verified success — advances the rotation to the next voice. Then it sleeps
until the next interval and does it again, forever.

"Verified success" is exit 0 **and** (the voice declared no `output_glob`,
or its `output_glob` gained a new file). Exit 0 alone is not enough — see
Permissions below for why.

There is no interactive user on the other end. A beat's standing prompt
has to tell it to decide and act on its own, because anything that would
normally wait for a human to answer — a permission prompt, a clarifying
question — has nowhere to go.

## The five-step arc

Sill's two starter voices (`prompts/analyst.md`, `prompts/reflector.md`)
each structure their beat the same way, adapted to their own register:

1. **Orient** — read your own last few entries, glance at the other
   voice's recent output, recall around whatever you're about to work on
   if memory tools are connected.
2. **Decide** — pick one concrete piece of work, name what mode you're
   running in, write down what you chose and why *before* acting.
3. **Act** — do the work; test it against something real.
4. **Store** — if you found something worth keeping, store it as a memory
   with source, speaker, and at least one concept tag.
5. **Log** — write a note recording what happened, for the next beat
   (yours or the other voice's) to read.

This structure is generic craft, not a fixed protocol the worker enforces
— it lives entirely in the standing prompts, which you're free to rewrite.
The two shipped voices differ in what they're *for*: the analyst measures,
tests, and builds; the reflector actually thinks about something without
needing to produce an artifact. Neither performs the other's job.

## Voice config format

`SILL_BEAT_CONFIG` (default `beats.json` at the project root) is a JSON
object with one top-level `voices` array. Each entry:

| Field          | Required | Meaning |
|-----------------|----------|---------|
| `name`          | yes      | Identifies the voice in logs and transcript filenames. |
| `prompt`        | yes      | Path (relative to the project root) to the voice's standing prompt. |
| `transcripts`   | yes      | Directory (relative to the project root) where this voice's per-beat transcripts are written. Created automatically if missing. |
| `output_glob`   | no       | A glob (relative to the project root) checked before and after the beat. If set, the beat only counts as successful if this glob gained a new file — see Permissions below. Omit it and the beat is judged on exit code alone. |
| `kickoff`       | no       | One line appended after the standing prompt to start the beat. Defaults to `"Begin."`. |

### Worked example

`backend/beats.example.json` ships two neutrally-named voices — copy it to
`beats.json` at the project root and edit from there:

```bash
cp backend/beats.example.json beats.json
```

```json
{
  "voices": [
    {
      "name": "analyst",
      "prompt": "prompts/analyst.md",
      "transcripts": "logs/analyst",
      "output_glob": "notes/analyst-*.md",
      "kickoff": "Run one beat now. You are running detached — there is no interactive user. Decide and act; do not attempt to elicit direction."
    },
    {
      "name": "reflector",
      "prompt": "prompts/reflector.md",
      "transcripts": "logs/reflector",
      "output_glob": "journal/reflector-*.md",
      "kickoff": "Run one beat now. You are running detached — there is no interactive user. Follow your own thread and write down what you find."
    }
  ]
}
```

The rotation alternates `analyst`, `reflector`, `analyst`, … in array
order. `prompts/analyst.md` and `prompts/reflector.md` (plus
`prompts/_receipt-gate.md`, which both include — see the receipt gate
below) already exist in this repo; the `notes/`, `journal/`, and `logs/`
directories they reference don't ship pre-created:

```bash
mkdir -p notes journal logs/analyst logs/reflector
```

## Env-var reference

| Var | Default | Meaning |
|---|---|---|
| `SILL_BEAT_CONFIG` | `beats.json` at the project root | Voice definitions (see above). |
| `SILL_BEAT_INTERVAL_SECONDS` | `7200` (2h) | Time between beats. Each voice fires roughly every *N* × interval, *N* = number of voices. |
| `SILL_BEAT_TIMEOUT_SECONDS` | `1800` (30min) | Elapsed-time cap on one beat's subprocess. On timeout the worker kills the agent CLI's process group, logs the attempt as skipped, and retries the same voice on the next run. |
| `SILL_BEAT_STATE_PATH` | `$XDG_STATE_HOME/sill/beat-rotation.json`, else `~/.local/state/sill/beat-rotation.json` | Where the rotation index persists. **Never `/tmp`** — the upstream lesson this guards against is a reboot silently clearing `/tmp` and resetting rotation with no record it happened. |
| `SILL_BEAT_LOG_PATH` | `$SILL_LOG_DIR/beat-worker.log` | The worker's own log (separate from any one voice's transcripts). |
| `SILL_BEAT_CLI` | `claude` | The agent CLI executable to spawn. Point it at a full path if it isn't on the PATH your scheduler uses. |
| `SILL_BEAT_WATCHDOG_SOCKET` | unset | If set, ping this Unix socket before every spawn and log a warning on failure. Log-only — never blocks or repairs anything. Unset skips the probe entirely. |
| `SILL_BEAT_POST_HOOK` | unset | A shell command run after every *successful* beat (e.g. housekeeping). Log-only, keyed on exit code, never fails the beat that triggered it. |
| `SILL_DETACHED_BEAT` | unset (worker sets it to `1` on every child) | The authoritative headless flag Sill's own hooks gate on — `spontaneous-recall` and `check-corrections` read this to know not to inject recall or advance any interactive-session clock. You don't set this yourself; the worker always does. |
| `SILL_BEAT_JOURNAL_DIRS` | unset (worker derives and sets it on every child) | Scope for `stored-slot-guard.py`, `tool-type-witness.py`, and `state-language-check.py`'s beat-aware fallback — the directory part of every voice's `output_glob` plus every voice's `transcripts` dir, colon-joined. **You don't set this either.** `spawn_beat()` computes it fresh from your loaded `beats.json` on every spawn; the worker's startup log prints the derived value (`Guard scope (SILL_BEAT_JOURNAL_DIRS for each child): ...`) so you can see exactly what it resolved to. |

`SILL_PROJECT_ROOT` (documented in `docs/extending.md`'s Paths table)
matters here too: it's where the worker looks for `beats.json` and
resolves every voice's relative paths against. Unset, it defaults to
whatever directory you launch the worker from — the scheduling templates
in `scheduling/` set it explicitly so a launchd/systemd invocation isn't
at the mercy of its working directory.

## Running one beat by hand

From the project root, with `beats.json` and the directories it references
already in place, run the voice currently due:

```bash
sill-worker --mode beat --once
```

The command runs one attempt and exits immediately afterward. Exit status
`0` means the attempt passed the output check and advanced rotation.
Exit status `1` means the attempt failed or was interrupted. Exit status
`75` means another worker owns the rotation state and no agent CLI was
started.

To supervise the scheduled loop in the foreground, omit `--once`. The
loop runs the first attempt immediately and uses the configured interval
for subsequent attempts. Ctrl-C or SIGTERM stops the worker.

```bash
SILL_BEAT_INTERVAL_SECONDS=1 sill-worker --mode beat
```

Expected output starts with a startup banner naming every voice and the
derived guard scope, for example:

```
2026-08-04 16:01:12,098 - INFO - Starting beat worker
2026-08-04 16:01:12,098 - INFO -   Project root: /path/to/your/project
2026-08-04 16:01:12,098 - INFO -   Config: /path/to/your/project/beats.json
2026-08-04 16:01:12,098 - INFO -   Interval: 1s (0.0h)
2026-08-04 16:01:12,098 - INFO -   Beat timeout: 1800s
2026-08-04 16:01:12,098 - INFO -   Rotation state: ~/.local/state/sill/beat-rotation.json
2026-08-04 16:01:12,098 - INFO -   Voice [analyst]: prompt=prompts/analyst.md, transcripts=logs/analyst
2026-08-04 16:01:12,098 - INFO -   Voice [reflector]: prompt=prompts/reflector.md, transcripts=logs/reflector
2026-08-04 16:01:12,098 - INFO -   Guard scope (SILL_BEAT_JOURNAL_DIRS for each child): 'notes/:logs/analyst/:journal/:logs/reflector/'
```

then, a few seconds to a few minutes later, one line telling you whether
the first beat was a **verified success**:

```
2026-08-04 16:01:12,319 - INFO - [analyst] Beat complete in 0.2s — transcript 20260804-160112.txt
```

or the **silent-failure warning** the Permissions section below exists to
prevent — read that section immediately if you see this:

```
2026-08-04 16:00:50,600 - WARNING - [analyst] Beat exited 0 in 0.3s but produced no file matching 'notes/analyst-*.md' — the agent CLI may be denying tools non-interactively; see docs/beats.md permissions. Rotation remains on [analyst].
```

No `beats.json` at all fails loudly (a Python traceback naming the missing
path) rather than silently — that's a config problem to fix, not the
silent-failure mode this doc is about.

## Worker ownership and cleanup

Each resolved rotation state path has one worker owner. The scheduled
loop holds a lock across attempts and interval sleep. A one-shot worker
uses the same lock, as do `python -m beat_worker` and calls to
`spawn_beat()`. Set a distinct `SILL_BEAT_STATE_PATH` for each independent
installation. Leave the sibling `.lock` file in place across restarts. The
kernel releases the lock when the last process holding its descriptor
closes it. The agent CLI inherits the descriptor so it can retain
ownership if the worker parent is killed.

The worker starts the agent CLI in its own process group. On timeout,
Ctrl-C or SIGTERM it kills that group and reaps the agent CLI. Processes
that move to another group fall outside this cleanup.

## Reading a transcript

Each beat writes one file under its voice's `transcripts` directory,
named `<YYYYMMDD-HHMMSS>.txt`. A real one looks like this:

```
# Analyst beat
# Spawned: 2026-08-04T16:01:12-07:00 — worker-written wall-clock receipt at child spawn; the child cannot fabricate this line
# Timestamp: 2026-08-04T16:01:12.319462
# Duration: 0.2s
# Exit code: 0
# Model: unknown (best-effort mtime match on session transcript)
============================================================

Wrote a note and stored a memory.
```

**The spawn-clock header.** `# Spawned: ...` is written by the *worker*,
not the child — it's a wall-clock receipt of when the subprocess was
launched, stamped by a process the child has no way to influence. A
child's own claims about timing, made in its own reply text, can't be
checked against anything else; this line can, because nothing the child
does after it starts can retroactively change what the parent already
wrote before handing it control.

`# Model` is the one field that *is* only best-effort: it's read by
matching the newest session file under `~/.claude/projects/<encoded
project root>/` written since spawn time, so it can misattribute if
another interactive session is writing under the same project
concurrently. Everything else in the header is exact.

**The scope caveat.** This file holds the subprocess's **final message
only** — not its full context window, not the individual tool calls it
made along the way. A term's absence here is not evidence the beat never
touched it; the transcript can't see what happened inside the beat, only
what it said at the end. The full tool-call record, if you need it, is the
session transcript under `~/.claude/projects/<encoded project
root>/*.jsonl` — match it by modification time against the beat's spawn
clock.

---

## Permissions

**Read this before you schedule anything.** Skipping straight to
Scheduling gets you a system that ticks on time forever while
accomplishing nothing, and nothing about the schedule itself will tell
you that's what's happening.

### The failure

`claude --print` with no tool permissions configured for the directory it
runs in **denies every tool call and exits 0** — it does not hang waiting
for a prompt (there's no one to prompt), and a single denial does not
necessarily abort the whole session either: the agent typically notices
the denial, gives up, and replies in plain text describing what it tried,
having changed nothing. Verified directly, in a clean directory with no
`.claude/settings.json`:

```bash
mkdir -p /tmp/sill-permission-demo && cd /tmp/sill-permission-demo
claude --print -p "Use the Bash tool to run: echo hi. Then use the Write tool to create proof.txt containing 'hi'. Report what happened." \
  --setting-sources project
echo "exit: $?"
ls proof.txt 2>&1
```

On this machine that produced exit `0`, a one-sentence report that the
write was denied, and no `proof.txt`. Your own output may phrase the
denial differently, but the shape — exit 0, no file, a prose explanation
you have to read to notice anything went wrong — is the failure mode
`produced_output()` in `backend/beat_worker.py` exists to catch, because
exit code alone cannot see it.

### The symptom

Once a voice declares an `output_glob`, `spawn_beat()` checks it after
every beat. A beat that exits 0 but leaves the glob unchanged is **not**
counted as success — the worker logs a warning naming the likely cause and
does **not** advance rotation, so the same voice retries next interval
instead of the rotation silently moving on as if nothing were wrong:

```
[analyst] Beat exited 0 in 0.3s but produced no file matching 'notes/analyst-*.md' — the agent CLI may be denying tools non-interactively; see docs/beats.md permissions. Rotation remains on [analyst].
```

Watch for it:

```bash
tail -f "${SILL_LOG_DIR:-/tmp}/beat-worker.log"
```

A voice with no `output_glob` has no such check — it's judged on exit code
alone, so an unpermissioned beat for that voice would show as a false
success. Give every voice an `output_glob` in `beats.json` if you want
this guard at all.

### The remedy

`beat_worker.py` spawns the child with a fixed command
(`claude --print -p "<prompt>"`, `cwd` set to the project root) and no
extra flags — so **`.claude/settings.local.json` in the project root is
the channel**, not a CLI flag you pass to the worker. Grant, at minimum,
the shell command that runs `sill notice`, plus Write/Edit:

```bash
mkdir -p .claude
cat > .claude/settings.local.json <<'EOF'
{
  "permissions": {
    "allow": [
      "Bash(sill notice *)",
      "Write",
      "Edit"
    ]
  }
}
EOF
```

`Bash(sill notice *)` prefix-matches any command starting with
`sill notice ` (see `claude --help`'s own `--allowedTools` example,
`"Bash(git *) Edit"`, for the pattern grammar). Because the grant lives in
this project's own settings file, bare `Write`/`Edit` are already scoped
to sessions running here — narrow them further with Claude Code's own
path-pattern syntax (`claude --help` → `--allowedTools`) once you've
confirmed the broad grant works; don't start from an untested narrow
pattern and debug two unknowns (scope and syntax) at once.

**Workspace trust is a separate gate that silently defeats the above.**
Even a correctly-written `settings.local.json` is ignored — with a warning,
not an error — if the directory itself has never been trusted:

```
Ignoring 2 permissions.allow entries from .claude/settings.json: this workspace has not been trusted. Run Claude Code interactively here once and accept the trust dialog, or set projects["<path>"].hasTrustDialogAccepted: true in ~/.claude.json.
```

That message is Claude Code's own, and it names both fixes. The simplest
is the first one — before scheduling anything, run `claude` (no `-p`)
interactively once in the project root and accept the trust dialog. This
warning, if you hit it, shows up inside a beat's own **transcript**
(`spawn_beat()` captures the child's stdout+stderr), not on any console
you're watching live — another reason the transcript is the first place to
look, not just the worker's own log.

**Verify with the by-hand run above before installing any schedule.**

```bash
sill-worker --mode beat --once
```

Look for `Beat complete` and exit status `0`. If the worker logs the
`produced no file` warning, check the permissions and try again before
moving on to Scheduling.

---

## Rotation starvation: one stuck voice blocks every voice

`advance_if()` (`backend/beat_worker.py`) only ever moves the rotation
past the voice that *just ran*, and only on verified success. That's the
right call for that one voice — a failing voice should keep retrying next
interval rather than have the worker silently give up on it. But the
rotation state is a single shared index, not one clock per voice, so the
consequence of that correct per-voice choice is global: as long as the
voice currently due keeps failing, no *other* voice ever gets a turn
either. **There is no cross-voice escape hatch** — nothing in this
codebase currently limits how long one voice can hold the rotation
hostage, and a schedule left running unattended will simply retry the
same stuck voice forever.

**What it looks like.** Confirmed live, on a two-voice rotation
(`analyst`, `reflector`): `analyst` ran once successfully, then
`reflector` failed eight consecutive scheduled intervals in a row — and
`analyst` never ran again for that entire stretch, despite being healthy
the whole time. Nothing about this looks like an error from the outside:
the schedule keeps ticking on time, the worker process keeps running, and
the log keeps growing. The only tell is *which* voice's name is in it.

**Common per-voice causes:** a typo'd `output_glob` that no file the
voice actually writes will ever match; a permission gap specific to one
voice (its standing prompt uses a tool the other voices' prompts don't,
and only that tool is denied); a standing prompt file that's missing or
unreadable for one voice only; or anything in "Permissions" above
happening to affect just one voice rather than all of them.

**How to notice.** Every per-voice outcome line in the worker's log —
success, timeout, transient-infrastructure skip, or the silent-failure
warning — starts with that voice's bracketed name followed by `Beat`
(verified against every such call site in `spawn_beat()`). Over a window
long enough to have expected each configured voice at least once
(roughly *N* voices × `SILL_BEAT_INTERVAL_SECONDS`), collect the distinct
names that actually appear:

```bash
grep -oE '\[[a-zA-Z0-9_-]+\] Beat' "${SILL_LOG_DIR:-/tmp}/beat-worker.log" \
  | sort -u
```

If `beats.json` configures voices that never show up here, rotation is
likely stuck on whichever voice *does*. Cross-check the rotation state
file itself (`SILL_BEAT_STATE_PATH`, default
`~/.local/state/sill/beat-rotation.json`) — its `next_index` will point at
the same stuck voice, unmoving, beat after beat, while the missing
voice's `output_glob`/`transcripts` directory stays untouched.

**How to fix.** Treat it as a single-voice failure and debug that one
voice the normal way: read its transcripts, check its `output_glob`
against what it's actually writing, re-run "Running one beat by hand"
above until *that specific voice* logs `Beat complete`. There is
currently no supported way to skip past a stuck voice and free the
rotation short of fixing the underlying per-voice cause — hand-editing
`next_index` in the state file would unstick the rotation mechanically,
but is untested, unsupported, and does not cure anything: the same voice
will simply fail again, and starve the rotation again, the next time it
comes up.

---

## Scheduling

**Choosing the interval happens earlier, not here.**
`docs/onboarding/05-christening.md` is where a human picks the number —
the arithmetic (beats per day, per week, per voice) is laid out there so
they choose it with the cost in front of them, and only after supervised
beats have already produced real output. This section only covers
installing whatever cadence they chose.

Nothing installs a schedule automatically — `install.sh` never touches
launchd or systemd. `scheduling/` has a macOS LaunchAgent template and a
Linux systemd `--user` unit template, plus a README covering the
substitute-and-install steps for each and the one asymmetry between them
(systemd's `--user` units need `loginctl enable-linger` to survive
logout; launchd doesn't). Read `scheduling/README.md` for the full
instructions — but not before the Permissions section above.
