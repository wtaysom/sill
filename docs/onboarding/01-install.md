# Install and use your first memory

**Done when** a fresh client session retrieves the content and UUID of a
synthetic preference you saved with Sill.

## Install

On macOS or Linux, use Git, Python 3.10 or later, and a running Docker
engine with Compose. Open the app you plan to use and sign in through that
app. A local Claude or Codex connection uses your existing client sign-in.
Sill's default local embeddings and memory tools need no model API key.

For Claude Desktop on macOS:

```bash
git clone https://github.com/wtaysom/sill.git
cd sill
./install.sh --client claude-desktop
./verify.sh
```

For Claude Code, use `--client claude-code`. For either Codex CLI or a
desktop local Codex session, use `--client codex`. Repeat `--client` to
connect several clients to this checkout in one run. Without that flag,
the installer selects the Claude Code and Codex CLIs it detects on
`PATH`. For desktop-only setup, select the client explicitly.

The installer puts the Sill commands in a pipx environment if pipx is
available. Otherwise it uses a virtual environment under
`~/.local/share/sill-venv` and links the commands into `~/.local/bin`. Add
the appropriate bin directory to your shell's `PATH` for the `sill`
commands below. Desktop registrations use an absolute executable path.

The first run downloads the embedding model and can take several minutes.
The installer waits up to 1,800 seconds by default. If it times out, check
the service status and logs for the named failure, then rerun the same
install command after fixing it.

```bash
docker compose -f backend/docker-compose.yml ps
```

`verify.sh` checks the services and schema, an MCP handshake over stdio, the
seed, and hook and adapter fixtures. If you chose `./install.sh --no-seed`,
use `./verify.sh --no-seed` as well. A passing smoke check means you can
move on to testing the tools inside your client.

## Connect your client

Quit and reopen the desktop app, or restart the CLI session. Check that
the client lists the Sill memory tools and allow the selected calls if
prompted. Claude Code uses `~/.claude.json`. For each app's registry,
permissions, and connection checks, follow the
[client setup guide](../desktop-clients.md).

## Remember and recall

1. In your first session, ask:

   > Use the Sill `remember` tool to store this synthetic test preference:
   > For imaginary moon maps, I prefer dotted orange gridlines. Return the
   > UUID from the tool result.

2. Record the UUID from the successful tool call and close the session.
   Open a fresh session connected to the same Sill instance. Use another
   available client if you connected one.
3. Ask without supplying the answer or the UUID:

   > Use the Sill `recall` tool to look up my synthetic preference for
   > imaginary moon maps. Report the content and the memory UUID from the
   > tool result.

Compare both the content and UUID with the original tool result. If the
client replies without using Sill, select the server or plugin in that
client and ask for the exact tool again. If the tool runs but cannot find
the memory, check that both clients use the same absolute env-file path
and that you started a fresh session after registration.

For everyday use, ask the assistant to recall relevant prior decisions at
the start of a task and to remember a concise decision when you ask it to
retain one. These prompts guide tool use. Automatic recall and guard
behavior depend on the host and its hook configuration.

## Optional identity and worker permissions

### Identity card

**Done when:** `sill identity show` prints a card whose name is
`null (not yet christened)`.

The identity card records which tools and project an instance uses. Set the
engine and harnesses to match your installation. This example uses Claude Code.

```bash
sill identity init
sill identity set --engine claude --scope project --harnesses claude
sill identity show
```

```text
identity: /path/to/state/identity.json
  name:          null (not yet christened)
  charter_path:  null
  born_at:       2026-08-05T16:18:07.785014+00:00
  engine:        claude
  scope:         project
  harnesses:     ["claude"]
  christened_at: null
```

`null` here is a value, not a gap: it means *not yet christened*, and it is
stated in those words so an instance reading its own card mid-onboarding can
tell the difference between "nobody has named me yet" and "this file is
unreadable." Both cases are reported, in plain language, without a
traceback. Set the name and charter during [christening](05-christening.md).
See the [identity field reference](../identity.md).

### Worker permissions

Phase 4 of the optional onboarding sequence.

**Done when:** a headless agent-CLI run in this project's directory
**creates a file**. Not "exits 0" — a denied run also exits 0.

This is the phase whose absence is invisible. An agent CLI running
non-interactively with no tool permissions configured for its directory does
not hang waiting for a prompt, because there is nobody to prompt. It denies
the tool call, the agent notices the denial, gives up, and replies in prose
describing what it tried. Exit code 0. Nothing written. A beat worker with
this problem ticks on schedule forever, produces transcripts full of
plausible reports, and accomplishes nothing.

