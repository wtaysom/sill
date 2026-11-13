# Scheduling the beat worker

**Nothing in this repo installs a schedule for you.** `install.sh` never
touches launchd or systemd, and the beat worker itself does not daemonize
or self-schedule beyond its own internal sleep loop — it just runs in the
foreground until something kills it. The two templates in this directory
are copy, substitute, and load/enable **by hand**. Until you do that last
step, `sill-worker --mode beat` only runs when you run it yourself.

**Before you load either one**, read `docs/beats.md`'s "Permissions"
section and run its by-hand verification command. A misconfigured install
scheduled this way ticks on time forever while doing nothing — the
schedule itself can't tell you that; only watching one beat actually
produce output can.

Both templates use the same three tokens `install.sh` already uses
elsewhere in this repo, plus one more this directory's templates add for
the reason explained right after the table:

| Token               | What it resolves to |
|----------------------|---------------------|
| `{{SILL_PYTHON}}`    | The interpreter that has the backend package installed — the pipx venv's `bin/python`, or `~/.local/share/sill-venv/bin/python` if you used the pip+venv fallback. |
| `{{SILL_DIR}}`       | This repo's root — where `beats.json` and `prompts/` live once you've set them up (**not** `backend/`). |
| `{{SILL_LOG_DIR}}`   | Where stdout/stderr and the worker's own `beat-worker.log` should go. `/tmp` works, but survives reboots better somewhere under your home directory. |
| `{{SILL_BEAT_CLI}}`  | The absolute path to the default Claude Code executable for voices without a `cli` field. |

**Why `{{SILL_BEAT_CLI}}` has to be an absolute path.** The worker's
default (`SILL_BEAT_CLI` unset) is the bare command name `claude`,
resolved against `PATH` — fine interactively, because your shell's `PATH`
almost certainly includes wherever `claude` actually lives. Both templates
in this directory set a fixed, minimal `PATH` instead
(`/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` for the plist,
`/usr/local/bin:/usr/bin:/bin` for the systemd unit), and a normal `claude`
install lives in `~/.local/bin` — outside both. Left unresolved, every
scheduled beat dies at spawn with `[Errno 2] No such file or directory:
'claude'` (rotation correctly holds when this happens, so it fails safe —
but it never beats; see this doc's troubleshooting section below).
Resolving `claude` to an absolute path once, at install time, sidesteps
needing `~/.local/bin` on the scheduler's `PATH` at all — the same
resolve-once-at-substitution strategy `{{SILL_PYTHON}}` already uses below,
for the identical reason (a scheduler's minimal `PATH` doesn't match your
interactive shell's).

A voice can set `cli` in `beats.json` to override this template value.
For a Codex voice, set `"launcher": "codex"` and set `cli` to the absolute
path returned by `command -v codex`. Without `cli`, a Codex voice resolves
`codex` on the scheduler's `PATH`. Verify each configured launcher by hand
with the same project, settings and permissions before loading a schedule.

Find your own values for these the same way `install.sh` resolves them:

```bash
# {{SILL_PYTHON}}: whichever of these exists, in order
ls "${PIPX_HOME:-$HOME/.local/pipx}/venvs/sill-memory/bin/python" 2>/dev/null \
  || ls "$HOME/.local/share/sill-venv/bin/python" 2>/dev/null \
  || command -v python3

# {{SILL_DIR}}: wherever you cloned this repo
cd /path/to/sill && pwd

# {{SILL_BEAT_CLI}}: resolved against YOUR shell's PATH, not the
# scheduler's — that's the whole point
command -v claude
```

---

## macOS (launchd)

Install:

```bash
cd /path/to/sill
SILL_PYTHON="$(ls "${PIPX_HOME:-$HOME/.local/pipx}/venvs/sill-memory/bin/python" 2>/dev/null \
  || ls "$HOME/.local/share/sill-venv/bin/python" 2>/dev/null \
  || command -v python3)"
SILL_LOG_DIR="$HOME/.local/share/sill/logs"
SILL_BEAT_CLI="$(command -v claude)"
mkdir -p "$SILL_LOG_DIR"

sed -e "s#{{SILL_PYTHON}}#$SILL_PYTHON#g" \
    -e "s#{{SILL_DIR}}#$PWD#g" \
    -e "s#{{SILL_LOG_DIR}}#$SILL_LOG_DIR#g" \
    -e "s#{{SILL_BEAT_CLI}}#$SILL_BEAT_CLI#g" \
    scheduling/com.sill.beat-worker.plist.template \
    > ~/Library/LaunchAgents/com.sill.beat-worker.plist

launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.sill.beat-worker.plist
```

Check it's running:

```bash
launchctl print "gui/$(id -u)/com.sill.beat-worker" | head -20
tail -f "$SILL_LOG_DIR"/beat-worker-launchd-std*.log
```

Stop (and unload — a loaded-but-stopped agent still relaunches on its own):

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.sill.beat-worker.plist
```

Edit-and-reload after changing the plist (e.g. to add
`SILL_BEAT_INTERVAL_SECONDS` or another env var under
`EnvironmentVariables` — see `docs/extending.md`'s env-var table):

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.sill.beat-worker.plist
# edit the plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.sill.beat-worker.plist
```

`RunAtLoad` + `KeepAlive` means launchd retries forever on any exit —
there's no failed/give-up state to clear. `ThrottleInterval` (60s in the
template) is the only brake on a fast crash-loop.

---

## Linux (systemd, `--user`)

Install:

```bash
cd /path/to/sill
SILL_PYTHON="$(ls "${PIPX_HOME:-$HOME/.local/pipx}/venvs/sill-memory/bin/python" 2>/dev/null \
  || ls "$HOME/.local/share/sill-venv/bin/python" 2>/dev/null \
  || command -v python3)"
SILL_LOG_DIR="$HOME/.local/share/sill/logs"
SILL_BEAT_CLI="$(command -v claude)"
mkdir -p "$SILL_LOG_DIR" ~/.config/systemd/user

sed -e "s#{{SILL_PYTHON}}#$SILL_PYTHON#g" \
    -e "s#{{SILL_DIR}}#$PWD#g" \
    -e "s#{{SILL_LOG_DIR}}#$SILL_LOG_DIR#g" \
    -e "s#{{SILL_BEAT_CLI}}#$SILL_BEAT_CLI#g" \
    scheduling/sill-beat-worker.service.template \
    > ~/.config/systemd/user/sill-beat-worker.service

# See "Linger" below before skipping this line.
loginctl enable-linger "$USER"

systemctl --user daemon-reload
systemctl --user enable --now sill-beat-worker.service
```

Check it's running:

```bash
systemctl --user status sill-beat-worker.service
tail -f "$SILL_LOG_DIR"/beat-worker-systemd-std*.log
```

Stop:

```bash
systemctl --user disable --now sill-beat-worker.service
```

Edit-and-reload after changing the unit:

```bash
systemctl --user stop sill-beat-worker.service
# edit ~/.config/systemd/user/sill-beat-worker.service
systemctl --user daemon-reload
systemctl --user start sill-beat-worker.service
```

### Linger

A `--user` unit normally stops the moment your last login session ends —
systemd tears down the whole user session, worker included. `loginctl
enable-linger "$USER"` tells systemd to keep your user session (and this
unit) running after logout and across boots, without you being logged in
at all. This is the one step launchd doesn't need an equivalent of — a
macOS LaunchAgent loaded with `launchctl bootstrap gui/<uid>` already
survives logout on its own. Skip `enable-linger` on Linux and the beat
worker will look correctly installed, start fine while you're logged in,
and then silently stop the next time you log out.

### The start-rate-limiter difference

systemd's own start-rate limiter (`StartLimitIntervalSec` /
`StartLimitBurst`, left at your distribution's defaults in the shipped
template) gives up restarting a unit that fails repeatedly inside one
window, and leaves it in a `start-limit-hit` failed state rather than
retrying forever. launchd has no equivalent — `RunAtLoad` + `KeepAlive`
retry forever, spaced only by `ThrottleInterval`.

