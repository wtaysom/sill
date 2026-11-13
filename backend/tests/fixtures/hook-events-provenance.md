# Hook event fixtures

Captured November 13, 2026 from isolated `--once` workers running Claude Code
2.1.296 and Codex CLI 0.162.1. The recorder ran through explicit Claude
`--settings` and Codex's normal `/hooks` trust flow, using local recording-only
commands. The fixtures retain the event name, native session and turn fields,
tool name, and tool input from the actual callback stdin. Native IDs were
replaced by fixture UUIDs and the synthetic project prefix was normalized.
The tool inputs contain only synthetic content.

The recorder omitted the `UserPromptSubmit` prompt text. These projections
establish the observed callback shape and tool-input keys. Recall output and
guard decisions require separate verification. The malformed and multi-file
test cases are synthetic and live in the test modules. The separate `*-transcript.jsonl`
and `*-rollout.jsonl` fixtures represent raw session records.
