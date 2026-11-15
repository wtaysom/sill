# Identity Card

The identity card is the first thing an instance can read about itself —
before recall, before a beat, before anyone asks it to think about
anything. `sill identity show|init|set` reads and writes one JSON file:
`$SILL_STATE_DIR/identity.json`.

## Why it exists

The motivating failure was concrete: a persistent agent that did not know
its own model, had no name, and was handed unbounded reflection. Every
instance should be able to answer "what am I, whose is this, and where's
the document that says why" from first boot, without a database call, a
recall query, or a person in the loop. That's all this file does — it
answers those questions and nothing else.

## Where it lives

| Resolution order | Value |
|---|---|
| 1. `$SILL_STATE_DIR` | if set, `$SILL_STATE_DIR/identity.json` |
| 2. `$XDG_STATE_HOME` | else, `$XDG_STATE_HOME/sill/identity.json` |
| 3. fallback | else, `~/.local/state/sill/identity.json` |

**Never `/tmp`.** This is the same lesson, and the same resolution shape,
as the beat worker's rotation state (`docs/beats.md`'s
`SILL_BEAT_STATE_PATH`) — a reboot that silently clears `/tmp` must not be
able to erase who an instance is along with everything else it wiped.
Confirm the default for yourself:

```bash
cd backend && python3 -c "from scripts import identity_card as ic; print(ic.default_state_dir())"
# -> ~/.local/state/sill  (or $XDG_STATE_HOME/sill if you have that set)
```

## Fields

| Field | Meaning |
|---|---|
| `name` | The instance's name, chosen by the human at christening. `null` until then — see "`name: null` is a value" below. |
| `charter_path` | Path to the charter document written during christening (the human's own words on what this instance is for). Not validated for existence by `set` — the charter may be written before or after this call. |
| `born_at` | UTC ISO-8601 timestamp of `init` — when this identity file was first created. Immutable: re-running `init` never moves it. |
| `engine` | The agent CLI/model this instance runs on, e.g. `claude`, `codex`. Free text, set via `sill identity set --engine`. |
| `scope` | The install scope this instance runs at — `project` or `home`, matching `install.sh --scope` (see "Hooks for Claude Code and Codex" in the README). Free text here too; this field records the fact, it doesn't enforce the vocabulary. |
| `harnesses` | Which harnesses are wired for this instance, e.g. `["claude", "codex"]`. Set via `sill identity set --harnesses claude,codex` (comma-separated on the command line, a JSON array on disk). |
| `christened_at` | UTC ISO-8601 timestamp of the *first* `set --name`. Set once; a later rename does not move it — christening is an originating event, not a re-nameable property. |

## `name: null` is a value, not a gap

A fresh identity card has `"name": null`. That is deliberate and load-
bearing: `null` here means **not yet christened** — a fact the file
asserts, not a key that's missing or a field nobody filled in yet. An
instance reading its own card mid-onboarding, before a human has named it,
should be able to tell the difference between "I haven't been told my
name" (a `null` this file states plainly) and "this file is broken and I
can't tell anything" (the corruption case below, which is reported
differently on purpose).

`show` makes the distinction explicit in its own words rather than making
the reader infer it from a bare `null`:

```bash
sill identity show
# -> ...
#      name:          null (not yet christened)
# ...
```

The moment a human runs `sill identity set --name <name>`, `name` stops
being `null` and `christened_at` is stamped in the same act — see
"Setting fields" below.

## Commands

### `sill identity init`

Creates the identity card if one doesn't exist yet, with `name: null` and
a fresh `born_at`. Idempotent: running it again when a valid card already
exists is a no-op that reports the existing `born_at` rather than
overwriting it.

```bash
sill identity init
# -> identity: initialized at /path/to/identity.json (born 2026-08-05T15:28:54.466425+00:00)

sill identity init   # run again — born_at does not move
# -> identity: already initialized at /path/to/identity.json (born 2026-08-05T15:28:54.466425+00:00)
```

