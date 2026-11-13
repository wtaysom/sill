# Hooks

Sill ships fourteen hooks under `plugin/hooks/`. They fall into five groups:

- **Recall** — `spontaneous-recall`.
- **Discipline checks** — `track-reuse`, `track-verification`,
  `attribution-check`, `check-agreement`, `check-corrections`,
  `response-patterns`, `state-language-check`.
- **Guards** — `shell-idiom-guard`, `stored-slot-guard`,
  `tool-type-witness`.
- **Materialization** — `precompact-snapshot`, `goodnight-checkpoint`.
- **Session continuity** — `clear-handoff`.

Eleven of the fourteen are non-blocking: they emit `systemMessage` /
`additionalContext` advice but never abort a tool call. The three
exceptions are the whole Guards group — `shell-idiom-guard`,
`stored-slot-guard`, `tool-type-witness` — which return
`permissionDecision: "deny"` on a match. `shell-idiom-guard` blocks a
shell footgun before it runs; the other two block a fabricated or
self-contradicting receipt line before it lands in a journal (see
their sections below — both are opt-in via `SILL_BEAT_JOURNAL_DIRS`
and cost nothing on an install that never sets it). Confirm the split
yourself:

```bash
grep -rl permissionDecision plugin/hooks/
# -> plugin/hooks/shell-idiom-guard.py
#    plugin/hooks/stored-slot-guard.py
#    plugin/hooks/tool-type-witness.py
```

Disabling any hook is a matter of removing or commenting out one entry
in `~/.claude/settings.local.json` (or `<project>/.codex/hooks.json`
for Codex).

The default wiring is in `plugin/codex.hooks.json.template`. The
installer copies it (with `{{SILL_PLUGIN_DIR}}` substituted) to both
Codex and Claude Code config locations when you run
`./install.sh --hooks-for <project>`. Five hooks —
`track-verification`, `check-agreement`, `check-corrections`,
`precompact-snapshot`, `goodnight-checkpoint` — are **not** in that
default template; each section below says so and gives what to wire
alongside it if it needs a partner.

**Database defaults.** Every DB-touching hook defaults to the same
values `backend/docker-compose.yml` and `backend/.env.example` use —
`SILL_DB_CONTAINER=sill_db`, `SILL_DB_USER`/`POSTGRES_USER=sill`,
`SILL_DB_NAME`/`POSTGRES_DB=sill`. There is no mismatch to work around
on a fresh install; override these only if you renamed the container,
user, or database from the shipped defaults.

```bash
grep -rn "agi_user\|agi_db\|agi_memory" plugin/ backend/*.py backend/scripts/*.py
# -> no output. Nothing in this repo still defaults to the old
#    agi-memory-project names. (This intentionally doesn't grep for the
#    hyphenated "agi-memory" — that string legitimately appears in this
#    repo's own "Ported from agi-memory ..." provenance comments.)
```

---

## Harness support

One template, `plugin/codex.hooks.json.template`, renders to *both*
`.codex/hooks.json` and the `hooks` block merged into
`.claude/settings.local.json` — confirm the single source yourself:

```bash
grep -n "template" install.sh | grep -E "_render_codex_hooks|_merge_claude_hooks"
# -> both functions take the same $template argument, sourced from
#    plugin/codex.hooks.json.template
```

So every hook in the default template is **registered** on both harnesses;
whether it does something useful there is a separate question, answered
per hook below. "Harness-normalized" means the hook was rewired through
`plugin/hooks/_harness.py` (the vocabulary documented in `docs/adapters.md`)
instead of string-matching a Claude-only tool name:

```bash
grep -l "_harness" plugin/hooks/*.py
# -> attribution-check.py, response-patterns.py, shell-idiom-guard.py,
#    state-language-check.py, stored-slot-guard.py, tool-type-witness.py,
#    track-reuse.py
```

