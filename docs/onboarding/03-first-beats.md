# Phase 6: Supervised First Beats

**Done when:** every configured voice has logged `Beat complete` at least
once, the files they wrote exist, and you have read one of them.

Supervised means exactly what it says: the worker runs in the foreground, on
a short interval, while you watch the log. Nothing is scheduled in this
phase. You are answering one question — *does a beat, run by this machine,
with these permissions, actually produce something?* — and you want the
answer in five minutes rather than in two hours of silence.

Do phase 4 (`docs/onboarding/01-install.md`) first. A beat with unverified
permissions exits 0, writes nothing, and reports plausibly. This phase
cannot distinguish that from a working install if you skip it.

## Configure the voices

```bash
cp backend/beats.example.json beats.json
mkdir -p notes journal logs/analyst logs/reflector
```

The example ships two voices — an analyst that measures, tests and builds,
and a reflector that thinks about something without needing to produce an
artifact. Each declares a standing prompt, a transcripts directory, and an
`output_glob`. Keep the `output_glob`s to catch a successful CLI outcome
that produced no new matching file. Both starter voices use Claude Code.
To run a voice with Codex, set `"launcher": "codex"` and optionally `cli`
to the absolute path from `command -v codex`. Run that voice with its own
project trust and tool permissions. See [voice configuration](../beats.md#voice-config-format).

## Seed the first beats with something to bite on

A first beat in an empty room writes about the empty room. It orients, finds
no prior entries, no open questions, nothing anyone wants to know, and
produces a competent paragraph about beginning. That entry is not a system
failure and it is also not worth reading.

So put one or two real questions where the voices will find them while
orienting — in `notes/` for the analyst, in `journal/` for the reflector, or
in the `kickoff` line of `beats.json` if you want to be direct about it.
Take them from your charter if you have already drafted it, or from whatever
you actually want to know: something checkable for the analyst, something
open for the reflector. A question the beat can fail at is better than a
theme it can only elaborate.

Check the question against the beat's tools before you seed it. A first beat
can shell out only to what phase 4's allow-list permits, and it reaches
memory through the `sill` MCP server rather than the CLI — there is no `sill
recall` subcommand. "Does recall return anything useful for the last three
things I worked on?" is a good question and a bad *first* one: a beat with
the mint path granted and no MCP connection answers it by explaining that it
cannot, which is honest and not what you wanted to learn. Ask the first
analyst beat something it can reach: what is in the store already, what the
identity card says, what the last transcript actually contains.

## Run it by hand

```bash
SILL_BEAT_INTERVAL_SECONDS=1 sill-worker --mode beat
```

The one-second interval makes the loop fire again immediately instead of
waiting two hours. Run it in the foreground and read the log as it goes.
The startup banner names every voice and the guard scope it derived:

```text
INFO -   Voice [analyst]: prompt=prompts/analyst.md, transcripts=logs/analyst
INFO -   Voice [reflector]: prompt=prompts/reflector.md, transcripts=logs/reflector
INFO -   Guard scope (SILL_BEAT_JOURNAL_DIRS for each child): 'notes/:logs/analyst/:journal/:logs/reflector/'
```

Note that last line — phase 7's drill uses it. The worker derives the guard
scope from your own `beats.json` on every spawn; you never set it by hand.

Then, per beat, exactly one outcome line. Success looks like this:

```text
INFO - [analyst] Beat complete in 0.3s — transcript 12345678-1234-4123-8123-123456789abc.txt
INFO - [reflector] Beat complete in 0.1s — transcript abcdefab-abcd-4abc-8abc-abcdefabcdef.txt
```

"Complete" means the launcher succeeded and the voice's `output_glob` gained a new file.
A real beat takes minutes, not the fractions of a second above.

The failure this phase exists to catch looks like this:

```text
WARNING - [analyst] Beat exited 0 in 0.3s but produced no file matching 'notes/analyst-*.md' — the agent CLI may be denying tools non-interactively; see docs/beats.md permissions. Rotation remains on [analyst].
```

Exit 0, no file. Go back to phase 4. Note the last sentence: rotation does
**not** advance past a failed voice, so the same voice retries next
interval. That is right for the voice and consequential for everything else
— the rotation index is shared, so one stuck voice starves every other voice
indefinitely, and the schedule keeps ticking the whole time. If a configured
voice's name never appears in your log, it is not idle; it is blocked behind
whichever voice does appear. `docs/beats.md`'s "Rotation starvation" section
covers diagnosis.

Once each voice has logged `Beat complete` once, press Ctrl-C. Interrupting
an in-flight later beat is expected, not a failure.

Ctrl-C prints one line and exits 0:

```
Interrupted (Ctrl-C) — beat worker stopped. Rotation stays on [analyst]; starting the worker again resumes there.
```

The interrupted beat does not count as taken, and its agent-CLI child is
killed along with the worker. A Python traceback here instead means you are
on a build from before this was checked against a real terminal — the worker
had no interrupt handler until then, and the documented way to stop it
printed a `KeyboardInterrupt` stack.

## Read what they wrote

```bash
ls notes journal
cat "$(ls -t logs/analyst/*.txt | head -1)"
```

Open the `.json` receipt with the same UUID as the `.txt` summary. Check
that its status is `succeeded`, `state_advanced` is `true`, and
`new_outputs` lists the file you expected. `spawned_at` and `completed_at`
are recorded by the worker. `requested_model` shows the launcher argument,
if any, and `effective_model` identifies the model when the worker has
evidence from the host. The summary holds the CLI's final message. Use the
receipt's `host_session_id` to find the host's execution record if you need
to inspect tool calls.

Which is why the actual done-condition of this phase is the last one, and it
is yours rather than the software's: **open the note and the journal entry
and read them.** Not to grade the prose. To confirm that something specific
happened — that the analyst measured a real thing and the reflector followed
a real thread, rather than both producing well-formed reports of work that
left no trace. If what you find is fluent and empty, the problem is usually
the standing prompt or the seeded question, and both are yours to rewrite:
`prompts/analyst.md` and `prompts/reflector.md` are starting points, not
fixtures.

---

Next: `docs/onboarding/04-seeded-fault-drill.md` — where you break one on
purpose.
