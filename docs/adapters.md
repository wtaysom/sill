# Harness Adapters

Sill's hooks run on two agent harnesses today — Claude Code and Codex —
and are built to make room for more. This document is the contract that
makes "supports harness X" a checkable claim instead of a hope: four
slots, what each requires of a harness, the normalization vocabulary that
makes the same hook logic fire correctly on both, and the conformance
test a harness must pass.

Prior state, so the contract has a defect to react to: three of Sill's
five `PreToolUse` guards shipped matching only Claude's tool names
(`Bash`, `Write`, `Edit`) — Codex's equivalents (`exec`, `exec_command`,
`apply_patch`) never matched, so `shell-idiom-guard`, `stored-slot-guard`,
and `tool-type-witness` were silently inert on Codex while the README
listed it as a supported host. Fixed by the normalization module below;
this document (and its conformance test) is what keeps it fixed the next
time a hook, a matcher, or a third harness gets added.

---

## The four-slot contract

A harness is "supported" when these four slots all work. Each slot names
the file that implements it on this repo's two reference harnesses, what
the mechanism actually is, and where its proof lives.

### 1. inject — recalled memories reach the agent's context

**Mechanism:** `plugin/hooks/spontaneous-recall.py`, a `UserPromptSubmit`
hook. It reads `prompt` (and `cwd`/`session_id`) from the payload, queries
the store, and returns `hookSpecificOutput.additionalContext` — text
inserted into the agent's context before it answers. Every genuine prompt
also gets a one-line `[TIME]` header, so this slot has visible output even
when nothing is recalled (a down store, a short prompt, or an empty
result all still emit the header) — see `docs/hooks.md`'s
spontaneous-recall section.

**What it requires of a harness:** a `UserPromptSubmit` event carrying a
`prompt` string, and a wire that accepts `hookSpecificOutput.additionalContext`
back. Nothing else — this is the one slot that needed **no** harness
normalization from `_harness.py` below, because neither harness's
`UserPromptSubmit` payload diverges in the fields this hook reads. That
claim is inferred, not independently reverse-engineered against a live
Codex `UserPromptSubmit` sample in this repo — see "What's version-fragile"
below.

**Proof:** `backend/tests/test_adapter_conformance.py::test_inject_slot_returns_additional_context_on_both_harnesses` —
runs the real hook as a subprocess against a Claude-shaped and a
Codex-shaped payload (the latter adding `turn_id`, the one confirmed
Codex marker) and asserts `hookSpecificOutput.additionalContext` comes
back non-empty on both, with no live database required.

### 2. mint — the agent deliberately stores a memory

**Mechanism:** two paths land in the same place. A deliberate mint goes
through `backend/sill.py`'s `notice()` (CLI: `sill notice ...`; MCP:
`remember`/`remember_batch`). An **auto-store** path also exists:
`plugin/hooks/response-patterns.py`'s `auto_store_insight()`, a `Stop`
hook that shells out to `$SILL_CLI notice <content> --importance 0.6
--force assertive --speaker $SILL_SPEAKER_SELF --concepts ...` when its
(off-by-default) insight detector fires. That subprocess call is the
harness-facing part of this slot: whatever the harness hands the `Stop`
hook, `auto_store_insight()` reduces it to plain strings
(`response_text`, `cwd`, `transcript_path`) before building the argv, so
the argv-building itself has no harness branching to get wrong — the
harness-sensitive part already happened one layer up, in
`get_response_text()`/`source_project()`.

