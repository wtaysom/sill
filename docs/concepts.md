# Concepts

How Sill is shaped, and why. Read this before you start customizing.

---

## What's in the database

The squashed schema at `backend/schema.sql` creates 43 tables on a
Postgres 16 instance with `pgvector` and Apache AGE installed. They
fall into seven groups.

### Memories (the core)

- `memories` — the parent table. Every memory has an `id`, `type`,
  `content`, `embedding` (vector), `importance`, `created_at`,
  `last_reused`, `reuse_count`, and `archived_at`.
- `episodic_memories`, `semantic_memories`, `procedural_memories`,
  `strategic_memories` — type-specific extensions, joined by id.
- `working_memory` — short-lived staging for in-progress thought.
- `ingestion_receipts` — provenance for batch imports (seed pack,
  external ingest jobs).

The four type tables exist because different memory shapes carry
different metadata (a procedural memory has a `recipe`, a strategic
memory has a `decision_context`, etc.). For most read paths you query
`memories` directly and join only when you need the type-specific
fields.

### Concepts and edges

- `concepts` — a vocabulary of named concepts (`"verification"`,
  `"attribution"`, …) plus their embeddings.
- `memory_concepts` — many-to-many between memories and concepts.

This is the routing layer that lets a query for "what do we have
about attribution?" find memories that don't textually contain the
word "attribution".

### Identity / worldview

- `worldview_primitives` — the agent's load-bearing positions
  ("memory is a routing layer, not a knowledge layer").
- `identity_aspects` — durable shape-of-self entries.
- `worldview_memory_influences`, `identity_memory_resonance` —
  link tables tying memories to the worldview/identity rows they
  reinforce or contradict.

Most installs won't touch these tables directly. They exist because
the upstream agi-memory project uses Sill as a research substrate;
you can ignore them for normal recall.

### Goals and drives

- `goals` — work items with `priority` (`active` / `queued`
  / `completed` / `abandoned`), `title`, `description`, timestamps.
- `drives` — longer-arc orientations with a `current_focus` field.
  `precompact-snapshot` and `goodnight-checkpoint` read/write this.
- `goal_memory_links` — ties memories to the goal they came out of.

### Maintenance config and state

- `maintenance_config`, `config` — knobs.
- `maintenance_state` — worker state machine.
- `heartbeat_state` — a singleton row left over from the retired
  heartbeat worker, but still live: `rabbit_bridge.py`'s inbox poll
  stamps its `last_user_contact` column after ingesting each RabbitMQ
  message. Nothing else in this codebase reads or writes it.
- `external_calls`, `outbox_messages` — for the RabbitMQ message bus.

The `maintenance_worker` runs by default and handles importance decay,
drift tracking, and other housekeeping. Reflective processing ships as the beat 
worker (see `docs/beats.md`), which is off until you turn it on.

### Embedding cache

- `embedding_cache` — content-hash → vector, so re-importing the same
  text doesn't re-call the embeddings service.
- `embedding_config` — records the locked model + dimension at
  install time.

### Clusters, episodes, and the rest

- `memory_clusters`, `memory_cluster_members`, `cluster_relationships`,
  `memory_neighborhoods` — graph-of-memories structures used by synthesis.
- `episodes`, `episode_memories` — temporal groupings.
- `memory_changes`, `importance_updates`, `boundaries`,
  `emotional_states`, `relationship_discoveries`,
  `health_snapshots`, `synthesis_attempts`,
  `synthesis_attempt_concepts`, `quality_pattern_suggestions`,
  `exploration_requests`, `review_queue` — bookkeeping for the
  longer-loop research features. Safe to ignore for a basic recall
  install.

If you only ever call `recall` and `remember`, you'll touch maybe
four of these tables. The rest are there for the maintenance and
research workflows to grow into.

---

## Memory shape: the two-shape rule

Sill memories should fit one of two shapes:

### Source Card

A verbatim quote with provenance, plus why it mattered.

```
"Memory retrieval is in-context programming." — Alice, 2024-11-03,
design discussion. Frames recall as pathway-steering, not just lookup.
```

