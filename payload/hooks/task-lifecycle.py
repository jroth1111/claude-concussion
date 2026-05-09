#!/usr/bin/env python3
"""TaskCreated/TaskCompleted guard for scoped task discipline."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


STATE_DIR = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))
LEDGER = STATE_DIR / "task-lifecycle.jsonl"


def load_payload() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for child in value.values():
            out.extend(strings(child))
        return out
    if isinstance(value, list):
        out = []
        for child in value:
            out.extend(strings(child))
        return out
    return []


def append(entry: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass


def block(reason: str) -> None:
    print(json.dumps({"continue": False, "stopReason": reason}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=("created", "completed"), required=True)
    args = parser.parse_args()
    payload = load_payload()
    text = " ".join(strings(payload))
    append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": f"Task{args.event.title()}",
        "session_id": payload.get("session_id") or payload.get("sessionId") or "",
        "text_excerpt": " ".join(text.split())[:500],
    })

    if args.event == "created":
        if len(text.strip()) < 30:
            block("Task creation needs concrete scope, acceptance/verification, and allowed work boundary.")
            return 0
        if not re.search(r"\b(scope|acceptance|verify|verification|evidence|files?|deliverable)\b", text, re.I):
            block("Task creation must include scope plus acceptance or verification evidence requirements.")
        return 0

    if args.event == "completed":
        # The event itself is the completion claim; do not require literal words
        # like "done" or "completed" in the payload before enforcing evidence.
        if not re.search(
            r"\b(test|tests|lint|build|evidence|blocked|descoped|proof|command|inspection|artifact|exit code|exit_code)\b",
            text,
            re.I,
        ):
            block("Task completion needs verification evidence or an explicit blocked/descoped status.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