The example below uses Claude Code. For Codex CLI, check the workspace and
approval settings for an actual write in the worker directory. See
[beat permissions](../beats.md#permissions).

If you need to diagnose a denied Claude Code write, try this in a throwaway
directory with no settings file:

```bash
mkdir -p /tmp/sill-permission-demo
cd /tmp/sill-permission-demo
claude --print -p "Use the Write tool to create proof.txt containing 'hi'. Report what happened in one sentence." --setting-sources project < /dev/null
echo "exit: $?"
ls proof.txt
```

Observed:

```text
The write was blocked because permission to create proof.txt hasn't been granted yet, so the file was not created.
exit: 0
ls: proof.txt: No such file or directory
```

Exit 0 and no file. That pair is the whole problem: the exit code is not
evidence of anything, and the only discriminator is whether the file exists.

Now the two-rung check, in this order, in **this project's** directory.

**Rung 1 — does the CLI work here at all?** Grant the tool on the command
line, bypassing every settings file:

```bash
claude --print -p "Use the Write tool to create proof.txt containing 'hi'. Report what happened in one sentence." --setting-sources project --allowedTools Write < /dev/null
cat proof.txt
```

`hi` in `proof.txt` means the CLI, the model, and the working directory are
all fine, and any remaining problem is in the permission channel.

**Rung 2 — does the channel the worker actually uses work?** The beat worker
spawns a fixed command with no `--allowedTools` flag, so a project settings
file is the only channel it has. Delete `proof.txt`, **merge** the grant into
this project's settings file, and run the same command *without* the flag.

Merge rather than overwrite: `./install.sh --hooks-for` may already have
put a `hooks` block in this file. The guard drill needs that block.

```bash
rm proof.txt
mkdir -p .claude
python3 - <<'PY'
import json, pathlib
p = pathlib.Path(".claude/settings.local.json")
d = json.loads(p.read_text()) if p.exists() else {}
allow = d.setdefault("permissions", {}).setdefault("allow", [])
for grant in ["Bash(sill notice *)", "Write", "Edit"]:
    if grant not in allow:
        allow.append(grant)
p.write_text(json.dumps(d, indent=2) + "\n")
print("hook entries preserved:", sum(len(v) for v in d.get("hooks", {}).values()))
PY
claude --print -p "Use the Write tool to create proof.txt containing 'hi'. Report what happened in one sentence." --setting-sources project,local < /dev/null
cat proof.txt
```

**`project,local`, not `project`.** Claude Code has three setting sources —
`user`, `project`, `local` — and `.claude/settings.local.json` is the *local*
one. `--setting-sources project` does not load it, so a rung-2 check with
that flag alone fails no matter how correct the file is, and fails **silently**:
the grant is not ignored with a warning, the file is simply never read. The
worker passes no `--setting-sources` at all, so it gets every source; the
comma form above is what makes this check test the worker's channel instead
of a narrower one.

`Bash(sill notice *)` prefix-matches any command starting with
`sill notice ` — the beat's mint path. `Write` and `Edit` are already scoped
to this project by living in this project's settings file; narrow them
further once the broad grant is confirmed working, not before, or you will
be debugging scope and pattern syntax at the same time.

**What this list does not grant is a read path into memory**, and there is no
`Bash(sill recall *)` to add — the CLI has no `recall` subcommand (`sill
--help` lists `seed`, `db`, `verify`, `notice`, `identity`, `backfill`).
Recall reaches a beat through the **MCP server**, not the shell, so a beat
that needs to look something up needs `sill` connected in the harness the
worker spawns (see [client setup](../desktop-clients.md)), not another `Bash(...)` entry here. A beat asked to
measure recall with only the grants above will correctly report that it
cannot — which is the right behavior and a confusing hour if you were not
expecting it.

**Rung 1 passes and rung 2 still fails.** Two causes, in the order worth
checking:

1. **The source mismatch above** — the symptom is total silence: no warning,
   no mention of the settings file, just an agent reporting that permission
   was not granted. Re-run with `--setting-sources project,local`.
2. **The workspace has never been trusted**, so Claude Code reads the file
   and then discards its `permissions.allow` entries. Unlike (1), this one
   announces itself, and names both fixes:

```text
Ignoring 3 permissions.allow entries from .claude/settings.json: this workspace
has not been trusted. Run Claude Code interactively here once and accept the
trust dialog, or set projects["/path/to/this/project"].hasTrustDialogAccepted:
true in ~/.claude.json.
```

The simpler fix is the first one it names: run `claude` with no arguments once
in this directory and accept the trust dialog. Note the path in that warning is
the *resolved* one — on macOS a project under `/tmp` is recorded as
`/private/tmp/...`. The warning appears inside a beat's **transcript**, not on
any console you are watching, which is why it goes unnoticed for weeks.

Clean up when both rungs pass:

```bash
rm proof.txt
```

Keep `.claude/settings.local.json`. It holds the permissions the worker needs.

Next, choose any relevant step from the [optional setup](README.md#optional-setup-after-the-memory-round-trip).