Fields that make this load-bearing: the **verbatim quote**, the
**source pointer** (who said it / where / when), and a sentence
about **what discriminator it supports** ("if recalled memories
didn't change response shape, this frame would be wrong").

### Decision / Bridge

A mapping the agent has adopted, plus what would revise it.

```
Decision (2026-02-15): when recall returns >3 hits, default to
recall_preview + selective recall_batch. Revise if preview snippets
prove too short to discriminate on >30% of queries.
```

Fields: the **decision**, the **revision condition**, and (often)
a **bridge to the framework it came from**.

### The quality gate

Seed memory #7 of `seed/methodology.jsonl` encodes the rule:

> Before storing a memory, check the draft has at least one of: a
> verbatim quote with source, a falsifier ("this would be wrong
> if…"), or a concrete example. Stable preferences/decisions are OK
> without these but should name what they're a preference *over*.
> If the draft is just a restated observation with none of the
> above, ask before storing.

This is the single most important rule in the methodology pack. It
prevents the corpus from filling up with restated trivia.

---

## A memory is a speech act

A store that treats every row as a fact goes wrong in three ways. It
fact-checks a promise as if it were a claim. It lets whoever typed a
sentence into the store pass for the person who said it, or for the one
who worked it out. And it takes the memories it shows most often for the
ones that matter most. Sill records enough about each memory to keep
these apart.

### Who said it: `speaker`

A memory can name whose utterance it records. When the person you work
with says "use tabs in this repo", the speaker is that person, even
though an assistant made the tool call that stored it. When the
assistant reaches a conclusion of its own, the speaker is the assistant,
under whatever name the instance goes by. A quotation from a book has
its author as speaker.

```bash
sill notice "Use tabs, not spaces, in this repo." --speaker Inez --force directive
```

`recall` can be restricted to one speaker (`speaker="Inez"`). The filter
applies before ranking, so the result is the best of what Inez said and
not the Inez rows that happened to make an overall top ten. The
`speakers` tool lists who is in the store, and `#Nobody` selects
memories recorded with no speaker. Nothing guesses a speaker: a memory
stored without one stays without one.

### What kind of act: `force`

The five kinds are Searle's. Each succeeds in its own way.

| Force | Example | It succeeds when |
|---|---|---|
| `assertive` | "The parser drops trailing commas." | it is true |
| `directive` | "Run the linter before every commit." | it is complied with |
| `commissive` | "I'll write up the migration notes." | it is kept |
| `expressive` | "I'm glad we dropped the old importer." | it is sincere |
| `declaration` | "This design is approved." | the speaker had the standing to make it so |

Only an assertive can be checked for truth. Fact-checking an instruction
makes no sense; the questions for it are whether it still stands and
whether it is being followed. A memory with no force is read as
assertive, which is what every row was before the field existed.

Storing is itself always an assertion: the row claims that this act took
place. So a stored promise can fail in two different ways, as a record
(nobody promised that) and as a promise (it was not kept).

### Who did the work: `production`

The speaker is whose utterance a row was. Who did the thinking behind it
is a separate question, and the answers often differ: a person reads out
a conclusion the assistant reached, or an assistant writes up an
experiment the person ran. `production` records the contributors when
someone declares them.

```bash
sill notice "Batching the embedding calls cut import time from 40 s to 6 s." \
  --speaker Inez \
  --made-json '{"made":[{"kind":"person","who":"Inez","work":"ran the timing experiment","at":[],"declared":{"by":null,"when":"2026-11-13T00:00:00Z","basis":"stated"}}]}'
```

Production is declared and never inferred. A memory with no production
is *undeclared*: nobody has said who made it. That is different from
saying nobody made it, and Sill does not fill the gap from the speaker,
the session, or the most recent transcript.

The object form above works everywhere. Inside a Claude Code or Codex
session there is a session to name as the declarer, and the short form
`--made "Inez: ran the timing experiment [stated]"` is accepted too. A
desktop app or a hosted chat has no such session, so the short form is
refused there. See [attribution and perspective
recall](migrations/011-perspective-recall.md) for the full grammar.

### The same words from another mouth

If the same speaker stores the same words twice, the second call returns
the first memory untouched. If a different speaker stores those words,
Sill keeps a separate memory and links it to the earlier one as a
restatement. Two people agreeing is recorded as two acts, and neither is
rewritten as the other's.

Replacing a memory is a different operation. `sill notice --supersedes
<id>` archives the old memory and links the new one to it, and the
`follow_supersedes` tool walks how a position changed. Quoting someone
does not supersede them, and neither does restating them. See [atomic
memory creation](migrations/010-atomic-mint.md).

### Shown is not used

Recall displays memories. That is *exposure*, and Sill counts it. It
does not make a memory look more recent or more important, because then
whatever was shown most would go on being shown. Recency moves only on
explicit use: fetching a memory by its id or, where the `track-reuse`
hook is installed, citing that id in a response. Access counts never
feed importance. Importance changes when someone decides it should,
through `update_importance`.

### Commitments can end

A promise stays live until something ends it. `link_commitment` gives a
commitment one home goal. Closing that goal, by setting it to
`completed` or `abandoned` with `change_goal_priority`, releases the
commitments that live there and only those: each is archived with the
goal named as the reason. Reopening the goal restores only the ones that
this closing released. A promise is neither kept forever by default nor
dropped without a trace.

---

## Embeddings (locked at install)

The Docker stack runs HuggingFace TEI with
`unsloth/embeddinggemma-300m` by default. That produces **768-dim**
vectors.

The Postgres schema is parameterized on `app.embedding_dimension` (a
GUC set from `EMBEDDING_DIMENSION` in `.env`), but the vector column
types in the schema are bound at table-creation time. Changing the
model after install means:

1. `docker compose -f backend/docker-compose.yml down -v` (drops the
   `postgres_data` volume — **data loss**).
2. Change `EMBEDDING_MODEL_ID` and `EMBEDDING_DIMENSION` in `.env`.
3. Bring the stack back up (re-creates the schema with new dims).
4. Re-seed and re-import any external memories.

In practice: pick your embedding model before first install and
don't change it. If you do change it, `./reset.sh` is the safer path
than hand-editing.

The default model is small by current standards (300M parameters — ~1.2 GB
of float32 weights on disk, not the ~300MB the name suggests),
CPU-friendly, and good enough for the kinds of recall sill is doing. If
you want to swap in a heavier model later, do it on a fresh install in its
own stack — see the README's "Run a second Sill side by side", which takes
more than `SILL_DB_CONTAINER` alone — and migrate by exporting and
re-importing memories.

---

## Recall

The recommended pattern is **progressive recall**.

### Layer 1: preview

```python
# MCP tool: mcp__sill__recall_preview
{"query": "what do we know about attribution?", "limit": 10}
# Returns: [{id, type, importance, snippet}, …]  ~100 chars each.
```

This is cheap. You get the shape of the result set without spending
tokens on full content.

### Layer 2: batch fetch

```python
# MCP tool: mcp__sill__recall_batch
{"ids": ["uuid-1", "uuid-3"]}
# Returns full memory rows for the IDs you actually want.
```

Selectively pulling 2 of 10 saves roughly 75% of the tokens versus
calling full `recall`. Worst case (you end up wanting all 10) is
about 6% overhead.

When to skip preview: enumerative queries ("all memories about Z"),
or queries where you specifically want the full content and don't
want a second round trip. The seed pack memory #13 documents this:

> Default to recall_preview + selective recall_batch for open-ended
> queries. Switch to full recall when the query is enumerative or
> when preview snippets are too short to discriminate.

### The methodology pack

`sill seed import seed/methodology.jsonl` loads 22 memories that
encode the inquiry / verification / recall / storage discipline this
project has converged on. They're tagged so spontaneous-recall surfaces
them in relevant prompts. You don't have to use them as written — but
they're a tested starting point.

---

## Workers

### Maintenance (default-on)

Started by `docker compose up -d … maintenance_worker`. Runs
`sill-worker --mode maintenance`. Currently does importance decay
and drift tracking; the loop is conservative and idempotent.

Logs: `docker compose logs maintenance_worker`.

### Beat worker

Reflective processing ships as the beat worker (see `docs/beats.md`), which is off
until you turn it on.

---

## Apache AGE graph layer

Sill ships with Apache AGE installed and a `memory_graph` graph
created. Vertex labels: `MemoryNode`, `ConceptNode`, `SelfNode`,
`LifeChapterNode`, `TurningPointNode`, `NarrativeThreadNode`,
`RelationshipNode`, `ValueConflictNode`.

The MCP server exposes some graph-shaped queries (`find_causes`,
`follow_supersedes`, `relate_concepts`), but most recall paths use
the relational tables (`memories`, `memory_concepts`, `concepts`).

If you don't need graph queries, you can ignore AGE entirely. It's
installed because the schema requires the extension; it costs almost
nothing when unused.

If you do want to extend along the graph, the conventions are:

- `MemoryNode` and `ConceptNode` are kept in sync with the `memories`
  and `concepts` tables.
- Edges live in AGE only — there's no relational mirror of
  `(MemoryNode)-[:SUPERSEDES]->(MemoryNode)`. Query them with
  Cypher via the `cypher()` SQL function.

See `backend/schema.sql` for the graph-creation block and edge-type
enum.

---

## Where to look next

- `docs/hooks.md` — what each shipped hook does and how to disable
  any you don't want.
- `docs/extending.md` — writing good memories, the quality gate,
  adding your own hooks and rule files, the env-var cheat sheet.
- `backend/sill_mcp_server.py` — the full MCP tool surface (a few
  dozen tools, all under `mcp__sill__*`).
- `seed/methodology.jsonl` — the 22 seed memories, readable as
  plain JSONL.
