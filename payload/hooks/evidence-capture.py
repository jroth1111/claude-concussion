#!/usr/bin/env python3
"""PostToolUse evidence capture.

Extracts high-signal facts from tool outputs into ~/.claude/state/evidence-index.jsonl.
Designed to survive compaction — entries are structured, not raw dumps.

Captures:
  - Bash: exit code, command, error patterns (FAIL/Error/Traceback/panic), test outcomes
  - Read: file path (not content)
  - Write/Edit/MultiEdit/NotebookEdit: file path + change indicator
  - Grep/Glob: pattern + match count

Skips noise: successful no-output commands, trivial reads under threshold, etc.
Caps file at 5MB; rotates to .1 backup.

Hook contract: stdin = JSON {tool_name, tool_input, tool_response, session_id, cwd}
              stdout = empty (this hook is observation-only)
              never blocks, always exits 0
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import sys
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))
EVIDENCE_FILE = Path(os.environ.get("CLAUDE_EVIDENCE_FILE", str(STATE_DIR / "evidence-index.jsonl")))
HEARTBEAT_FILE = Path(os.environ.get("CLAUDE_HEARTBEAT_FILE", str(STATE_DIR / "heartbeat.jsonl")))
MAX_BYTES = 5 * 1024 * 1024  # 5MB
MAX_SUMMARY_LEN = 400
MAX_ERROR_LINES = 8

# Patterns that signal high-value content worth preserving
ERROR_PATTERNS = re.compile(
    r"(?im)^.*\b("
    r"error|fail(?:ed|ure)?|exception|traceback|panic"
    r"|assertion(?:error)?|fatal|abort"
    r"|undefined reference|cannot find|not found"
    r"|segfault|killed"
    r")\b.*$"
)
TEST_OUTCOME_PATTERNS = re.compile(
    r"(?i)(\d+\s+passed|\d+\s+failed|\d+\s+error|\d+\s+test[s]?\s+(?:passed|failed|run))"
)
PROCESS_SESSION_RE = re.compile(r"Process running with session ID\s+([0-9]+)")
BASH_MUTATION_RE = re.compile(
    r"\b(?:rm|mv|cp|mkdir|rmdir|touch|truncate|tee|dd|chmod|chown|ln)\b"
    r"|\b(?:git\s+(?:add|apply|am|checkout|clean|commit|merge|mv|pull|push|rebase|reset|restore|stash))\b"
    r"|\b(?:sed\s+-i|perl\s+-pi)\b"
    r"|(?:^|[^\w])>{1,2}\s*"
    r"|\b(?:python3?|node|ruby|perl)\b[^\n;|&]*(?:write_text|write_bytes|open\s*\([^)]*,\s*['\"](?:w|a|x|\+)|writeFile(?:Sync)?|appendFile(?:Sync)?|rmSync|unlinkSync)",
    re.IGNORECASE,
)


def safe_load_stdin() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def truncate(s: str, n: int = MAX_SUMMARY_LEN) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def extract_errors(text: str, limit: int = MAX_ERROR_LINES) -> list[str]:
    if not text:
        return []
    matches = []
    for line in text.splitlines():
        if ERROR_PATTERNS.search(line):
            matches.append(truncate(line, 200))
            if len(matches) >= limit:
                break
    return matches


def shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except Exception:
        return []


def path_candidates(command: str, limit: int = 8) -> list[str]:
    tokens = shell_tokens(command)
    candidates: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in {"-m", "-C", "--message"}:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        if "/" in token or token.startswith((".", "~")):
            if token not in candidates:
                candidates.append(token)
        if len(candidates) >= limit:
            break
    return candidates


def is_zero_exit(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, int):
        return value == 0
    text = str(value).strip().lower()
    if text in {"", "0", "ok", "success", "succeeded"}:
        return True
    try:
        return int(text) == 0
    except Exception:
        return False


def is_interrupted(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "interrupted", "interrupt", "cancelled", "canceled"}


def rotate_if_needed():
    try:
        if EVIDENCE_FILE.exists() and EVIDENCE_FILE.stat().st_size > MAX_BYTES:
            backup = EVIDENCE_FILE.with_suffix(".jsonl.1")
            if backup.exists():
                backup.unlink()
            EVIDENCE_FILE.rename(backup)
    except Exception:
        pass


def rotate_heartbeat_if_needed():
    try:
        if HEARTBEAT_FILE.exists() and HEARTBEAT_FILE.stat().st_size > MAX_BYTES:
            backup = HEARTBEAT_FILE.with_suffix(".jsonl.1")
            if backup.exists():
                backup.unlink()
            HEARTBEAT_FILE.rename(backup)
    except Exception:
        pass


def write_entry(entry: dict):
    rotate_if_needed()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with EVIDENCE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def evidence_id(entry: dict) -> str:
    basis = json.dumps(
        {
            "ts": entry.get("ts"),
            "session": entry.get("session"),
            "tool": entry.get("tool"),
            "severity": entry.get("severity"),
            "summary": entry.get("summary"),
            "command": entry.get("command"),
            "file_path": entry.get("file_path"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "ev_" + hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()[:12]


def handle_bash(tool_input: dict, tool_response: dict) -> dict | None:
    command = str(tool_input.get("command", ""))
    if not command:
        return None

    # tool_response shape varies — try common fields
    stdout = str(tool_response.get("stdout", "") or tool_response.get("output", ""))
    stderr = str(tool_response.get("stderr", ""))
    exit_code = tool_response.get("exit_code", tool_response.get("exitCode", 0))
    exit_failed = not is_zero_exit(exit_code)
    interrupted = is_interrupted(tool_response.get("interrupted", False))

    is_mutating = bool(BASH_MUTATION_RE.search(command))

    # Severity is authoritative — process state only. Pattern matches in
    # output stay as retrieval hints in `errors`/`test_outcome` but never
    # elevate severity, otherwise capturing prior captures loops the count up.
    combined = f"{stdout}\n{stderr}".strip()
    errors = extract_errors(combined) if exit_failed or interrupted else []
    test_match = TEST_OUTCOME_PATTERNS.search(combined)
    background_sessions = PROCESS_SESSION_RE.findall(combined)
    if not exit_failed and not interrupted and not is_mutating and not test_match and not background_sessions:
        return None

    severity = "error" if (exit_failed or interrupted) else ("mutation" if is_mutating else "info")
    summary_parts = [f"exit={exit_code}"]
    if interrupted:
        summary_parts.append("INTERRUPTED")
    if test_match:
        summary_parts.append(test_match.group(1))
    if background_sessions:
        summary_parts.append("background_sessions=" + ",".join(background_sessions[:8]))
    summary_parts.append(f"cmd={truncate(command, 150)!r}")

    return {
        "tool": "Bash",
        "severity": severity,
        "summary": " ".join(summary_parts),
        "command": truncate(command, 300),
        "exit_code": exit_code,
        "errors": errors,
        "test_outcome": test_match.group(1) if test_match else None,
        "changed_path_candidates": path_candidates(command) if is_mutating else [],
        "background_tasks": [
            {"session_id": sid, "state": "running", "source": "Bash output"}
            for sid in background_sessions[:8]
        ],
    }


def handle_read(tool_input: dict) -> dict | None:
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None
    return {
        "tool": "Read",
        "severity": "info",
        "summary": f"read {file_path}",
        "file_path": file_path,
    }


def handle_write(tool_input: dict, tool_response: dict, tool_name: str) -> dict | None:
    _ = tool_response  # reserved for future use (e.g., diff stats)
    # NotebookEdit uses notebook_path; falling back keeps it from dropping.
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        return None
    return {
        "tool": tool_name,
        "severity": "mutation",
        "summary": f"{tool_name.lower()} {file_path}",
        "file_path": file_path,
    }


def handle_search(tool_input: dict, tool_response: dict, tool_name: str) -> dict | None:
    pattern = tool_input.get("pattern", "")
    if not pattern:
        return None
    output = str(tool_response.get("output", "") or tool_response)
    match_count = output.count("\n") if output else 0
    return {
        "tool": tool_name,
        "severity": "info",
        "summary": f"{tool_name.lower()} {truncate(pattern, 80)!r} -> ~{match_count} matches",
        "pattern": truncate(pattern, 200),
    }


def main():
    payload = safe_load_stdin()
    if not payload:
        sys.exit(0)

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    if isinstance(tool_response, str):
        tool_response = {"output": tool_response}

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cwd = payload.get("cwd", "")
    session_id = payload.get("session_id") or payload.get("sessionId") or os.environ.get("CLAUDE_SESSION_ID", "")
    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath", "")
    launcher = os.environ.get("CLAUDE_LAUNCHER", "")

    # Heartbeat first — Layer 1 must fire on every matched tool call
    # regardless of whether an evidence entry is produced. Silent sessions
    # (successful no-output Bash, tool names not in the if/elif chain) would
    # otherwise leave Layer 1 blind even though hooks are firing. Rotation
    # mirrors evidence-index 5MB cap.
    rotate_heartbeat_if_needed()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with HEARTBEAT_FILE.open("a", encoding="utf-8") as hb:
            hb.write(json.dumps({
                "ts": ts,
                "event": "PostToolUse",
                "tool": tool,
                "cwd": cwd,
                "session_id": session_id,
                "transcript_path": transcript_path,
                "launcher": launcher,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    entry_data = None
    if tool == "Bash":
        entry_data = handle_bash(tool_input, tool_response)
    elif tool == "Read":
        entry_data = handle_read(tool_input)
    elif tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        entry_data = handle_write(tool_input, tool_response, tool)
    elif tool in ("Grep", "Glob"):
        entry_data = handle_search(tool_input, tool_response, tool)

    if entry_data is None:
        sys.exit(0)

    entry = {
        "ts": ts,
        "session": session_id,
        "cwd": cwd,
        **entry_data,
    }
    entry["evidence_id"] = evidence_id(entry)
    write_entry(entry)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never break tool flow because of capture errors
        sys.exit(0)
