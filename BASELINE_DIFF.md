# Baseline Comparison

## Vanilla Package

Expanded package:

```text
@anthropic-ai/claude-code@2.1.138
```

Observed files:

```text
package/LICENSE.md
package/README.md
package/bin/claude.exe
package/cli-wrapper.cjs
package/install.cjs
package/package.json
package/sdk-tools.d.ts
```

Vanilla Claude Code is the CLI/runtime package. It does not include a project-independent compaction recovery barrier, write barrier, post-compact capture, transcript evidence ledger, or rehydration stop linter.

## Added By This Installer

This installer adds a Claude settings hook layer:

- `PreCompact`: arms/surfaces compaction state through `state-snapshot.sh`
- `PostCompact`: captures compact summary and resurfaces state
- `SessionStart`: resurfaces recovery state after startup/resume/compact
- `PreToolUse`: blocks secret exposure, dangerous shell forms, raw rehydration flag mutation, and edit-class tools while recovery is pending
- `PostToolUse`: records evidence and heartbeat
- `Stop`: blocks final answers while a required recovery packet is missing
- subagent/task lifecycle hooks: require structured handoff boundaries

The installer writes only Claude-owned surfaces. Codex needs a separate native adapter and is intentionally outside this package.