**What it requires of a harness:** a `Stop` event the hook can pull
response text from — either `last_assistant_message` directly (Codex
hands this to the hook; see `docs/hooks.md`'s response-patterns section)
or a `transcript_path` file in a schema `get_response_text()` knows how
to walk — and a shell that can run the configured `$SILL_CLI` (default
`sill`, the console script).

**Proof, split across two files by design (see "Not duplicated" below):**

- `backend/tests/test_notice.py::test_auto_store_argv_parses_for_both_harness_calling_shapes` —
  drives the **real** `auto_store_insight()` (not a hand-typed argv) under
  the two calling shapes `source_project()` actually branches on —
  `cwd` + `transcript_path` both present (Claude) vs. `cwd` alone
  (Codex, since it hands `last_assistant_message` directly and this hook
  never needs `transcript_path` to get response text on that harness) —
  and parses the captured argv against `backend/sill.py`'s own parser
  (`sill.build_parser()`).
- `backend/tests/test_adapter_conformance.py::test_mint_slot_argv_parses_through_the_full_cli_chain` —
  proves the layer the test above doesn't touch: the argv is what
  `$SILL_CLI` (the **`sill` console script**, `sill_cli.py` — not
  `sill.py` directly) actually receives. `sill_cli.py`'s `notice`
  subcommand is a bare passthrough shim (`add_help=False`, no arguments
  of its own) that forwards everything to `sill.py`'s parser; this test
  runs that full chain — `sill_cli.build_parser()` → the shim →
  `sill.main()` — end to end and checks it returns 0.

**Not duplicated, on purpose:** both tests exist because they check
*different* parser layers (`sill.build_parser()` directly, vs. the full
`sill_cli` → `sill` chain a real subprocess call actually goes through).
Neither hand-copies the other's assertions; see each test's own docstring
for the boundary.

### 3. capture — the conversation reaches a searchable archive

**Mechanism:** `plugin/hooks/_harness.py`'s `iter_transcript_tool_uses(path)`
— reads a session transcript file and yields `{id, name, input}` for every
tool call, across both JSONL schemas (see the divergence table below for
exactly how those schemas differ). This is what lets a hook that needs
"what tools ran this session" work without its own per-harness transcript
parser. Its consumer in the shipped hooks is
`response-patterns.py`'s `session_has_deliberate_mint()` (whether this
session already minted a memory, so insight auto-store doesn't echo it).
`track-reuse.py` is *not* one — it needs tool results as well as uses, so
it keeps its own walker; see slot 4.

**What it requires of a harness:** a transcript file on disk, one JSON
object per line, each tool call identifiable by *some* combination of
fields `iter_transcript_tool_uses` already knows how to read (see the
vocabulary section below) — or, for a genuinely new harness, a small
addition to `_iter_transcript_records` that recognizes its shape.

**Proof:** `backend/tests/test_adapter_conformance.py`'s `test_capture_slot_*`
tests, against two fixtures (`backend/tests/fixtures/claude-transcript.jsonl`,
`backend/tests/fixtures/codex-rollout.jsonl`) that encode the *same*
logical `recall_batch` MCP call in each harness's native shape and assert
`iter_transcript_tool_uses` yields the same normalized `name`/`input` for
both (call `id`s are intentionally not compared — see "What this contract
does and doesn't prove" below).

### 4. track — a recalled memory's use is detected

**Mechanism:** `plugin/hooks/track-reuse.py`, a `Stop` hook. It has its
own transcript walker (`get_recalled_memories()`, `get_response_text()`)
— separate from `_harness.iter_transcript_tool_uses` above, because this
hook needs *both* tool USES and tool RESULTS (memory content coming back
from a `recall`/`hydrate` call), where `iter_transcript_tool_uses` only
yields uses. It joins a transcript's tool name via
`_harness.join_mcp_name` — the fix for a real, shipped bug: the old code
concatenated Codex's split `namespace`/`name` with no separator
(`f"{namespace}{name}"`), producing `mcp__sillrecall_batch`, and only
"worked" because the downstream filter did a substring check for
`"recall"`/`"hydrate"`. Once memories and response text are extracted,
`detect_reuse()` (three guards: body-sampled evidence, reject a phrase
shared by ≥2 recalled memories, zero a burst of more than 3 detections)
is harness-agnostic — it never sees a raw payload, only already-extracted
strings.

**What it requires of a harness:** the same transcript-file requirement
as *capture*, plus tool RESULT records this hook's own walker can find
(`tool_result` content blocks on Claude; `function_call_output` records
on Codex).

**Proof:** `backend/tests/test_adapter_conformance.py::test_track_slot_reaches_the_same_verdict_on_both_harnesses` —
the same two fixtures as *capture*, carrying an identical recalled-memory
body and an identical final response that reuses a phrase from it. Both
harnesses' fixtures produce the **same** recalled-memory content and the
**same** reuse verdict (memory id, non-id evidence, channel) — not just
structurally-similar output, byte-identical, because the fixtures were
deliberately built to share the same content.

---

## The normalization vocabulary (`plugin/hooks/_harness.py`)

One module, nine divergences (see the table below), pure functions over
payload dicts — no I/O except `iter_transcript_tool_uses`, which reads a
file. Every function is **total**: garbage in (`None`, a list, a bare
string, a dict with `None` values) returns the documented "nothing here"
value rather than raising, because these run on every tool call in every
session — an exception escaping this module is a broken harness, not a
broken test.

| Function | Input | Returns | Notes |
|---|---|---|---|
| `detect(payload)` | hook payload | `"claude"` \| `"codex"` \| `"unknown"` | Codex-positive only, keyed on `turn_id` (every Codex event carries it; no Claude payload does). No Claude-positive marker exists, so a Claude payload reads `"unknown"`, not `"claude"` — don't invent one without verifying it first. |
| `tool_kind(payload)` | hook payload | `"shell"` \| `"write"` \| `"edit"` \| `"read"` \| `"mcp"` \| `"other"` | The mapping the divergence table's "Tool names" row is built from. |
| `mcp_tool_name(payload)` | hook payload | flat `mcp__server__tool` or `None` | Both harnesses' **hook payloads** already flatten MCP names — this just reads the field. |
| `join_mcp_name(record)` | transcript record (`{namespace, name}` or `{name}`) | flat `mcp__server__tool` or `None` | For the surface `mcp_tool_name` does NOT cover: Codex **transcripts** split MCP names into `namespace` + `name`. Joins with `"__"` only when a namespace is present, so a non-MCP call like `{"name": "exec"}` passes through unchanged. |
| `shell_command(payload)` | hook payload | the command as scannable text, or `None` | Reads whichever key that tool uses — `command` for `Bash`/`shell`/`shell_command`, `cmd` for `exec_command` — and renders an argv list one element per line (see the Shell command key row above). |
| `written_path(payload)` | hook payload | the target path or `None` | `Write`/`Edit`/`MultiEdit`'s `file_path`, or `apply_patch`'s path parsed out of its patch body's own `*** Update File: <path>` / `*** Add File: <path>` header line (Codex's `apply_patch` has no separate `file_path` field at all). Unparseable → `None`, never a guess. |
| `written_text(payload)` | hook payload | the introduced text or `None` | `Write`'s full content, `Edit`'s `new_string`, `MultiEdit`'s `new_string`s newline-joined, or `apply_patch`'s added (`+`) lines — scoped to the *first* file's header only, so a multi-file patch can't leak file B's content into a check scoped to file A. |
| `written_files(payload)` | hook payload | `[(path, added_text), ...]` | The multi-file-safe form of `written_path`/`written_text` together: one pair per `apply_patch` file header (each text scoped to only that file), or the single `(path, text)` pair for `Write`/`Edit`/`MultiEdit`. Exists because a caller gating an in-scope check on `written_path()` alone before ever reading `written_text()` judges an entire multi-file patch by its first file only — `state-language-check.py`, `stored-slot-guard.py`, and `tool-type-witness.py` all iterate this instead for exactly that reason. |
| `assistant_text(payload)` | hook payload | the assistant's turn text or `None` | Reads `last_assistant_message` directly — the one field Codex's `Stop` payload hands over that Claude's requires a transcript read to get (ground-truth item 3; already exploited by `response-patterns.py` and `track-reuse.py`, independently of this module). |
| `iter_transcript_tool_uses(path)` | a transcript file path | `Iterator[{id, name, input}]` | The one function that does I/O. Walks both JSONL schemas — see the divergence table's "Transcript tool-call record" row. A non-path argument (including an `int`, which `open()` treats as a raw fd — a real, fixed bug; see `_harness.py`'s comments), a missing file, or a malformed line yields nothing rather than raising, and the result is built eagerly so a bad file can't fail *mid*-iteration either. |

