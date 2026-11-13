# Attribution and perspective recall (011)

Public clients can declare `force`, `speaker`, `production`, and `minted_in`
with `remember` and `MemoryInput`. `production` is optional: omitted or JSON
null remains SQL NULL. It describes declared intellectual contributors;
`speaker` records whose act the row records. Neither implies the other.
All declarations are validated before the atomic mint call. A reused act
keeps its existing declaration, context, and concepts.

Use a typed production object for sessionless clients, for example:

```json
{"made":[{"kind":"person","who":"Inez","work":"experiment","at":[],"declared":{"by":null,"when":"2026-11-13T00:00:00Z","basis":"stated"}}]}
```

CLI notice accepts `--made-json` for that object or `--made` for the compact
`who [as Voice]: work [@ address] [basis]` grammar. Model voices are arbitrary
nonempty names. Model identities must be precise IDs, or null with a supplied
session. A compact `self` declaration requires a session. Nothing infers an
author, fills an absent model from a nearby transcript, or scans for the newest
transcript. Readers explicitly render undeclared production as such.

`session_context` accepts host IDs from `CODEX_THREAD_ID`,
`CLAUDE_CODE_SESSION_ID`, `CLAUDE_SESSION_ID`, or a hook payload. Conflicting
host IDs remain unknown unless the payload disambiguates them.
`SILL_SESSION_ID` supplies an explicit correlation scope;
`SILL_SESSION_ID_SOURCE=worker_attempt` and `SILL_ATTEMPT_ID` describe a worker
attempt, independently of `host_session_id`. Optional `SILL_VOICE`,
`SILL_HOOK_SESSION_ID`, `SILL_HOOK_TRANSCRIPT_PATH`, and `SILL_HOOK_CWD` provide
explicit runtime evidence. Model fields remain null unless a caller supplies
an explicit `minted_in` object. No model receipt resolver is bundled here.

`recall(speaker="Inez")` filters both vector and full-text candidates before
their limits. `speaker="#Nobody"` selects NULL speakers, never a stored
person of that name. `speakers()` lists nonreserved declared speakers, omitting
archived rows. `public.hybrid_recall_perspective` and its AGE namespace wrapper
share one implementation; existing `hybrid_recall` calls remain compatible.
Rank fusion still uses k=60; public `score` remains a rank score, with top
single-list/both-list values of 0.0164/0.0328.

## Optional Chorus stage compatibility

Installed MCP discovery omits `stage` by default, and direct dispatch rejects
it too. Chorus is outside the memory wheel. Source-checkout operators who
want this capability must install Chorus and its provider dependencies
separately, then explicitly set `SILL_ENABLE_STAGE=1` in the MCP process.
The capability writes the requested transcript file and calls external models;
its credentials and provider configuration remain the operator's responsibility.
No provider packages or credentials are included in the memory wheel.

Opt-in with missing Chorus support still omits the tool; direct dispatch gives
an actionable missing-support error. Unset `SILL_ENABLE_STAGE` to disable both
paths. This changes earlier discovery, which advertised a tool that a clean
installation could not import.