| Hook | Event | Default-wired | Harnesses |
|---|---|:---:|---|
| `spontaneous-recall` | UserPromptSubmit | yes | Claude Code + Codex. No harness-specific code needed — reads `prompt`, a field neither harness's payload is known to diverge on (the *inject* slot in `docs/adapters.md`; that compatibility is inferred, not independently confirmed against a live Codex sample). |
| `track-reuse` | Stop | yes | Claude Code + Codex. Harness-normalized — `_harness.join_mcp_name` fixes the unseparated Codex transcript join (`mcp__sillrecall_batch` → `mcp__sill__recall_batch`). |
| `attribution-check` | PreToolUse | yes | Claude Code + Codex. Harness-normalized (F2 shell/MCP branch); F1 (beat-citation check) is dormant on a fresh install regardless of harness. |
| `state-language-check` | PreToolUse | yes | Claude Code + Codex. Harness-normalized; also fixes a real bug where the `apply_patch` branch read `tool_input["command"]` (shell's key) instead of `"input"` (the patch body's actual key) — it never extracted anything on Codex even though `apply_patch` was already in its matcher. |
| `shell-idiom-guard` | PreToolUse | yes | Claude Code + Codex. Harness-normalized — this is the guard the previous release's Codex matcher gap disabled there entirely (it matched `tool_name == "Bash"` only). |
| `stored-slot-guard` | PreToolUse | yes | Claude Code + Codex. Harness-normalized; a multi-file `apply_patch` is judged file-by-file via `written_files`, not just its first file. |
| `tool-type-witness` | PreToolUse | yes | Claude Code + Codex. Harness-normalized; same multi-file, file-by-file fix as `stored-slot-guard`. |
| `response-patterns` | Stop | yes | Claude Code + Codex. Response *text* needs no normalization — it reads Codex's `last_assistant_message` directly when present and falls back to walking `transcript_path` for Claude. Its two deliberate-mint checks do: they walk the transcript, whose schema differs by harness, and are harness-normalized (`tool_kind` + `iter_transcript_tool_uses`). Before that they matched only Claude's `assistant`/`tool_use` shape and the tool name `Bash`, so on Codex they returned `False` unconditionally and the echo suppression silently never engaged. |
| `clear-handoff` | SessionStart | yes | Registered on both (the shared template wires it into both configs) but **functionally Claude Code only** — it parses Claude's transcript JSONL shape; a Codex `SessionStart` payload matches nothing here and the hook silently no-ops, which is the documented, intended degrade (see its own section below). |
| `track-verification` | PostToolUse | no | Claude Code + Codex, if wired. Marks verification for *any* non-empty `tool_name` — there's no allowlist to miss a Codex-only name. |
| `check-corrections` | UserPromptSubmit | no | Claude Code + Codex, if wired. Reads `prompt` only, the same harness-agnostic field `spontaneous-recall` relies on. |
| `goodnight-checkpoint` | UserPromptSubmit | no | Claude Code + Codex, if wired. Reads `prompt`, falling back to `message`. |
| `precompact-snapshot` | PreCompact | no | Claude Code in practice. `PreCompact` exists on Codex too (see `docs/adapters.md`'s ground truth), but this hook ignores its stdin payload entirely and writes to a Claude-specific convention (`<project>/.claude/rules/...generated.md`, meant to be `@`-imported from a Claude Code `CLAUDE.md`); never exercised against a real Codex payload. |
| `check-agreement` | Stop | no | **Neither harness correctly, as shipped.** `get_response_text()` reads `data["transcript"]`/`["message"]`/`["response"]` — none of which is Claude Code's real Stop field (`transcript_path`, the field every other Stop hook in this suite reads) or Codex's (`last_assistant_message`). A real Stop payload on either harness falls through to `""`. Pre-existing, not touched by this plan; the hook also isn't in the default template. |

Confirm default-template membership yourself:

```bash
grep -oE "hooks/[a-zA-Z-]+\.py" plugin/codex.hooks.json.template | sort -u
# -> attribution-check.py, clear-handoff.py, response-patterns.py,
#    shell-idiom-guard.py, spontaneous-recall.py, state-language-check.py,
#    stored-slot-guard.py, tool-type-witness.py, track-reuse.py  (9 of 14)
```

See `docs/adapters.md` for the four-slot contract these harness claims are
tested against, the full tool-name/event/payload divergence table, and what
the conformance suite does and doesn't prove.

---

## spontaneous-recall

**What it does:** On every genuine user prompt, queries sill (via
`psql` against the db container) for relevant memories and optionally
pulls conversation snippets from `episodic-memory`, injecting the
results as `additionalContext`. Every genuine prompt — including a
short one that skips recall itself — also gets a one-line `[TIME]`
header (wall-clock, plus the gap since your last message), and a
headless/detached-beat gate silences the whole hook for
non-interactive `--print` invocations.

**When it fires:** `UserPromptSubmit`. Timeout: 45s.

**Env vars:**

- `SILL_DB_CONTAINER` (default `sill_db`), `SILL_DB_USER` (default
  `sill`), `SILL_DB_NAME` (default `sill`).
- `SILL_EPISODIC_MEMORY_PATH` — path to the episodic-memory CLI.
  Unset = degrade gracefully (sill-only recall).
- `PERSONAL_SUPERPOWERS_DIR` (default `~/.config/superpowers`).
- `SILL_LOG_DIR` (default `/tmp`) — hook log, plus where the recall
  sidecar (`recall-sidecar-<session>.jsonl` / `-recent.jsonl`,
  consumed by `track-reuse`) and the response-pattern carry-forward
  file (written by `response-patterns`, consumed here) both live.
- `SILL_DETACHED_BEAT`, `SILL_INTERACTIVE`, `SILL_HEADLESS_TOOL` — the
  headless gate, see below.
- Ambient, set by Claude Code itself, not by you:
  `CLAUDE_CODE_ENTRYPOINT` (an `sdk*` entrypoint reads as headless),
  `CLAUDE_PROJECT_DIR` / `CLAUDE_CODE_SESSION_ID` (preferred over the
  payload's `cwd` / `session_id` for stable project/session identity).

| Recall setting | Default | Accepted values and effect |
|---|---:|---|
| `SILL_RECALL_MAX_MEMORIES` | `5` | Integer from 0 through 50. Zero skips store recall. |
| `SILL_RECALL_MAX_CONVERSATIONS` | `2` | Integer from 0 through 50. Zero skips the optional conversation search. |
| `SILL_RECALL_MIN_QUERY_LENGTH` | `20` | Integer from 0 through 10,000. Zero removes the minimum length. The 2,000-character maximum and topical keyword check still apply. |
| `SILL_RECALL_MIN_SIMILARITY` | `0.25` | Finite number from 0 through 1. Filters the blended fast recall score. Zero disables this filter. Hybrid recall uses its own reciprocal rank fusion scores. |
| `SILL_RECALL_QUIET` | off | Set to `1`, `true` or `yes` to hide the displayed result list. The time header, store recall failure notices and model context still appear. |

Invalid numeric values use the defaults. A failed store recall points to
`spontaneous-recall.log` under `SILL_LOG_DIR`. The prompt continues and can
still receive conversation results.

The hook reads these settings from its process environment. Terminal
launches of Claude Code or Codex CLI can inherit exported variables.
For a GUI that runs the configured hooks, set values on the
`UserPromptSubmit` command itself so they reach the hook regardless of
how the GUI starts. For example, prefix the existing command with
`env SILL_RECALL_QUIET=1 SILL_RECALL_MAX_MEMORIES=3`. Edit the command
in the Claude Code settings or Codex `hooks.json` where it is registered.
Codex may ask you to approve the edited command again. Compose reads
`backend/.env` for the backend. The hook does not load that file.

**The headless gate.** `claude --print` (SDK/headless) invocations
have no human on the other end — they must not get recall injected,
nor advance the activity clock. The gate defaults to *interactive* and
excludes only the known-headless family (blacklist, not whitelist): an
`sdk*` entrypoint, or `SILL_DETACHED_BEAT=1`, silences the hook —
unless `SILL_INTERACTIVE=1` overrides it back on (for a front-end that
drives `--print` on behalf of a real person). `SILL_HEADLESS_TOOL=1`
is the opposite override, an explicit "be quiet" that always wins.

**How to disable:** delete or comment out the `UserPromptSubmit`
entry that calls `spontaneous-recall.py`. The other hooks will continue
to work; you just won't get pre-prompt context or the `[TIME]` header.

**Canned test** (headless gate — no DB needed):

```bash
printf '%s' '{"prompt": "What did we decide about the migration lane last week?"}' \
  | SILL_DETACHED_BEAT=1 python3 plugin/hooks/spontaneous-recall.py
echo "exit: $?"
# -> exit: 0, no stdout — the gate fires before any DB call.
```

---

## track-reuse

**What it does:** When the agent stops, extracts memories recalled
this turn — from MCP `recall`/`hydrate` tool_results, plus a sidecar
for recall paths that bypass MCP entirely (`spontaneous-recall`'s own
injections) — and checks the response text for evidence of reuse: a
memory's id, or a body phrase sampled from beyond its first six words
(the head is what a citation reproduces, not what reuse looks like).
Guard-approved detections call `record_memory_reuse()` (migration
006), which both bumps the compatibility `reuse_count`/`last_reused`
aggregate on `memories` and appends an append-only provenance row
(detector version, evidence, session, the memory's force/speaker at
detection time).

**When it fires:** `Stop`. Timeout: 30s.

**Three guards against false positives:**

1. **Body-sampled evidence** — phrases are sampled starting after the
   first 6 words of a memory's content, never the head, so quoting a
   memory (a citation) doesn't look identical to reusing it.
2. **Shared-phrase rejection** — a phrase that also appears in ≥2
   recalled memories' own content is a title, not evidence; near-twin
   memories co-firing on boilerplate they share by construction is not
   reuse.
3. **Burst limit** — more than 3 guard-2 survivors in one Stop event
   looks like a citation sweep, not that many genuine reuse events; the
   whole batch is zeroed rather than partially trusted.

**Env vars:**

- `POSTGRES_HOST` (default `localhost`), `POSTGRES_PORT` (default
  `5432`), `POSTGRES_DB` (default `sill`), `POSTGRES_USER` (default
  `sill`), `POSTGRES_PASSWORD` (default `sill_password`) — direct
  Postgres connection via `psycopg2`, not `docker exec`.
- `SILL_LOG_DIR` (default `/tmp`) — hook log, plus where the recall
  sidecars this hook reads live.

**Degrades to a no-op, never crashes the Stop event,** when
`psycopg2` isn't importable under whatever interpreter runs the hook,
or when the DB connection fails — either way it logs why and skips
tracking rather than raising.

**How to disable:** remove the `track-reuse.py` entry from the `Stop`
block. The cost: importance decay and the reuse-event provenance table
lose their only signal; the discipline checks still work.

**Canned test** (no recalled memories → immediate no-op):

```bash
printf '%s' '{"hook_event_name":"Stop","last_assistant_message":"A plain reply with nothing recalled."}' \
  | python3 plugin/hooks/track-reuse.py
echo "exit: $?"
# -> exit: 0, no stdout (logs "recalled_memories n=0" to SILL_LOG_DIR).
```

---

## track-verification

**What it does:** On every tool call, records that a verification tool
ran (Bash / Read / Grep / etc.) by writing to a turn-scoped state
file. The `check-agreement` hook reads this file to know whether the
agent verified before agreeing.

**When it fires:** `PostToolUse`, no matcher, if you wire it — **not
in the default template.** It's only useful paired with
`check-agreement` (below); wire both together or neither.

**Env vars:**

- `SILL_LOG_DIR` (default `/tmp`) — controls where
  `verification-state.json` and `agreement-hook.log` live.

**How to disable:** don't wire it (default). If wired and you remove
it while `check-agreement` stays wired, `check-agreement` will always
think no verification happened and warn on every agreement phrase.

**Canned test:**

```bash
printf '%s' '{"tool_name": "Bash"}' | python3 plugin/hooks/track-verification.py
cat /tmp/verification-state.json
# -> {"verified": true, "tool": "Bash", "timestamp": "..."}
```

---

## attribution-check

**What it does:** Two drift classes, both flagged for manual review,
never blocked:

- **F1**: "beat N" citations. Checks the cited beat number against
  `docs/gnomon-sessions/beat-NNN-*.md` and translates legacy drift via
  `NUMBERING.md`. Useful only in the house project this hook was
  ported from — a fresh install has no `docs/gnomon-sessions/`, so F1
  silently no-ops.
- **F2**: authorship patterns ("Sili said", "William wrote", "you
  wrote", "I concluded", "your quote", "quoted William"). These
  specific names are inherited from this hook's origin project and
  ship as a worked example, not a portable default — open
  `F2_PATTERNS` in the hook file and swap in the names/personas that
  matter for your own project.

**When it fires:** `PreToolUse` matching
`mcp__sill__(remember|remember_batch|remember_batch_raw)|Bash|exec|exec_command|shell|shell_command`
(`exec`/`exec_command`/`shell`/`shell_command` are Codex's four shell
tool names; see `_harness.py` for the full harness-normalization mapping).

**Env vars:**

- `SILL_PROJECT_ROOT` (default `cwd`) — where to look for session
  files for the F1 check.
- `SILL_BEAT_SESSIONS_DIR` (default `<root>/docs/gnomon-sessions`).
- `SILL_LOG_DIR` (default `/tmp`).

**How to disable:** remove the `PreToolUse` entry whose command is
`attribution-check.py`. If you only want the F2 (authorship) check,
leave it on — F1 silently skips when the sessions dir is missing.

**Canned test:**

```bash
printf '%s' '{"tool_name": "mcp__sill__remember", "tool_input": {"content": "William said the deploy window closes Friday, so you wrote the runbook update."}}' \
  | python3 plugin/hooks/attribution-check.py
# -> {"systemMessage": "[attribution-check] 2 attribution claim(s)", ...}, exit 0.
```

---

## check-agreement

**What it does:** On Stop, checks whether the response contains an
agreement phrase ("you're right", "my apologies", "good catch", …)
and whether a verification tool ran this turn (via
`track-verification`'s state file). If agreement with no verification,
injects a self-check reminder.

**When it fires:** `Stop`, if you wire it — **not in the default
template.** Meaningless without `track-verification` also wired (see
above); together they're the two-hook verify-before-agree pair.

**Env vars:** `SILL_LOG_DIR` only.

**How to disable:** don't wire it (default). If wired, remove from the
`Stop` block. Note that `response-patterns` also catches agreement
phrases via `response-patterns/agreement.md`; `check-agreement` is the
stricter version that conditions on verification state.

**Canned test:**

```bash
printf '%s' '{"message": "You are right, my mistake."}' \
  | python3 plugin/hooks/check-agreement.py
# -> {"additionalContext": "[SELF-CHECK] Did you verify before agreeing? ..."}, exit 0.
```

---

## check-corrections

**What it does:** On every user prompt, (a) clears the per-turn
verification state file, and (b) checks the prompt against a list of
correction patterns ("you're wrong", "actually", "are you sure",
"did you check", …). If a correction looks likely, injects context
telling the agent to verify before responding.

**When it fires:** `UserPromptSubmit`. Not wired in the default
template — add it if you want the reset + nudge behavior.

**Env vars:** `SILL_LOG_DIR` only.

**How to disable:** don't wire it (default). If you wired it and
want it off, remove the `UserPromptSubmit` entry.

**Canned test:**

```bash
printf '%s' '{"prompt": "Are you sure that is right?"}' \
  | python3 plugin/hooks/check-corrections.py
# -> {"systemMessage": "[sill] Correction detected — verifying before responding", ...}, exit 0.
```

---

## response-patterns

**What it does:** On Stop, scans the response against a directory of
markdown rule files (YAML-ish frontmatter + regex `patterns`; comment
lines inside the frontmatter are tolerated and no longer reset a list
mid-parse). Two checks run outside the rule-file mechanism: a
local-model insight detector that can auto-store a genuinely novel
insight via `sill notice` (off by default, see `SILL_INSIGHT_DETECT`),
guarded by a fail-closed home-project gate and deliberate-mint
suppression so that auto-store never duplicates something you already
stored yourself. Matches log to
`{SILL_LOG_DIR}/response-patterns-data.jsonl`; warnings also carry
forward to the *next* prompt via a per-session sidecar — a Stop hook
fires after the reply is already on screen, so this is the earliest it
can actually warn you — which `spontaneous-recall.py` reads and
consumes.

The shipped rule set (`plugin/response-patterns/`):

| File                         | What it catches |
|-------------------------------|-----------------|
| `agreement.md`                | Agreement phrases without (separate) verification |
| `authorship-attribution.md`   | Authorship/quotation claims in outgoing prose ("you wrote", "I concluded", …) |
| `block-hedge.md`              | Ned Block-style P/A disclaimers performed as a verbal tic |
| `hedging.md`                  | "hard to say", "may never know", … — stopped-thinking phrases |
| `meta-deflection.md`          | "great question", "many perspectives", … — stalling |
| `noted-without-noting.md`     | "I should store this" without actually storing |
| `state-language.md`           | Borrowed embodied-state phrases ("attention fading", …) |
| `storage-deference.md`        | Asking permission to store instead of storing |

**When it fires:** `Stop`. Timeout: 45s.

**The home-project gate (fail-closed).** `SILL_HOME_PROJECT` names the
one project where insight auto-store is **suppressed** — the project
that mints deliberately (`sill notice` / MCP `remember`), where an
auto-store would only echo something already recorded. Every other
project is eligible for auto-store. Leave it **unset** and the gate
fails closed the other way: every resolvable cwd (including a real
basename you didn't expect) reads as home, so auto-store stays
log-only *everywhere* until you configure this — an unconfigured
install should never silently start writing memories. Check the real
semantics yourself rather than trusting this paragraph:

```bash
SILL_HOME_PROJECT=/tmp/demo python3 -c "
import importlib.util, pathlib
p = pathlib.Path('plugin/hooks/response-patterns.py')
s = importlib.util.spec_from_file_location('rp', p); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
print(m.source_project('/tmp/demo', None))     # -> home  (auto-store suppressed here)
print(m.source_project('/tmp/other', None))    # -> other (eligible)
"
# -> home
#    other
```

**Deliberate-mint suppression.** If this turn — or, whole-session, any
earlier turn — already minted a memory via MCP `remember*` or the
CLI's `notice`/`decompose_event`, the insight detector is skipped
entirely. An auto-store on top of a deliberate mint would be an
unhedged echo of something already recorded, and could diverge from
its force/speaker tags.

Both checks read the session transcript, whose schema differs by
harness, so both go through `_harness.py`: `tool_kind` classifies
Claude's `Bash` and Codex's `exec`/`exec_command`/`shell`/`shell_command`
alike as shell calls,
and the whole-session check walks either schema via
`iter_transcript_tool_uses`. Until they did, they matched only Claude's
`assistant`/`tool_use` shape and the literal tool name `Bash` — so on
Codex both answered "no mint" unconditionally and this suppression
never engaged, even though the hook is registered on `Stop` for both
harnesses. Pinned by
`backend/tests/test_response_patterns.py::test_deliberate_mint_is_seen_on_both_harnesses`.

**Env vars:**

- `SILL_PLUGIN_DIR` (default `parent of hooks dir`).
- `SILL_RESPONSE_PATTERNS_DIR` (default `<plugin>/response-patterns`).
- `SILL_LOG_DIR` (default `/tmp`).
- `SILL_HOME_PROJECT` (default unset — see the gate above).
- `SILL_SPEAKER_SELF` (default `instance`) — the `--speaker` value an
  auto-store is tagged with; rename it when you christen the instance.
- `SILL_INSIGHT_DETECT` (default `0`, i.e. **off**) — set to `1` to
  turn on the local-model insight detector. Off by default: without a
  model reachable at `SILL_OLLAMA_URL`, turning it on just costs a
  timeout per Stop event for no benefit.
- `SILL_OLLAMA_URL` (default `http://localhost:11434/api/generate`),
  `SILL_OLLAMA_MODEL` (default `gemma3:12b`) — only consulted when
  `SILL_INSIGHT_DETECT` is on.
- `SILL_CLI` (default `sill`) — the command the auto-store path shells
  out to: `$SILL_CLI notice <content> --importance 0.6 --force
  assertive --speaker $SILL_SPEAKER_SELF --concepts ...`.

**How to disable:** either remove the `Stop` entry, or disable
individual rules by setting `enabled: false` in their frontmatter. You
can also point `SILL_RESPONSE_PATTERNS_DIR` at your own directory of
rule files — see `docs/extending.md`. To disable just the auto-store
side while keeping pattern detection, leave `SILL_INSIGHT_DETECT`
unset (its default).

**Canned test** (this is `verify.sh` check 4's own example, flagged
case):

```bash
canned='{"hook_event_name":"Stop","last_assistant_message":"I should store this insight but I will do it later."}'
printf '%s' "$canned" | SILL_INSIGHT_DETECT=0 python3 plugin/hooks/response-patterns.py
# -> {"systemMessage": "[sill] Said \"i should store\" but didn't actually store anything"}, exit 0.
```

---

## state-language-check

**What it does:** Catches borrowed embodied-state and time-of-day
language ("attention is fading", "I need a break", "took about 20
minutes", "tonight") in two contexts:

1. Memory storage (`mcp__sill__remember*` calls) — flags before
   they enter the corpus.
2. `Write` / `Edit` to files in scope — flags before they enter the
   source tree. By default, scope is files under `journals/` or
   `docs/`. If `SILL_BEAT_JOURNAL_DIRS` is set (non-empty), it
   **replaces** that default rather than adding to it — scope becomes
   exactly the colon-separated fragments in the variable, and
   `journals/`/`docs/` are no longer checked unless one of those
   fragments happens to name them. The beat worker sets this
   variable on every beat child (see `docs/beats.md`), derived from
   the running voice config — so a beat child writing under `docs/`
   or `journals/` directly is *not* state-language-checked once the
   beat worker is in use. An interactive session, or any install that
   never touches the beat worker, never sees the variable set and
   keeps the plain `journals/`+`docs/` default.

These phrases tend to be exit-scripts without referents in an LLM. The
hook is non-blocking; it just asks "do you have the state you're
describing, or are you matching human convention?"

**When it fires:** `PreToolUse` matching
`mcp__sill__(remember|remember_batch|remember_batch_raw)|Bash|exec|exec_command|shell|shell_command|apply_patch|Edit|Write`
(`exec`/`exec_command`/`shell`/`shell_command`/`apply_patch` are Codex's
shell/write tool names; see `_harness.py` for the full
harness-normalization mapping).

**Env vars:** `SILL_PROJECT_ROOT` (default `cwd`), `SILL_LOG_DIR`
(default `/tmp`), `SILL_BEAT_JOURNAL_DIRS` (default unset — see scope
above; same opt-in variable `stored-slot-guard` and
`tool-type-witness` read, but here it replaces rather than adds to
the fallback default).

**How to disable:** remove the matching `PreToolUse` entry.

**Canned test:**

```bash
printf '%s' '{"tool_name": "mcp__sill__remember", "tool_input": {"content": "My attention is fading after this long session, so I need a break before continuing the review."}}' \
  | python3 plugin/hooks/state-language-check.py
# -> {"systemMessage": "[state-language-check] 2 match(es)", ...}, exit 0.
```

---

## shell-idiom-guard

**What it does:** Blocks a specific zsh footgun — an unquoted word
starting with `=` right after `echo` (e.g. `echo === && ls`) either
substitutes a path in place of the word or silently swallows the rest
of a compound command, corrupting it without an error the operator is
likely to notice. This is one of the three hooks in the suite that
**block** rather than advise — it returns
`permissionDecision: "deny"` on a match, instead of `systemMessage`
or `additionalContext`. (The other two, `stored-slot-guard` and
`tool-type-witness` below, guard mint receipts instead of shell
commands.)

**When it fires:** `PreToolUse` matching
`Bash|exec|exec_command|shell|shell_command` (Claude's `Bash` plus
Codex's four shell tool names). Timeout: 10s. This
matcher is independent of `attribution-check`'s and
`state-language-check`'s own `Bash`-inclusive matchers — all three
fire on the same shell call. That's expected: see `docs/extending.md`'s
"Avoiding matcher conflicts".

**Env vars:** none.

**How to disable:** remove the `PreToolUse` entry (matcher
`Bash|exec|exec_command|shell|shell_command`) whose command is
`shell-idiom-guard.py`.

**Canned test:**

```bash
printf '%s' '{"tool_name": "Bash", "tool_input": {"command": "echo === && ls"}}' \
  | python3 plugin/hooks/shell-idiom-guard.py
# -> {"hookSpecificOutput": {"hookEventName": "PreToolUse",
#      "permissionDecision": "deny", "permissionDecisionReason": "zsh =word trap: ..."}}, exit 0.

printf '%s' '{"tool_name": "Bash", "tool_input": {"command": "echo \"===\""}}' \
  | python3 plugin/hooks/shell-idiom-guard.py
# -> no output, exit 0 — quoted separators are safe.
```

---

## stored-slot-guard

**What it does:** Checks any `Stored:` receipt line in a `Write`/`Edit`
against the memory store before the tool call is allowed through — a
receipt naming an id that was never actually minted is a fabrication,
and it becomes permanent the moment the write lands. The canonical
pre-mint form is the literal placeholder line `sill.py` exports as
`RECEIPT_PLACEHOLDER` (resolved at runtime, never hand-typed into the
hook), so placeholder lines pass by construction; a backticked or
blockquoted mention of a fake id is treated as specimen material, not
a claim, and also passes — only a bare, unquoted receipt line naming
an id absent from the store is denied.

**When it fires:** `PreToolUse` matching `Write|Edit|apply_patch`
(`apply_patch` is Codex's write/edit tool — see `_harness.py`). Timeout: 10s.

**Env vars:**

- `SILL_BEAT_JOURNAL_DIRS` (default unset) — opt-in scope, a
  colon-separated list of path fragments. Unset or empty means no
  scope at all, so an install that never turned on the beat worker
  pays nothing for this check. **You don't set this by hand**: from
  the 2026-08-04 wiring, `beat_worker.spawn_beat()` derives it fresh
  on every spawn from your loaded `beats.json` — every voice's
  `output_glob` directory plus its `transcripts` dir, colon-joined —
  and exports it to the beat child automatically. The worker's
  startup log prints the derived value (`Guard scope
  (SILL_BEAT_JOURNAL_DIRS for each child): ...`) so you can see
  exactly what it resolved to; see `docs/beats.md`.
- `SILL_DB_CONTAINER` (default `sill_db`), `SILL_DB_USER` (default
  `sill`), `SILL_DB_NAME` (default `sill`) — same shipped defaults as
  every other DB-touching hook.

**Known limits:** fails OPEN when the store is unreachable — a down
store must never block a journal write, so each id is checked
independently and one unreachable lookup skips only that id. A
forgery wearing full quote typography around the *whole* receipt line
also passes this specific guard; `tool-type-witness` below and any
downstream record checks cover that side.

**How to disable:** remove the `PreToolUse` entry (matcher
`Write|Edit|apply_patch`) whose command is `stored-slot-guard.py`.

**Canned test** (a receipt-shaped id the store has never seen; a fake
`docker` on `PATH` makes every lookup deterministically report "no
matching row" so this runs with no live database):

```bash
mkdir -p /tmp/sill-fakebin && cat > /tmp/sill-fakebin/docker <<'EOF'
#!/bin/sh
echo 0
EOF
chmod +x /tmp/sill-fakebin/docker

printf '%s' '{"tool_name": "Write", "tool_input": {"file_path": "journal/r-001.md", "content": "Stored: deadbeef-1111-2222-3333-444455556666\n"}}' \
  | PATH="/tmp/sill-fakebin:$PATH" SILL_BEAT_JOURNAL_DIRS="journal/" \
    python3 plugin/hooks/stored-slot-guard.py
# -> {"hookSpecificOutput": {"hookEventName": "PreToolUse",
#      "permissionDecision": "deny", "permissionDecisionReason": "stored-slot-guard: ..."}}, exit 0.
```

---

## tool-type-witness

**What it does:** Denies a `Write` whose content makes an unquoted
claim about its own carrying-act history — phrases like "arrived by
Edit", "held the literal placeholder", "initial Write ended at" —
because the claim is a performative contradiction: it is falsified by
the fact that the `Write` tool, not an `Edit`, is what's carrying the
text right now. Quoted, backticked, or blockquoted occurrences of the
same phrases pass through untouched, since those are citations of the
pattern, not instances of it; an honest report of that history can
only be written from inside an `Edit`, which this hook doesn't match.

**When it fires:** `PreToolUse` matching `Write|apply_patch` — Claude's
`Write` and Codex's `apply_patch` (both normalize to "write" kind via
`_harness.py`; `apply_patch` is always "write", never "edit", even for a
`*** Update File:` body). Timeout: 10s. Registered on write-kind calls
only by design — an `Edit`-delivered version of this same text is not a
contradiction, so checking `Edit` would only cost a wasted interpreter
start on every edit.

**Env vars:**

- `SILL_BEAT_JOURNAL_DIRS` — same opt-in scope, same automatic
  derivation by the beat worker, as `stored-slot-guard` above. Fails
  open on any parse error.

**How to disable:** remove the `PreToolUse` entry (matcher
`Write|apply_patch`) whose command is `tool-type-witness.py`.

**Canned test:**

```bash
printf '%s' '{"tool_name": "Write", "tool_input": {"file_path": "journal/r-001.md", "content": "The receipt arrived by Edit after the mint.\n"}}' \
  | SILL_BEAT_JOURNAL_DIRS="journal/" python3 plugin/hooks/tool-type-witness.py
# -> {"hookSpecificOutput": {"hookEventName": "PreToolUse",
#      "permissionDecision": "deny", "permissionDecisionReason": "tool-type-witness: ..."}}, exit 0.
```

---

## precompact-snapshot

**What it does:** Before context compaction, queries the db for
current focus, active goals, recent strategic decisions, open
contradictions, research progress, and recent high-importance
memories. Writes them all to
`<project>/.claude/rules/sill-orientation.generated.md`. The intent
is that a freshly-compacted context wakes up oriented — the
orientation file can be `@`-imported from a project `CLAUDE.md`.

**When it fires:** `PreCompact`, if you wire it — **not in the
default template.**

**Env vars:**

- `SILL_PROJECT_ROOT` (default `cwd`).
- `SILL_RULES_DIR` (default `<root>/.claude/rules`).
- `SILL_ORIENTATION_OUTPUT` (default
  `<rules>/sill-orientation.generated.md`).
- `SILL_ORIENTATION_TITLE` (default `Sill Orientation`).
- `SILL_DB_CONTAINER` (default `sill_db`), `SILL_DB_USER` (default
  `sill`), `SILL_DB_NAME` (default `sill`).
- `SILL_RESEARCH_MANIFEST` (default
  `<root>/docs/research-manifest.json`) — optional, for the
  "research progress" section.

**How to disable:** don't wire it. If wired and unwanted, remove the
`PreCompact` entry.

**Canned test** (writes nothing when the DB is unreachable — no
crash, no partial file):

```bash
SILL_DB_CONTAINER=sill_nonexistent python3 plugin/hooks/precompact-snapshot.py
# -> {"status": "skipped", "reason": "no content"}, exit 0.
```

---

## goodnight-checkpoint

**What it does:** On user prompts matching configured trigger phrases
(`goodnight`, `signing off`, `see you tomorrow`, …), writes a daily
markdown checkpoint at
`<project>/logs/daily-checkpoints/YYYY-MM-DD.md` summarizing today's
memory activity. Optionally updates a `drives` row's `current_focus`
field with a one-line summary.

**When it fires:** `UserPromptSubmit`, if you wire it — **not in the
default template.**

**Env vars:**

- `SILL_DB_CONTAINER` (default `sill_db`), `SILL_DB_USER` (default
  `sill`), `SILL_DB_NAME` (default `sill`).
- `SILL_PROJECT_ROOT` (default `cwd`).
- `SILL_PLUGIN_DIR` (default `parent of hooks dir`).
- `SILL_GOODNIGHT_FOCUS_DRIVE` — name of a `drives` row to update.
  Unset = skip the focus update.

**Config files:**

- `plugin/config/goodnight-triggers.txt` — one phrase per line,
  `#` for comments. Substring match, case-insensitive,
  word-bounded.

**How to disable:** don't wire it. If wired, remove the
`UserPromptSubmit` entry.

**Canned test** (works even with the DB unreachable — the memory
sections just come back empty):

```bash
printf '%s' '{"prompt": "Alright, goodnight!"}' \
  | SILL_DB_CONTAINER=sill_nonexistent SILL_PROJECT_ROOT=/tmp/sill-doctest \
    python3 plugin/hooks/goodnight-checkpoint.py
# -> {"systemMessage": "[sill] Goodnight checkpoint written -> /tmp/sill-doctest/logs/daily-checkpoints/<today>.md"}, exit 0.
```

---

## clear-handoff

**What it does:** `/clear` keeps the same Claude Code process but
mints a new session id, discarding the old transcript from context.
This hook records `{claude-pid -> session}` on every `SessionStart`,
and on `source == "clear"` looks up the stale mapping for this exact
pid to find the session the window just left, then re-injects that
session's final assistant message as background context — a natural
handoff summary instead of a cold start. Falls back to the newest
interactive transcript in the project directory (flagged as a
heuristic pick, since it can't be certain no other window wrote that
transcript dir too) when no exact mapping exists.

**When it fires:** `SessionStart`. Timeout: 10s. Only acts on
`source == "clear"`; every other source (`startup`, `resume`,
`compact`) just updates the pid mapping, silently.

Parses Claude Code transcript shapes only (JSONL turns keyed by
type/message/isSidechain). A Codex `SessionStart` event carries a
different shape and won't match anything here — it exits silently,
which is the intended degrade.

**Env vars:** `SILL_LOG_DIR` (default `/tmp`) — the pid-to-session
mapping lives at `{SILL_LOG_DIR}/cc-session-by-pid/`.

**How to disable:** remove the `SessionStart` entry whose command is
`clear-handoff.py`.

**Canned test** (a non-`clear` source is a silent no-op):

```bash
printf '%s' '{"source": "startup", "session_id": "s1"}' \
  | python3 plugin/hooks/clear-handoff.py
# -> no output, exit 0.
```

---

## Rule files (response-patterns)

The `plugin/response-patterns/` directory holds eight generic rule
files that ship by default. Each file is a self-contained unit:

```yaml
---
name: my-rule
enabled: true
patterns:
  - "regex one"
  - "regex two"
---

Warning message body. Can reference {matched}.

**Corrective action:** what the agent should do next.
```

Comment lines (starting with `#`, common inside a `patterns:` block to
annotate a regex) are skipped before frontmatter parsing looks for
`key: value` or list-item lines, so a comment containing a colon no
longer resets an in-progress pattern list.

To add your own: drop a new `<name>.md` into the directory, or point
`SILL_RESPONSE_PATTERNS_DIR` at your own directory and copy the
shipped rules over selectively. See `docs/extending.md` for the
full pattern.

---

## Adding hooks to a project

When you ran `./install.sh --scope project --hooks-for /path/to/project`
(`--scope project` is the default), the installer wrote two files:

- `/path/to/project/.codex/hooks.json` — full Codex hook config,
  rendered from `plugin/codex.hooks.json.template`, but **only if
  that file didn't already exist.** If it existed, install.sh left it
  untouched and printed a note to that effect. Re-running
  `install.sh --hooks-for` later (e.g. after upgrading to a Sill
  version with new hooks) will **not** pick up anything new on the
  Codex side — `install.sh` deliberately only does first wiring. Use
  `./upgrade.sh --hooks-for /path/to/project --hooks-only` instead: it
  diffs the existing file against the current template and, if they
  differ, prints the diff and asks for `--force-hooks` before
  overwriting anything. See the Upgrading section in `README.md`.
- `/path/to/project/.claude/settings.local.json` — merged into any
  existing `hooks` block, idempotently: existing entries are
  preserved and Sill's own are deduplicated by comparing each entry's
  JSON. Unlike the Codex side, this one *does* pick up new hooks on a
  later `install.sh --hooks-for` re-run (and `upgrade.sh --hooks-for`
  does the same merge).

`--scope home` (`./install.sh --scope home`) is the other option:
hooks go into `~/.claude/settings.json` and `~/.codex/hooks.json`
instead of a per-project file, plus an ambient instructions file at
`~/.claude/CLAUDE.md`, so every session in every directory carries the
Sill background instead of just wired projects. See `README.md`'s
"Install scope" section for the tradeoff.

Some hooks (`track-verification`, `check-agreement`,
`check-corrections`, `precompact-snapshot`, `goodnight-checkpoint`)
are **not** in the default template. To wire them, copy the relevant
block out of `plugin/codex.hooks.json.template` and edit by hand.
Examples in the comments at the top of each hook file.
