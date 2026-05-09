# Claude Concussion

Post-compaction recovery hooks for Claude Code.

When Claude Code compacts a long session, it can lose the thread: the active task, the current file state, the last thing you told it not to touch. It comes back sounding confident, but it's working from a summary, not from what actually happened.

**Claude's code compaction feels like it went through a shredder. Half the context just vanishes into the void. Claude compacts code the way airlines handle luggage: technically it arrives, but a lot seems to go missing.**

Claude Concussion adds a barrier. After compaction, Claude has to rebuild its working state from real evidence before it can edit files or give a final answer.

```text
claude: compacted -> concussed -> hydrate context -> continue
```

## How It Works

Hooks fire at each stage of the Claude Code lifecycle:

- `PreCompact` sets a rehydration-required flag before the context window is compacted.
- `PostCompact` saves the compact summary as an unverified snapshot.
- `SessionStart` restores the rehydration state when Claude resumes.
- `PreToolUse` blocks writes (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`), secret exposure, raw flag deletion, and dangerous shell commands until rehydration is done.
- `PostToolUse` logs evidence from reads, edits, searches, commands, failures, and verification runs.
- `Stop` blocks final answers until a valid recovery packet exists.
- Task and subagent hooks enforce structured handoff packets at spawn and exit.

Claude cannot write to the repo or close the session until it proves it knows what it was doing.

## The Rehydration Protocol

When the rehydration flag is set, Claude must produce a recovery packet covering:

- directive candidates
- selected directive
- active contract
- last user intent
- open items
- current frontier
- last verified boundary
- modified files
- active background tasks
- compact summary status
- evidence consulted
- next action
- stop condition

`rehydration-clear.py` validates the packet against the schema. If it passes, the barrier clears.

## Why This Exists

Vanilla Claude Code has no post-compaction recovery step.

This package started as a hardened local control layer built on top of `@anthropic-ai/claude-code`. Claude Concussion is the portable extraction: hooks, schema, settings fragment, docs, installer, and doctor check — no personal wrapper dependencies.

## Install

### Agent Install

If you are an LLM/coding agent installing this for a user, do the install in this order:

```sh
git clone https://github.com/jroth1111/claude-concussion.git
cd claude-concussion

python3 -m py_compile install.py scripts/*.py payload/hooks/*.py
python3 -m json.tool templates/settings.fragment.json >/dev/null

python3 install.py --dry-run
python3 install.py
python3 scripts/doctor.py
```

Then tell the user to restart Claude Code or start a fresh Claude Code session so the new hook settings are loaded. In the new session, verify hook loading with:

```sh
claude --debug
```

or with Claude Code's `/hooks` UI.

Do not copy files by hand unless the installer fails. The installer merges the hook settings, backs up the existing `settings.json`, renders the local Claude home path into the hook commands, and writes an install receipt.

### Manual Install

Dry run:

```sh
python3 install.py --dry-run
```

Install into `~/.claude`:

```sh
python3 install.py
```

Install into a different Claude home:

```sh
python3 install.py --claude-home /path/to/.claude
```

The installer backs up `settings.json` before writing. Pass `--no-backup` to skip.

## Verify

Before installing:

```sh
python3 -m py_compile install.py scripts/*.py payload/hooks/*.py
python3 -m json.tool templates/settings.fragment.json >/dev/null
```

After installing:

```sh
python3 scripts/doctor.py
```

Then check hook loading in Claude Code:

```sh
claude --debug
```

or use Claude Code's `/hooks` UI.

## What Gets Installed

Into `~/.claude/hooks`:

- `state-snapshot.sh`
- `postcompact-capture.py`
- `rehydration_schema.py`
- `rehydration-schema.json`
- `rehydration-clear.py`
- `rehydration-clear-guard.py`
- `rehydration-stop-linter.py`
- `write-barrier.py`
- `evidence-capture.py`
- `secret-redactor.py`
- `secrets-guard.py`
- `bash-safety-gate.py`
- `posttoolbatch-reconcile.py`
- `subagent-lifecycle.py`
- `task-lifecycle.py`

Into `~/.claude/agent-control`:

- regression probes
- compaction, recovery, and verification docs

Into `~/.claude/settings.json`:

- merged hook configuration for all Claude Code lifecycle events

Into `~/.claude/state`:

- install receipt (`compaction-hooks-installer.json`)

## Scope

Claude Concussion is for Claude Code only. Hook authority lives in:

- `~/.claude/settings.json`
- `~/.claude/hooks/*`
- `~/.claude/agent-control/*`

It does not touch Codex. Codex needs its own adapter.

## Package

```sh
python3 scripts/package.py
```

Outputs `dist/claude_concussion.zip`.

## Contributing

Human drive-bys and coding agent PRs are both welcome.

Whatever produces the PR: test it against a real compaction event. Run a session long enough to trigger compaction, let Claude resume, and confirm the barrier fires and clears. Unit tests don't cover real compaction behaviour.
