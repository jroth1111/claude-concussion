#!/usr/bin/env python3
"""Stop hook linter for post-compaction recovery packets.

When a rehydration flag is active, the next assistant turn must not finish with
a substantive answer that skipped the recovery packet. This hook inspects the
latest assistant text in the transcript and blocks/warns when the required
packet fields are absent.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

from rehydration_schema import (
    evidence_since,
    format_validation_errors,
    is_assistant_record,
    load_schema,
    nonzero_evidence_requires_classification,
    record_text,
    validate_packet_text,
)


def safe_session_id(session_id: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:160]


def flag_file_for(payload: dict) -> tuple[Path, str]:
    override = os.environ.get("CLAUDE_REHYDRATION_FLAG")
    if override:
        return Path(override), "override"
    state_dir = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))
    session_id = payload.get("session_id") or payload.get("sessionId") or os.environ.get("CLAUDE_SESSION_ID") or ""
    if isinstance(session_id, str) and session_id:
        return state_dir / "rehydration" / f"{safe_session_id(session_id)}.json", f"session:{session_id}"
    return state_dir / "rehydration-required", "legacy-global:no-session-id"


def find_transcript(payload: dict) -> Path | None:
    transcript = payload.get("transcript_path") or payload.get("transcriptPath") or ""
    if transcript and Path(transcript).is_file():
        return Path(transcript)
    session_id = payload.get("session_id") or payload.get("sessionId") or os.environ.get("CLAUDE_SESSION_ID") or ""
    if isinstance(session_id, str) and session_id:
        matches = glob.glob(str(Path.home() / ".claude" / "projects" / "**" / f"{session_id}.jsonl"), recursive=True)
        if matches:
            return Path(matches[0])
    return None


def latest_assistant_text(path: Path) -> str:
    latest = ""
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not is_assistant_record(obj):
                continue
            text = record_text(obj)
            if text.strip():
                latest = text
    return latest


def flag_data(flag_file: Path) -> dict:
    try:
        data = json.loads(flag_file.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def block(reason: str) -> None:
    print(json.dumps({
        "decision": "block",
        "reason": reason,
    }))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    flag_file, scope = flag_file_for(payload)
    if not flag_file.exists():
        return 0

    transcript = find_transcript(payload)
    if not transcript:
        block(
            f"Rehydration flag active for {scope}, but transcript was unavailable. "
            "Before ending the turn, produce the required recovery packet and clear "
            "with rehydration-clear.py."
        )
        return 0

    schema = load_schema()
    flag = flag_data(flag_file)
    session_id = payload.get("session_id") or payload.get("sessionId") or os.environ.get("CLAUDE_SESSION_ID") or ""
    armed_at = str(flag.get("armed_at") or flag.get("ts") or "")
    evidence_file = Path(os.environ.get("CLAUDE_EVIDENCE_FILE", str(Path.home() / ".claude" / "state" / "evidence-index.jsonl")))
    recent_evidence = evidence_since(evidence_file, session_id=session_id, since_ts=armed_at)
    required_ids = {
        str(row.get("evidence_id"))
        for row in recent_evidence
        if row.get("severity") in {"error", "mutation"} or row.get("tool") == "Bash"
    }

    latest = latest_assistant_text(transcript)
    result = validate_packet_text(latest, schema=schema, required_evidence_ids=required_ids)
    nonzero_error = nonzero_evidence_requires_classification(latest, recent_evidence)
    errors = format_validation_errors(result)
    if nonzero_error:
        errors.append(nonzero_error)
    if errors:
        block(
            f"Rehydration flag active for {scope}; assistant response does not satisfy "
            f"rehydration schema v{schema.get('schema_version', 1)} ({'; '.join(errors)}). "
            "Produce the packet before any substantive final answer, then clear with "
            "rehydration-clear.py."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