If the file exists but is corrupt, `init` refuses to touch it rather than
silently replacing possibly-recoverable data — see "Corrupt or missing
files" below.

### `sill identity show`

Prints every field. Never requires the file to exist or be well-formed
first — see "Corrupt or missing files" below for exactly how it degrades
when the card isn't in a readable state.

```bash
sill identity show
# -> identity: /path/to/identity.json
#      name:          Ada
#      charter_path:  docs/onboarding/charter.md
#      born_at:       2026-08-05T15:28:54.466425+00:00
#      engine:        claude
#      scope:         project
#      harnesses:     ["claude", "codex"]
#      christened_at: 2026-08-05T15:28:54.584770+00:00
```

### `sill identity set`

Updates one or more fields; leaves the rest untouched. Creates the file
first (with fresh defaults) if `init` hasn't been run yet, so christening
doesn't require a separate init step.

```bash
sill identity set --name Ada --charter docs/onboarding/charter.md
# -> identity: updated name, christened_at, charter_path at /path/to/identity.json

sill identity set --engine claude --scope project --harnesses claude,codex
# -> identity: updated engine, scope, harnesses at /path/to/identity.json
```

Flags, all optional (at least one is required — `set` with none of them is
a usage error, exit 64): `--name`, `--charter PATH`, `--engine`,
`--scope`, `--harnesses A,B`. Confirm the flag set for yourself:

```bash
sill identity set --help
```

## Corrupt or missing files degrade, never crash

This file is read by an agent trying to orient itself. A stack trace at
that moment is the opposite of orientation, so every command handles two
failure states explicitly and reports them in plain language, always
exiting 0 on `show`:

- **Missing** (no file at the resolved path yet — the common case on
  first boot, before onboarding has run `init`):

  ```bash
  sill identity show
  # -> identity: not yet initialized (no file at /path/to/identity.json). Run 'sill identity init' to create one.
  ```

- **Corrupt** (a file exists but isn't valid JSON, or is valid JSON that
  isn't an object — e.g. truncated by a crash mid-write):

  ```bash
  sill identity show
  # -> identity: /path/to/identity.json exists but is corrupt (invalid JSON (...)) — not safe to read.
  #    Fix it by hand, or remove it and run 'sill identity init' to start fresh.
  ```

`init` and `set` take the more conservative path on a corrupt file: they
refuse to write, rather than silently replacing data that might still be
partially recoverable by hand. Both exit non-zero and name the same
recovery (fix it, or remove it and re-`init`) — there's exactly one
documented way out, not a different story per command. Verify the refusal
for yourself:

```bash
demo_dir="$(mktemp -d)" && echo 'not json' > "$demo_dir/identity.json"
SILL_STATE_DIR="$demo_dir" sill identity set --name Ada; echo "exit=$?"
# -> identity: .../identity.json exists but is corrupt (...) — refusing to set fields onto a corrupt file. ...
# -> exit=1
cat "$demo_dir/identity.json"   # unchanged: still 'not json'
rm -rf "$demo_dir"
```

## Reading it directly

`identity.json` is meant to be read two ways: `sill identity show` for a
human or an agent that wants the formatted version, or the raw file for
anything that wants to load it as data (e.g. a standing prompt template
that wants to interpolate `name` and `engine` at session start):

```bash
cat "$(cd backend && python3 -c 'from scripts import identity_card as ic; print(ic.identity_path())')"
```

```json
{
  "name": "Ada",
  "charter_path": "docs/onboarding/charter.md",
  "born_at": "2026-08-05T15:28:54.466425+00:00",
  "engine": "claude",
  "scope": "project",
  "harnesses": ["claude", "codex"],
  "christened_at": "2026-08-05T15:28:54.584770+00:00"
}
```

## Where the christening happens

`sill identity set --name ... --charter ...` is a mechanism, not the
ceremony itself — the christening runbook (`docs/onboarding/`, a later
part of this release) is what walks a human through choosing a name and
writing a charter, then calls this command once, with both answers in
hand. This document covers the mechanism: what the fields mean, how the
file degrades, and the commands that read and write it.
