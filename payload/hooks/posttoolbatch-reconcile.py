#!/usr/bin/env python3
"""PostToolBatch heartbeat for batch-level reconciliation."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


STATE_DIR = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))
HEARTBEAT_FILE = Path(os.environ.get("CLAUDE_HEARTBEAT_FILE", str(STATE_DIR / "heartbeat.jsonl")))
LEDGER = STATE_DIR / "posttoolbatch.jsonl"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {
        "ts": ts,
        "event": "PostToolBatch",
        "session_id": payload.get("session_id") or payload.get("sessionId") or "",
        "transcript_path": payload.get("transcript_path") or payload.get("transcriptPath") or "",
        "cwd": payload.get("cwd") or "",
    }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with HEARTBEAT_FILE.open("a", encoding="utf-8") as hb:
            hb.write(json.dumps(entry, sort_keys=True) + "\n")
        with LEDGER.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
