# Context Recovery After Compaction

Read when a compacted summary says generated content is missing/wrong in current files, when verbatim recovery is needed, or when current files conflict with the summary. The headline rule is in `CLAUDE.md`.

If compacted summary says generated content missing/wrong in current files, recover verbatim from JSONL (`~/.claude/projects/<project-hash>/<session-id>.jsonl`) instead of reconstructing:

1. Grep distinctive keyword for lines.
2. `python3` + `json.loads()` line.
3. Filter `message["content"]` for `tool_use` blocks where `name == "Write"` or `"Edit"`.
4. Extract `input["content"]` (Write) or `input["new_string"]` (Edit).

Use when summary says X generated but file missing/wrong, verbatim needed, or current files conflict with summary. Don't use when summary sufficient or content reconstructable from current state. Complements Execution Discipline: conversation memory lossy under capacity pressure; prefer artifacts (ledger, code, transcripts, checkpoints) over recall.
