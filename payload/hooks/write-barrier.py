#!/usr/bin/env python3
"""PreToolUse write barrier — session-scoped, flag-gated, permissionMode-agnostic.

Engages ONLY when the current session's rehydration flag exists. With a
session_id, that flag is ~/.claude/state/rehydration/<session_id>.json. Without
a session_id, the hook falls back to the legacy global flag
~/.claude/state/rehydration-required. When the scoped flag is absent the hook
exits silently regardless of settings (including bypassPermissions).

Gates the full edit-class tool set so adding a new edit tool to the matcher
without updating GATED_TOOLS would silently bypass the barrier — keep the two
in sync. MultiEdit is included; binary inspection confirms it ships alongside
Write/Edit/MultiEdit/NotebookEdit.

Bash deletion of rehydration flags is handled by rehydration-clear-guard.py.

Hook contract: stdin = JSON {tool_name, tool_input, ...}
              stdout = JSON {decision: "block", reason: "..."} when blocking
              exits 0 always
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

GATED_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def safe_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:160]


def flag_file_for(payload: dict) -> tuple[Path, str, str]:
    override = os.environ.get("CLAUDE_REHYDRATION_FLAG")
    if override:
        return Path(override), "override", ""

    state_dir = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))
    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or os.environ.get("CLAUDE_SESSION_ID")
        or ""
    )
    if isinstance(session_id, str) and session_id:
        return state_dir / "rehydration" / f"{safe_session_id(session_id)}.json", f"session:{session_id}", session_id
    return state_dir / "rehydration-required", "legacy-global:no-session-id", ""


def block(reason: str):
    out = {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    print(json.dumps(out))
    sys.exit(0)


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)

    flag_file, scope, session_id = flag_file_for(payload)
    if not flag_file.exists():
        sys.exit(0)

    tool = payload.get("tool_name", "")
    if tool not in GATED_TOOLS:
        sys.exit(0)

    reason_detail = ""
    try:
        reason_detail = flag_file.read_text(encoding="utf-8").strip()[:500]
    except Exception:
        pass

    clear_cmd = "python3 __CLAUDE_HOME__/hooks/rehydration-clear.py"
    if session_id:
        clear_cmd += f" --session-id {session_id}"
    clear_cmd += ' --reason "<what was reconstructed>"'

    msg = (
        f"Write barrier active for {scope}: {flag_file} exists. "
        f"{tool} blocked. Inspect git status/diff and reopen changed files first, "
        f"then clear with `{clear_cmd}`."
    )
    if reason_detail:
        msg += f"\nFlag reason: {reason_detail}"
    block(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
