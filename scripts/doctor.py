#!/usr/bin/env python3
"""Doctor checks for the installed Claude compaction hook layer."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_HOOKS = {
    "PreToolUse": ["secrets-guard.py", "bash-safety-gate.py", "rehydration-clear-guard.py", "write-barrier.py"],
    "PostToolUse": ["evidence-capture.py"],
    "PostToolBatch": ["posttoolbatch-reconcile.py"],
    "PreCompact": ["state-snapshot.sh"],
    "PostCompact": ["postcompact-capture.py", "state-snapshot.sh"],
    "SessionStart": ["state-snapshot.sh"],
    "Stop": ["rehydration-stop-linter.py"],
    "SubagentStart": ["subagent-lifecycle.py"],
    "SubagentStop": ["subagent-lifecycle.py"],
    "TaskCreated": ["task-lifecycle.py"],
    "TaskCompleted": ["task-lifecycle.py"],
    "SessionEnd": ["state-snapshot.sh"],
}

FORBIDDEN_AUTHORITY = re.compile(r"\.codex|Codex|codex")


def commands_for(settings: dict, event: str) -> list[str]:
    out: list[str] = []
    for entry in settings.get("hooks", {}).get(event, []) or []:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook, dict):
                out.append(str(hook.get("command") or ""))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-home", default="~/.claude")
    args = parser.parse_args()
    home = Path(args.claude_home).expanduser().resolve()
    settings_path = home / "settings.json"
    errors: list[str] = []
    warnings: list[str] = []

    if not settings_path.exists():
        errors.append(f"missing settings.json: {settings_path}")
        settings = {}
    else:
        settings = json.loads(settings_path.read_text())

    for event, names in REQUIRED_HOOKS.items():
        commands = commands_for(settings, event)
        for name in names:
            if not any(name in command for command in commands):
                errors.append(f"settings missing {event} command containing {name}")

    for name in sorted({name for names in REQUIRED_HOOKS.values() for name in names} | {"rehydration_schema.py", "rehydration-schema.json", "rehydration-clear.py", "secret-redactor.py"}):
        path = home / "hooks" / name
        if not path.exists():
            errors.append(f"missing hook file: {path}")

    for path in list((home / "hooks").glob("*")) + [settings_path]:
        if path.is_file():
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if FORBIDDEN_AUTHORITY.search(text):
                errors.append(f"Codex authority reference found in Claude installer surface: {path}")

    if not (home / "state").exists():
        warnings.append(f"state directory has not been created yet: {home / 'state'}")

    for warning in warnings:
        print(f"warn: {warning}")
    if errors:
        print("status: error")
        for error in errors:
            print(f"- {error}")
        return 2
    print("status: ok")
    print(f"claude_home: {home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
