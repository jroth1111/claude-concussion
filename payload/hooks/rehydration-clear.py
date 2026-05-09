#!/usr/bin/env python3
"""Audited helper for clearing one post-compaction rehydration flag.

Uses session-scoped flags when a session id is provided. Refuses ambiguous
global clears when multiple session flags are pending.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from rehydration_schema import (
    evidence_since,
    format_validation_errors,
    latest_assistant_packet,
    latest_compaction_line,
    load_schema,
    nonzero_evidence_requires_classification,
    read_jsonl,
)


def safe_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:160]


def state_dir() -> Path:
    return Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))


def resolve_flag(session_id: str) -> tuple[Path, str, str]:
    override = os.environ.get("CLAUDE_REHYDRATION_FLAG")
    if override:
        return Path(override), "override", session_id

    sid = session_id or os.environ.get("CLAUDE_SESSION_ID", "")
    if sid:
        return state_dir() / "rehydration" / f"{safe_session_id(sid)}.json", f"session:{sid}", sid

    pending = sorted((state_dir() / "rehydration").glob("*.json"))
    if len(pending) == 1:
        try:
            data = json.loads(pending[0].read_text(encoding="utf-8"))
            sid = data.get("session_id", "") if isinstance(data, dict) else ""
        except Exception:
            sid = ""
        return pending[0], f"single-pending-session:{sid or pending[0].stem}", sid
    if len(pending) > 1:
        raise SystemExit(
            "refusing to clear: multiple session-scoped rehydration flags exist; "
            "rerun with --session-id for the current Claude session"
        )

    return state_dir() / "rehydration-required", "legacy-global:no-session-id", ""


def find_transcript(session_id: str, explicit: str = "") -> Path | None:
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    if not session_id:
        return None
    matches = list((Path.home() / ".claude" / "projects").glob(f"**/{session_id}.jsonl"))
    if matches:
        return max(matches, key=lambda p: p.stat().st_mtime)
    return None


def read_flag(flag_file: Path) -> dict:
    try:
        data = json.loads(flag_file.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def required_evidence_ids(session_id: str, armed_at: str) -> tuple[list[dict], set[str]]:
    evidence_file = Path(
        os.environ.get(
            "CLAUDE_EVIDENCE_FILE",
            str(state_dir() / "evidence-index.jsonl"),
        )
    )
    rows = evidence_since(evidence_file, session_id=session_id, since_ts=armed_at)
    ids = {
        str(row.get("evidence_id"))
        for row in rows
        if row.get("severity") in {"error", "mutation"} or row.get("tool") == "Bash"
    }
    return rows, ids


CLEARANCE_LOG = Path(
    os.environ.get(
        "CLAUDE_REHYDRATION_CLEAR_LOG",
        str(state_dir() / "rehydration-clearance.jsonl"),
    )
)


def packet_validation_errors(packet, recent_evidence: list[dict]) -> list[str]:
    errors = format_validation_errors(packet.result)
    nonzero_error = nonzero_evidence_requires_classification(packet.text, recent_evidence)
    if nonzero_error:
        errors.append(nonzero_error)
    return errors


def latest_valid_packet_with_wait(
    transcript: Path,
    *,
    boundary_line: int,
    schema: dict,
    evidence_ids: set[str],
    recent_evidence: list[dict],
    wait_seconds: float,
):
    deadline = time.monotonic() + max(0.0, wait_seconds)
    last_packet = None
    last_errors: list[str] = []
    while True:
        packet = latest_assistant_packet(
            transcript,
            after_line=boundary_line,
            schema=schema,
            required_evidence_ids=evidence_ids,
        )
        if packet:
            errors = packet_validation_errors(packet, recent_evidence)
            if not errors:
                return packet, []
            last_packet = packet
            last_errors = errors
        if time.monotonic() >= deadline:
            return last_packet, last_errors
        time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear ~/.claude/state/rehydration-required after explicit reconstruction."
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="What was reconstructed before clearing the barrier.",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="Claude session id whose rehydration flag should be cleared.",
    )
    parser.add_argument(
        "--transcript-path",
        default="",
        help="Explicit transcript JSONL path. Defaults to ~/.claude/projects/**/<session-id>.jsonl.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=float(os.environ.get("CLAUDE_REHYDRATION_CLEAR_WAIT", "2.0")),
        help="Seconds to wait for the just-emitted packet to appear in transcript JSONL.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate the latest post-compaction packet without clearing the flag.",
    )
    args = parser.parse_args()

    reason = " ".join(args.reason.split())
    if len(reason) < 20:
        print("refusing to clear: --reason must describe what was reconstructed", flush=True)
        return 2

    flag_file, scope, session_id = resolve_flag(args.session_id)

    flag_reason = ""
    if flag_file.exists():
        try:
            flag_reason = flag_file.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            flag_reason = ""
    else:
        flag_reason = ""

    if not flag_file.exists():
        CLEARANCE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": "rehydration_clear",
            "cleared": False,
            "flag": str(flag_file),
            "scope": scope,
            "reason": reason,
            "flag_reason": "",
            "cwd": os.getcwd(),
            "launcher": os.environ.get("CLAUDE_LAUNCHER", ""),
            "session_id": session_id or os.environ.get("CLAUDE_SESSION_ID", ""),
            "validation": "skipped_already_absent",
        }
        with CLEARANCE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"rehydration flag was already absent: {flag_file}")
        print(f"scope: {scope}")
        print(f"clearance logged: {CLEARANCE_LOG}")
        return 0

    flag = read_flag(flag_file)
    transcript = find_transcript(session_id, args.transcript_path or str(flag.get("transcript_path") or ""))
    if not transcript:
        print(
            "refusing to clear: transcript unavailable; pass --transcript-path or ensure session_id maps to ~/.claude/projects",
            flush=True,
        )
        return 2

    schema = load_schema()
    records = read_jsonl(transcript)
    boundary_line = latest_compaction_line(records)
    trigger = str(flag.get("armed_by") or flag.get("trigger") or "").lower()
    if boundary_line == 0 and trigger in {"precompact", "postcompact", "sessionstart_compact", "compact"}:
        print(
            f"refusing to clear: compaction-triggered flag has no compact boundary in {transcript}",
            flush=True,
        )
        return 2
    armed_at = str(flag.get("armed_at") or flag.get("ts") or "")
    recent_evidence, evidence_ids = required_evidence_ids(session_id, armed_at)
    packet, errors = latest_valid_packet_with_wait(
        transcript,
        boundary_line=boundary_line,
        schema=schema,
        evidence_ids=evidence_ids,
        recent_evidence=recent_evidence,
        wait_seconds=args.wait_seconds,
    )
    if not packet:
        print(
            f"refusing to clear: no assistant rehydration packet found after compact boundary line {boundary_line} in {transcript}",
            flush=True,
        )
        return 2
    if errors:
        print(
            f"refusing to clear: packet at {transcript}:{packet.line} fails schema v{schema.get('schema_version', 1)}: "
            + "; ".join(errors),
            flush=True,
        )
        print("packet template: python3 __CLAUDE_HOME__/hooks/rehydration_schema.py --template", flush=True)
        print(
            "next allowed command: python3 __CLAUDE_HOME__/hooks/rehydration-clear.py "
            "--check-only --session-id <session_id> --reason \"<what was reconstructed>\"",
            flush=True,
        )
        return 2

    if args.check_only:
        print(f"rehydration packet valid: {transcript}:{packet.line}")
        print(f"scope: {scope}")
        print("flag not cleared: --check-only")
        return 0

    if flag_file.exists():
        flag_file.unlink()
        cleared = True
    else:
        cleared = False

    CLEARANCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": "rehydration_clear",
        "cleared": cleared,
        "flag": str(flag_file),
        "scope": scope,
        "reason": reason,
        "flag_reason": flag_reason,
        "cwd": os.getcwd(),
        "launcher": os.environ.get("CLAUDE_LAUNCHER", ""),
        "session_id": session_id or os.environ.get("CLAUDE_SESSION_ID", ""),
        "transcript_path": str(transcript),
        "compact_boundary_line": boundary_line,
        "packet_line": packet.line,
        "schema_version": schema.get("schema_version", 1),
        "evidence_ids_required": sorted(evidence_ids),
        "evidence_ids_cited": packet.result.get("cited_evidence_ids", []),
        "validation": "passed",
        "selected_directive": packet.result.get("fields", {}).get("SELECTED_DIRECTIVE", ""),
        "current_frontier": packet.result.get("fields", {}).get("CURRENT_FRONTIER", ""),
        "stop_condition": packet.result.get("fields", {}).get("STOP_CONDITION", ""),
    }
    with CLEARANCE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if cleared:
        print(f"rehydration flag cleared: {flag_file}")
    else:
        print(f"rehydration flag was already absent: {flag_file}")
    print(f"scope: {scope}")
    print(f"clearance logged: {CLEARANCE_LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
