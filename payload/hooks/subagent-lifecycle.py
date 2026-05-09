#!/usr/bin/env python3
"""Subagent lifecycle guard and handoff recorder."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path


STATE_DIR = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))
LEDGER = STATE_DIR / "subagent-lifecycle.jsonl"
REQUIRED_HANDOFF = ("FILES_READ", "CLAIMS", "EVIDENCE", "UNCERTAINTY")


def load_payload() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def append(entry: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass


def context() -> None:
    text = (
        "Subagent handoff requirement: before stopping, include SUBAGENT_HANDOFF with "
        "FILES_READ, CLAIMS, EVIDENCE, and UNCERTAINTY. Cite files/commands actually "
        "inspected and state residual gaps instead of returning generic confidence."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": text,
        }
    }))


def block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=("start", "stop"), required=True)
    args = parser.parse_args()
    payload = load_payload()
    append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": f"Subagent{args.event.title()}",
        "session_id": payload.get("session_id") or payload.get("sessionId") or "",
        "agent_id": payload.get("agent_id") or "",
        "agent_type": payload.get("agent_type") or "",
        "transcript_path": payload.get("transcript_path") or "",
        "agent_transcript_path": payload.get("agent_transcript_path") or "",
    })
    if args.event == "start":
        context()
        return 0

    if payload.get("stop_hook_active"):
        return 0
    message = str(payload.get("last_assistant_message") or "")
    if "SUBAGENT_HANDOFF" not in message:
        block("Subagent must finish with SUBAGENT_HANDOFF containing FILES_READ, CLAIMS, EVIDENCE, and UNCERTAINTY.")
        return 0
    missing = [field for field in REQUIRED_HANDOFF if not re.search(rf"\b{field}\b\s*:", message)]
    if missing:
        block("Subagent handoff missing required fields: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
