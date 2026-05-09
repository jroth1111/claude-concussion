# Compaction Hydration Runbook

Claude Code compaction is lossy. A compact summary can preserve the broad shape of a session while losing the binding details: the latest directive, modified files, unfinished verification, background commands, or the reason an action was unsafe.

Claude Concussion treats the compact summary as a dehydrated packet. It is useful signal, but it is not authority until rehydrated against transcript, tool, file, and state evidence.

## Installed Surfaces

| Surface | Role | Hook event |
| --- | --- | --- |
| `hooks/state-snapshot.sh` | Arms/surfaces rehydration state | `PreCompact`, `SessionStart`, `SessionEnd` |
| `hooks/postcompact-capture.py` | Records compact summary as an unverified projection | `PostCompact` |
| `hooks/write-barrier.py` | Blocks edit-class tools while rehydration is required | `PreToolUse` |
| `hooks/rehydration-clear-guard.py` | Blocks raw flag deletion and unsafe Bash while dehydrated | `PreToolUse:Bash` |
| `hooks/rehydration-clear.py` | Clears the barrier after packet validation | manual helper |
| `hooks/rehydration-stop-linter.py` | Blocks final answers without a recovery packet | `Stop` |
| `hooks/evidence-capture.py` | Records recent tool evidence and heartbeat | `PostToolUse` |
| `hooks/secrets-guard.py` | Blocks direct secret/transcript/state exposure | `PreToolUse` |
| `hooks/secret-redactor.py` | Prints redacted views of protected files | manual helper |
| `hooks/bash-safety-gate.py` | Blocks destructive shell shortcuts | `PreToolUse:Bash` |
| `hooks/posttoolbatch-reconcile.py` | Records batch heartbeat state | `PostToolBatch` |
| `hooks/subagent-lifecycle.py` | Adds/requires subagent handoff boundaries | `SubagentStart`, `SubagentStop` |
| `hooks/task-lifecycle.py` | Adds/requires task creation/completion evidence | `TaskCreated`, `TaskCompleted` |
| `hooks/rehydration-schema.json` | Source of truth for recovery packet fields | consumed by hooks |

## Hydration Protocol

When `state-snapshot.sh` observes compaction, it creates a rehydration flag under the Claude state directory. While that flag exists:

- edit-class tools are denied;
- Bash is narrowed around recovery-safe behavior;
- direct flag deletion is denied;
- final answers are blocked unless a recovery packet exists.

Claude must reconstruct the working state before continuing. The packet must include:

- `DIRECTIVE_CANDIDATES`
- `SELECTED_DIRECTIVE`
- `ACTIVE_CONTRACT`
- `LAST_USER_INTENT`
- `BD_ISSUE`
- `OPEN_ITEMS`
- `CURRENT_FRONTIER`
- `LAST_VERIFIED_BOUNDARY`
- `MODIFIED_FILES`
- `ACTIVE_BACKGROUND_TASKS`
- `COMPACT_SUMMARY_STATUS`
- `EVIDENCE_CONSULTED`
- `NEXT_ACTION`
- `STOP_CONDITION`

The barrier is cleared with:

```sh
python3 ~/.claude/hooks/rehydration-clear.py \
  --session-id "<session_id>" \
  --reason "reconstructed active contract, latest directive, file frontier, and verification state"
```

If the helper cannot infer the right transcript, pass `--transcript-path`.

## Evidence

`evidence-capture.py` records durable breadcrumbs in `~/.claude/state/evidence-index.jsonl`:

- command exit states and failures;
- read/search paths;
- write/edit paths;
- verification-looking command results;
- stable evidence IDs when available.

It records metadata, not full file contents. This avoids turning evidence storage into a prompt-injection or secret-exfiltration surface.

## Summary Status

`postcompact-capture.py` records compact summaries as `projection_unverified`. A compact summary can guide recovery, but it does not outrank:

- post-compact user messages;
- queued commands or attachments;
- current file contents;
- tool outputs and evidence records;
- explicit tracker/task state.

If these conflict, the transcript and durable state win over the summary.

## Bash During Recovery

Bash parsing is not a complete sandbox. The durable enforcement path is the edit-class write barrier. The Bash hooks add defense in depth:

- block direct mutation of rehydration flags;
- block destructive shortcuts such as forced reset, force push, destructive clean, and shell-piped installers;
- block direct export/reading/archive of protected Claude transcript and secret surfaces;
- allow redacted inspection through `secret-redactor.py`.

## Manual Smoke Checks

Validate the settings fragment:

```sh
python3 -m json.tool templates/settings.fragment.json >/dev/null
```

Compile Python hooks:

```sh
python3 -m py_compile install.py scripts/*.py payload/hooks/*.py
```

Install into a temporary Claude home:

```sh
tmp_home="$(mktemp -d)"
python3 install.py --claude-home "$tmp_home"
python3 scripts/doctor.py --claude-home "$tmp_home"
```

Build the distributable:

```sh
python3 scripts/package.py
zip -T dist/claude_concussion.zip
```
