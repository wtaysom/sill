# Atomic memory creation (migration 010)

Migration 010 makes a live utterance unique by `coalesce(speaker, '')` and the
MD5 digest of its full content. A same-speaker retry returns the original UUID
without changing any metadata, subtype, graph node or concepts. NULL and empty
speaker share one namespace. Different speakers receive distinct records; the
later mint points `restates_id` to the earliest live identical text from another
speaker, ordered by `created_at, id`. Neither record is superseded or archived.
Content locks serialize concurrent speakers before this lookup. Digest collisions
with different full text raise an explicit error rather than returning a false match.

Before adding any schema objects, the migration locks the table and checks for
existing duplicate live speaker/digest groups. Failure reports every conflicting
UUID and rolls back the migration. Review those rows with their owners and
explicitly resolve the duplicates, then retry the upgrade. The migration never
chooses a winner or archives data automatically.

## SQL and compatibility

`public.mint_memory` returns `(memory_id uuid, outcome text, restates uuid)`.
`public.mint_memory_with_embedding` adds a required vector after content and a
trailing optional `p_embedding_profile text`. `public.mint_memories(jsonb)`
returns ordered `(idx, memory_id, outcome, restates)` rows. Both single-record
entrypoints delegate to `public._mint_memory`; JSON and legacy batches call these
entrypoints within one statement. Base row, graph, optional subtype and concepts
roll back together. Reused results carry `outcome='reused'` and `restates=NULL`;
that receipt does not alter the row's existing restatement link.

SQL NULL and JSON-null `type_metadata` request a bare record. `{}` requests a
subtype with ordinary creation defaults (semantic confidence 0.8, procedural
`{"steps":[]}`, episodic event time now, strategic confidence 0.8). Typed APIs
supply explicit metadata. Supersession retains migration 009's separate current
fact defaults, historical source envelopes, concept inheritance/clearing and
production declaration rules. It archives the predecessor before invoking mint;
any failed successor, or an already standing successor, restores the predecessor.
The successor also receives its graph node.

Compatibility decisions:

- UUID and UUID-list return shapes remain unchanged on Python and legacy SQL
  creators. Receipts from the Python single/JSON-batch paths are logged; CLI
  reuse/restatement notices go to stderr, preserving the stdout receipt format.
- Both legacy generic creators mint a bare NULL-speaker row. They cannot reuse or
  retag another speaker's row. The old cross-speaker content-only dedup is removed.
- `ag_catalog.create_memory` retains its historical semantic 30-character and junk
  pattern guards. `public.create_memory` and the new public mint require nonempty
  content without those guards. Typed wrappers use the named public mint; their
  behavior no longer depends on a caller's search path.
- `remember_batch_raw` and its SQL adapter retain the baseline's safe-default
  subtypes on new rows: episodic `context={"type":"raw_batch"}`, semantic
  confidence 0.8 and empty references, procedural `{"steps":[]}`, strategic
  content as pattern with confidence 0.8. These defaults are supplied to mint,
  never attached after reuse. The new generic vector mint with NULL/JSON-null
  metadata remains bare, as does legacy single-record vector creation.
- The legacy vector entrypoints are replaced with one definition each, adding a
  trailing optional profile argument. Existing arities still resolve. Non-NULL
  profile identifiers raise an actionable error until embedding-regime support
  is installed; they are never silently ignored. Generated and supplied vectors
  use the same atomic core; raw batches validate length, dimensions and finite
  values before writes and never call the embedding service.
- Clients require migration 010. They do not probe and fall back to create-then-
  retag on errors. A CLI timeout reports failure; retrying the complete request
  is safe. No prefix-of-content recovery can adopt an unrelated row.

## Creation-route inventory

| Supported route | Atomic destination / behavior |
| --- | --- |
| `CognitiveMemory.remember` / sync wrapper, MCP remember | `public.mint_memory`, explicit subtype metadata and concepts |
| `CognitiveMemory.remember_batch` / MCP batch | `public.mint_memories`, typed metadata and concepts per item |
| `CognitiveMemory.remember_batch_raw` / sync wrapper | `public.batch_create_memories_with_embeddings` → supplied-vector mint; legacy subtype defaults, one transaction |
| `memory_tools.MemoryFormation`, worker tool handlers | Existing API remember route; no independent INSERT |
| `sill.notice`, CLI notice, `ingest_md_memories`, auto-store hooks and beat CLI calls | `public.mint_memory`, speaker/force/concepts in the mint statement; bare |
| `scripts.decompose_event` | Named public mint for each act inside the event transaction; no retag UPDATE |
| `scripts.seed_import` | NULL-speaker precheck, then named public mint with explicit seed subtype facts; fake-vector test path uses supplied-vector mint, never raw INSERT |
| `public.create_memory`, `ag_catalog.create_memory` | Bare NULL-speaker mint; AGE guard retained as above |
| Four `public.create_<type>_memory` wrappers | Typed NULL-speaker mint; retries cannot insert duplicate subtypes |
| `public.batch_create_memories` | Typed wrappers within one statement; original item/default schema retained |
| `public.create_memory_with_embedding`, vector batch | Supplied-vector mint, NULL-speaker; single is bare, batch supplies legacy defaults |
| Both `public.supersede_memory` overloads | Migration 009 semantics; archive-before-mint with transaction rollback |

All four baseline `INSERT INTO memories` function bodies are superseded by the
010 core/wrappers or the 008–010 supersession lane. Baseline and migrations
001–009 remain immutable. Direct administrative INSERT/UPDATE is not a creator
API; the unique index still rejects a duplicate live utterance, while repair and
history tools retain explicit access to raw records.

The cumulative callable namespace and argument manifest is
`backend/tests/fixtures/function-contracts.json` (`v03_m2`). Integration tests
compare fresh and upgraded function definitions and invoke both public-first and
AGE-first search paths. They exercise real concurrent connections, long content,
NULL/empty speaker equivalence, complete graph/subtype/concept rollback,
preflight refusal, source/trust normalization and public writer return shapes.

— Sili
