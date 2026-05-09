#!/usr/bin/env python3
"""Persist PostCompact compact_summary as auditable projection evidence."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


STATE_DIR = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))
CAPTURE_FILE = Path(os.environ.get("CLAUDE_POSTCOMPACT_FILE", str(STATE_DIR / "postcompact-captures.jsonl")))
MAX_EXCERPT = 1200


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "tool_result":
                    continue
                if item.get("type") in {"text", "input_text", "output_text"} or "text" in item:
                    parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return ""


def is_compaction_boundary(obj: dict[str, Any]) -> bool:
    if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
        return True
    if obj.get("type") == "compacted":
        return True
    payload = obj.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "compacted"


def latest_user(path: Path) -> tuple[str, str]:
    latest = ""
    latest_source = "unknown"
    try:
        records: list[tuple[int, dict[str, Any]]] = []
        boundary = 0
        for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, dict):
                records.append((lineno, obj))
                if is_compaction_boundary(obj):
                    boundary = lineno
        for lineno, obj in records:
            if boundary and lineno >= boundary:
                continue
            if obj.get("type") != "user":
                continue
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            text = text_from_content(message.get("content"))
            if text.strip():
                latest = " ".join(text.split())[:MAX_EXCERPT]
                latest_source = "human"
    except Exception:
        return "", "unknown"
    return latest, latest_source


def git_status(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except Exception:
        return ""
    return proc.stdout[:MAX_EXCERPT]


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    if str(payload.get("hook_event_name") or payload.get("hookEventName") or "") != "PostCompact":
        return 0

    summary = str(payload.get("compact_summary") or payload.get("compactSummary") or payload.get("summary") or "")
    transcript = str(payload.get("transcript_path") or payload.get("transcriptPath") or "")
    cwd = str(payload.get("cwd") or "")
    latest_human, latest_source = latest_user(Path(transcript)) if transcript else ("", "unknown")
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": payload.get("session_id") or payload.get("sessionId") or "",
        "event": "PostCompact",
        "trigger": payload.get("trigger") or payload.get("source") or "",
        "transcript_path": transcript,
        "cwd": cwd,
        "compact_summary_hash": hashlib.sha256(summary.encode("utf-8", errors="replace")).hexdigest() if summary else "",
        "compact_summary_excerpt": summary[:MAX_EXCERPT],
        "summary_status": "projection_unverified" if summary else "missing",
        "latest_user_before_compact": latest_human,
        "latest_human_prompt": latest_human,
        "latest_user_source": latest_source,
        "git_status_short": git_status(cwd),
    }
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with CAPTURE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