---

## Divergence table — the worked example

This is what the vocabulary above abstracts over. Established by direct
inspection of Codex CLI **0.144.1**'s embedded schemas, the live install,
and a sample of recent session transcripts (see "What's version-fragile"
for exactly which rows that covers and how to redo it). If your `codex
--version` differs, treat every Codex-column claim below as a hypothesis
to confirm, not a given.

| Aspect | Claude Code | Codex | Normalized by |
|---|---|---|---|
| Shell tool name | `Bash` | **four** names, in two record types: `exec` (a `custom_tool_call`) and `exec_command` / `shell` / `shell_command` (all `function_call`s) — the same action spelled four ways. Census counts in the section below | `tool_kind` → `"shell"`, `shell_command` |
| Shell command key | `tool_input.command`, a string | **varies by tool**: `exec_command` → `cmd` (string); `shell` → `command` (an **argv list**, `['bash','-lc','ls']`); `shell_command` → `command` (string); `exec` → the record's `input`, a string of JavaScript, not a shell line | `shell_command` tries `_COMMAND_KEYS` in order and renders an argv list for scanning (`_command_value`) |
| Write tool name | `Write` | `apply_patch` — always classified `"write"`, never `"edit"`, even for an `*** Update File:` body | `tool_kind`, `written_path`, `written_text` |
| Edit tool name | `Edit` / `MultiEdit` | *(no direct equivalent — `apply_patch` covers this too)* | `tool_kind` → `"edit"` for Claude only |
| Read tool name | `Read` | `view_image` | `tool_kind` → `"read"` |
| MCP naming, hook-payload surface | flat `mcp__server__tool` | flat `mcp__server__tool` (already normalized on this surface — no divergence here) | `mcp_tool_name` (pass-through) |
| MCP naming, transcript surface | flat `mcp__server__tool` (a `tool_use` block's `name`) | **split**: `namespace: "mcp__sill"` + `name: "recall_batch"` on a `function_call` record | `join_mcp_name` — a bare `f"{namespace}{name}"` concatenation (the pre-fix code) silently produced `mcp__sillrecall_batch` |
| Transcript envelope | `{type, timestamp, sessionId, cwd, message: {role, content: [...]}}` | `{timestamp, type, payload}`, where `payload.type` is `"response_item"`'s inner kind (`function_call`, `function_call_output`, `custom_tool_call`, `message`) | `iter_transcript_tool_uses` |
| Transcript tool-call record | `type=="assistant"` → `message.content[]` blocks with `type=="tool_use"` (`id`, `name`, `input` already flat) | `type=="response_item"`, `payload.type=="function_call"` (`id` from `call_id`, `name` via `join_mcp_name`, `input` from a JSON-**encoded-string** `arguments` field) **or** `payload.type=="custom_tool_call"` (`id` from `call_id`, `name`, `input` taken as-is — field shape confirmed by the v0.2.1 census; some records also carry `id`/`status`, ignored) | `iter_transcript_tool_uses` |
| Tool-result record | `type=="user"` → `message.content[]` blocks with `type=="tool_result"` (`tool_use_id`, `content`) | `type=="response_item"`, `payload.type=="function_call_output"` (`call_id`, `output`) | `track-reuse.py`'s own walker (not `_harness.py` — it needs results, not just uses) |
| `apply_patch` path encoding | n/a | no `file_path` field — the path is the first `*** Update File: <path>` / `*** Add File: <path>` line inside `tool_input.input`'s patch body text | `written_path`'s `_parse_patch_path` |
| Assistant text on `Stop` | requires a transcript read (`get_response_text` walks the file) | handed directly as `last_assistant_message` | `assistant_text`; also read ad hoc by `response-patterns.py`/`track-reuse.py` |
| Events available | `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `PreCompact` (the ones some hook in this repo targets — `PostToolUse`/`PreCompact` are opt-in, not in the default template; see `docs/hooks.md`) | superset — adds `PostCompact`, `PermissionRequest`, `SubagentStart`, `SubagentStop`; wire is Claude-compatible at the top level | n/a — no hook in this repo currently targets a Codex-only event |
| Harness self-identification | no positive marker found | `turn_id` present on every event | `detect` |
| MCP server registration | `~/.claude.json` (global, one entry: `mcpServers.sill`; written by `claude mcp add --scope user` when the CLI is on `PATH`, else merged directly) | `~/.codex/config.toml` (global, `[mcp_servers.sill]` + `[features].hooks = true`) | `install.sh` step 7 writes both |
| Hook wiring | `<project>/.claude/settings.local.json` (merges idempotently on every `--hooks-for` re-run); also `~/.claude/settings.json` under `--scope home` | `<project>/.codex/hooks.json` under `install.sh` (written once; **skipped** if it already exists, by design — first wiring only); also `~/.codex/hooks.json` under `--scope home`. Re-rendering an existing file is `upgrade.sh --hooks-for`'s job, not `install.sh`'s — it diffs against the current template and requires `--force-hooks` to overwrite (fixed the gap this row used to describe as unconditional; see `docs/hooks.md`'s "Adding hooks to a project") | `install.sh` step 8, from `plugin/codex.hooks.json.template`; `upgrade.sh --hooks-for` for re-rendering |
| Hook command trust | none — commands run as configured | **SHA-256-pinned** per command in `config.toml`'s `[hooks.state]` — see the warning below | n/a — operational, not code |

To register a specific instance directly, run `sill connect claude-code`
or `sill connect codex` with `--env-file /absolute/path/to/backend/.env`.
Use `sill connect claude-desktop` for Claude Desktop on macOS. Add
`--dry-run` to check the merge before writing. The client launches the
installed `sill-mcp` using the absolute env-file path. Set database
coordinates or a DSN in that file. Its settings are read literally without
shell expansion.

A previous installer may have registered the bare `sill-mcp` command.
Check which instance it was using before replacing that entry with
`--adopt-legacy`. Review any entry with custom arguments, environment
variables, or transport settings yourself.

Confirm the tool-name mapping yourself against the live source rather
than trusting this table indefinitely:

```bash
grep -n "_SHELL_NAMES\|_WRITE_NAMES\|_EDIT_NAMES\|_READ_NAMES" plugin/hooks/_harness.py
```

That shows what the code *believes*. To check what Codex actually sends,
census your own rollouts — this is how v0.2.1 found three of the table's
rows wrong, and it beats reasoning about what the tool "should" send:

```bash
python3 - <<'EOF'
import json, pathlib, collections
keys = collections.defaultdict(collections.Counter)
for f in pathlib.Path.home().joinpath(".codex/sessions").rglob("*.jsonl"):
    for line in f.open(encoding="utf-8", errors="replace"):
        try: p = json.loads(line).get("payload") or {}
        except Exception: continue
        if p.get("type") not in ("function_call", "custom_tool_call"): continue
        a = p.get("arguments") or p.get("input")
        if isinstance(a, str):
            try: a = json.loads(a)
            except Exception: pass
        shape = tuple(sorted(a)) if isinstance(a, dict) else type(a).__name__
        keys[p.get("name")][shape] += 1
for name, shapes in sorted(keys.items(), key=lambda kv: -sum(kv[1].values())):
    print(name, dict(shapes.most_common(3)))
EOF
```

A tool name printed here that `_SHELL_NAMES` (or the write/edit/read
sets) doesn't contain is a call every guard is currently ignoring; an
argument key that `_COMMAND_KEYS` doesn't list is a command no guard can
read. Both were true before v0.2.1.

---

## Two things about Codex that aren't guessable

Neither of these is discoverable by reading a hook's own code — both bit
this project silently before being found, so they're recorded here
rather than left to be rediscovered.

**Codex SHA-256-pins every hook command.** `~/.codex/config.toml`'s
`[hooks.state]` section records a hash of each configured hook command,
one entry per `(hooks.json path, event, index)` triple — confirmed live
against a real `~/.codex/config.toml` while writing this document:

```toml
[hooks.state."<project>/.codex/hooks.json:pre_tool_use:0:0"]
trusted_hash = "sha256:<64 hex chars>"
```

Note the event name in that key is **lowercase snake_case**
(`pre_tool_use`, `stop`, `user_prompt_submit`) — different casing from
the `PreToolUse`/`Stop`/`UserPromptSubmit` used in `hooks.json` itself
and in every payload's `hookEventName`. Edit a hook's command string —
even just re-rendering `.codex/hooks.json` with a new plugin path — and
Codex silently invalidates its trust in that command; the operator has
to re-approve it before it fires again. There is no way to pre-seed this
trust from a template. Practical consequence: don't casually rewrite
`.codex/hooks.json` commands expecting the existing install to keep
working unattended — check whether Codex is prompting for re-approval
after any such change.

**Codex fails CLOSED on `PermissionRequest` reserved output fields.**
Three fields — `updatedInput`, `updatedPermissions`, `interrupt` — are
reserved on Codex's `PermissionRequest` response wire. A **Claude-shaped**
response landing there (for instance, a `PreToolUse`-style
`hookSpecificOutput.permissionDecision` payload emitted on the wrong
event) gets read as one of these reserved fields and **denies the tool
call** — the opposite of fail-open. This is why every guard in this repo
(`shell-idiom-guard`, `stored-slot-guard`, `tool-type-witness`) only ever
emits `PreToolUse` decisions and is grep-checked to never contain the
three reserved field names:

```bash
grep -rl permissionDecision plugin/hooks/
# -> shell-idiom-guard.py, tool-type-witness.py, stored-slot-guard.py — and
#    nothing else; none of the three source files contain "updatedInput",
#    "updatedPermissions", or a bare "interrupt" key (also pinned by
#    backend/tests/test_guards_on_both_harnesses.py and this contract's
#    own test_*_never_emits_a_permission_request_shaped_payload tests).
```

If you write a new guard: **never** target `PermissionRequest` at all on
Codex unless you have independently confirmed the exact accepted shape
against a real Codex build — a best-effort guess is worse than not
handling the event, because the failure mode is silent denial, not a
visible error.

---

## What's version-fragile, and how to re-derive it

Everything in the divergence table above (except the two Claude-side
columns, which come from this repo's own working code) was established
by inspecting **Codex CLI 0.144.1** — multiple Codex versions coexist
in practice, and Codex Desktop's hook behavior has not been checked at
all. Treat the following as **claims about that one build**, not as
permanent facts about Codex:

- The exact tool names (`exec`, `exec_command`, `shell`, `shell_command`,
  `apply_patch`, `view_image`, and the `collaboration.*`/`web.run`/
  `update_plan` family that fall through to `"other"`). Which of these a
  session emits varies by build and by config: the census below found all
  four shell spellings across its 309 transcripts.
- `custom_tool_call`'s field shape (`call_id`, `name`, `input`) — carried
  as **unverified** from Plan 4 Task 1 until the v0.2.1 census
  **confirmed** it: 1,314 real records, `input` always a string, names
  `exec` (1,188) and `apply_patch` (126). Some records add `id` and
  `status` fields, which `iter_transcript_tool_uses` ignores.
- What `exec`'s `input` string *contains* is not a shell line — it is a
  JavaScript program that calls other tools
  (`await Promise.all([tools.exec_command({cmd: "…"})…`). `shell_command`
  returns it anyway, which is right for consumers that scan for a
  command's text, but it is script text, not an argv.
- Whether a Codex `UserPromptSubmit` payload's fields beyond `turn_id`
  genuinely match Claude's (assumed here because
  `plugin/codex.hooks.json.template` already wires `spontaneous-recall.py`
  to fire on it unconditionally, and because ground-truth item 4 — "output
  wire is Claude-compatible at the top level" — is suggestive, but this
  is an inference, not a direct observation recorded anywhere in this
  repo).
- The event superset (`PostCompact`, `PermissionRequest`, `SubagentStart`,
  `SubagentStop`) and the SHA-256 command-pinning behavior.

**How to re-derive any of this against a newer or different Codex
build:**

1. Check the build first — `codex --version` — and record it next to
   whatever you find, the way this document should have from the start
   if that record existed.
2. Wire a temporary "dump" hook to every event Codex will fire it on
   (`PreToolUse`, `PostToolUse`, `Stop`, `UserPromptSubmit`, `SessionStart`
   at minimum) that does nothing but append its raw stdin to a file:
   ```bash
   cat >> /tmp/codex-payload-dump.jsonl
   ```
   Run a real session that exercises a shell command, a file write, an
   MCP call, and a normal prompt/response turn. Inspect the dump.
3. For transcript shapes specifically, locate a real rollout file (the
   session log Codex itself writes) after that same session and diff its
   record shapes against the divergence table's "Transcript" rows.
4. Where you find a difference, update `plugin/hooks/_harness.py`'s
   mapping tables (`_SHELL_NAMES`, `_WRITE_NAMES`, etc.), extend
   `backend/tests/test_harness.py` with the new case, and update this
   document's divergence table and this section's build number in the
   same change — a divergence table that falls behind the code it
   describes is worse than no table.

---

## Supporting a new harness

1. **Implement the four slots.** At minimum: something that turns that
   harness's `UserPromptSubmit`-equivalent event into
   `hookSpecificOutput.additionalContext` (*inject*); confirm its `Stop`
   (or equivalent) event gives `response-patterns.py` enough to extract
   response text and shell out to `$SILL_CLI notice` (*mint* — usually
   free, since the mint path barely touches harness-specific fields);
   teach `plugin/hooks/_harness.py`'s `_iter_transcript_records` the new
   transcript schema (*capture*); confirm `track-reuse.py`'s own walker
   can find both tool uses and tool results in that schema, or extend it
   (*track*).
2. **Add matchers.** A per-harness hooks config
   (`plugin/<harness>.hooks.json.template` or equivalent) whose
   `PreToolUse` matchers include that harness's actual tool names —
   copy `plugin/codex.hooks.json.template`'s structure and see
   `docs/hooks.md` for what each hook's matcher needs to contain.
3. **Pass the conformance test.** Add the new harness's fixtures under
   `backend/tests/fixtures/`, extend `backend/tests/test_adapter_conformance.py`'s
   parametrized cases (and `backend/tests/test_harness.py`,
   `backend/tests/test_guards_on_both_harnesses.py`) with the new shapes,
   and add a `detect()` case if the harness has its own positive marker.
   Green here — plus `verify.sh`'s conformance check on a real install —
   is what "supports harness X" means going forward.
4. **Never emit a Claude-shaped `PermissionRequest` payload** on a
   harness you haven't independently confirmed accepts that exact shape
   — see the warning above.
5. **Document the divergences**, even the boring ones — add a column or
   a set of rows to the divergence table above rather than leaving them
   implicit in the code. The table is the artifact that makes the next
   harness's adapter faster to write than this one's was.

---

## What this contract does and doesn't prove

Said plainly, because a green conformance suite is easy to over-read:

- **It proves** that `_harness.py`'s normalization functions, the mint
  argv-building path, and `track-reuse.py`'s detector all behave
  identically on the two fixture shapes checked in — including two
  fixtures engineered to encode the exact same underlying event (one
  `recall_batch` MCP call, one recalled memory, one response that reuses
  a phrase from it) so the *capture* and *track* proofs are not just
  "both produce plausible output" but "both produce the same output."
- **It does not prove** either fixture is byte-accurate to what a real
  session of that harness produces. Both
  `backend/tests/fixtures/claude-transcript.jsonl` and
  `backend/tests/fixtures/codex-rollout.jsonl` are **hand-written**
  against the schemas in the divergence table above, not captured from a
  live session — see "What's version-fragile" for exactly which parts of
  those schemas are solid vs. best-effort. The v0.2.1 census (below)
  confirmed the record *shapes* against 309 real transcripts; the
  fixtures themselves are still hand-written to match.
- **It does not prove** the *inject* slot's Codex behavior beyond
  "the hook doesn't crash and returns the right JSON shape when handed a
  plausible payload" — there is no live Codex database or session behind
  that test, by design (this suite runs with no external services).
- **It does not exercise** Codex Desktop, `PermissionRequest`,
  `SessionStart`/`PreCompact` on Codex, or any harness beyond the two
  covered here.

A harness adapter this contract calls "conformant" is one that behaves
correctly on the shapes this document and its fixtures describe. Treat a
green run as evidence, not as a substitute for occasionally re-deriving
those shapes against a live session (see "How to re-derive" above).