This is deliberate to know about rather than fight, not a bug in either
template: `beat_worker.py`'s loop fires its first beat immediately on
start, before its first sleep (see `backend/beat_worker.py`), so a broken
config (wrong `{{SILL_PYTHON}}`, no `beats.json` at `{{SILL_DIR}}`) crashes
fast and repeatedly — on Linux that can hit the rate limiter within moments
of starting the unit, and the unit will then look inactive rather than
crash-looping forever the way its macOS counterpart would.

If `systemctl --user status sill-beat-worker.service` right after starting
it shows inactive/failed with `start-limit-hit`:

```bash
# fix whatever's actually broken first (see docs/beats.md's Permissions
# section and the by-hand verification run), then:
systemctl --user reset-failed sill-beat-worker.service
systemctl --user start sill-beat-worker.service
```

Or raise the ceiling instead of relying on noticing and clearing it by
hand — add to the unit's `[Unit]` block (the traditional, broadly-portable
location for these two keys across systemd versions; some newer systemd
releases also accept them under `[Service]`, but `[Unit]` is the safe
choice regardless of which version you're running):

```ini
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=10
```

---

## Either platform: confirm it's actually beating

Neither `launchctl print` nor `systemctl status` tells you whether a beat
*succeeded* — only that the process is running. Watch for an actual voice
completing:

```bash
tail -f "$SILL_LOG_DIR"/beat-worker.log
# -> "[analyst] Beat complete in Ns — transcript <attempt-id>.txt"
```

A repeating `"[<voice>] Beat exited 0 in Ns but produced no file matching
..."` warning means the schedule is running but the tool-permission problem
`docs/beats.md`'s Permissions section describes is still unresolved —
scheduling was never the failure; run that section's by-hand check again.

A `launch_error` receipt with a `No such file or directory` message in the
worker log means the configured executable could not be started. Check the
voice's `cli` path, the Claude default in `SILL_BEAT_CLI`, or the scheduler's
`PATH`. The rotation stays on that voice until it completes successfully.
