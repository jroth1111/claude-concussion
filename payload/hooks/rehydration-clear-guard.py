#!/usr/bin/env python3
"""PreToolUse Bash guard for the rehydration flag.

The edit-class write barrier is useful only if the flag cannot be silently
removed by a raw shell command. This hook blocks direct shell deletion/truncation
of the flag and requires the audited helper instead.

When the current session's rehydration flag is active, Bash is temporarily
read-only allowlisted. That closes the gap where a post-compaction assistant can
mutate the workspace through shell before reconstructing directive authority.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import re


TARGET_PREFIX = r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s'\";|&`$()<>]+)?/?"
TARGET_BOUNDARY = r"(?=$|[\s'\";|&`$()<>]|\*)"
REHYDRATION_TARGETS = (
    re.compile(
        rf"(?:^|[\s'\"=]){TARGET_PREFIX}\.claude/state/rehydration-required{TARGET_BOUNDARY}"
        r"|(?:^|[\s'\"=])rehydration-required(?=$|[\s'\";|&`$()<>])",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:^|[\s'\"=]){TARGET_PREFIX}\.claude/state/rehydration(?:/[^;\n|&`$()<>]*)?{TARGET_BOUNDARY}"
        r"|(?:^|[\s'\"=])state/rehydration(?:/[^;\n|&`$()<>]*)?(?=$|[\s'\";|&`$()<>]|\*)",
        re.IGNORECASE,
    ),
)
MUTATION_RE = re.compile(
    r"\b(?:rm|unlink|mv|truncate|tee|dd|chmod|chown)\b"
    r"|(?:^|[^\w])>{1,2}\s*"
    r"|\bfind\b[^\n;]*(?:-delete|-exec\s+(?:rm|unlink|mv|sh|bash)\b)"
    r"|\b(?:os\.remove|os\.unlink|pathlib|Path\b|unlink\s*\(|\.unlink\s*\(|removeSync|rmSync|rmdirSync|"
    r"shutil\.rmtree|writeFile(?:Sync)?|write_text|write_bytes|open\s*\()",
    re.IGNORECASE,
)
READ_ONLY_SIMPLE_COMMANDS = {"rg", "grep", "sed", "head", "tail", "cat", "jq", "wc", "find", "ls", "pwd", "date", "shasum", "echo", "printf"}
READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "rev-parse"}
READ_ONLY_BD_SUBCOMMANDS = {"prime", "ready", "show", "list", "memories", "search", "stats"}
SHELL_SEPARATORS = {";", "&&", "||", "|", "<", ">", ">>", "`"}
READ_ONLY_SEPARATORS = {";", "&&", "|"}
REHYDRATION_CLEAR_HELPER = "__CLAUDE_HOME__/hooks/rehydration-clear.py"
REHYDRATION_SCHEMA_HELPER = "__CLAUDE_HOME__/hooks/rehydration_schema.py"
SECRET_REDACTOR_HELPER = "__CLAUDE_HOME__/hooks/secret-redactor.py"
SHELL_MUTATION_RE = re.compile(
    r"\b(?:rm|mv|cp|mkdir|rmdir|touch|truncate|tee|dd|chmod|chown|"
    r"git\s+(?:checkout|reset|commit|push|pull|merge|rebase|clean|stash|apply|am)|"
    r"python3?|node|ruby|perl|osascript|sqlite3)\b"
    r"|(?:^|[^\w])>{1,2}\s*"
    r"|\bfind\b[^\n;]*(?:-delete|-exec\s+(?:rm|unlink|mv|cp|sh|bash)\b)"
    r"|\b(?:writeFile(?:Sync)?|appendFile(?:Sync)?|rmSync|rmdirSync|unlink\s*\(|"
    r"\.unlink\s*\(|shutil\.rmtree|write_text|write_bytes|open\s*\([^)]*,\s*['\"](?:w|a|x|\+))",
    re.IGNORECASE,
)


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }
        )
    )
    raise SystemExit(0)


def mentions_rehydration_target(cmd: str) -> bool:
    return any(pattern.search(cmd) for pattern in REHYDRATION_TARGETS)


def mutates_rehydration_target(cmd: str) -> bool:
    if not mentions_rehydration_target(cmd):
        return False
    return bool(MUTATION_RE.search(cmd))


def safe_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:160]


def active_rehydration_flag(payload: dict) -> tuple[bool, str]:
    override = os.environ.get("CLAUDE_REHYDRATION_FLAG")
    if override:
        return Path(override).exists(), "override"
    state_dir = Path(os.environ.get("CLAUDE_STATE_DIR", str(Path.home() / ".claude" / "state")))
    session_id = payload.get("session_id") or payload.get("sessionId") or os.environ.get("CLAUDE_SESSION_ID") or ""
    if isinstance(session_id, str) and session_id:
        flag = state_dir / "rehydration" / f"{safe_session_id(session_id)}.json"
        return flag.exists(), f"session:{session_id}"
    flag = state_dir / "rehydration-required"
    return flag.exists(), "legacy-global:no-session-id"


def shell_tokens(cmd: str) -> list[str] | None:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";&|<>`")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def separator_indexes(tokens: list[str]) -> list[int]:
    return [index for index, token in enumerate(tokens) if token in SHELL_SEPARATORS]


def git_branch_read_only(args: list[str]) -> bool:
    if not args:
        return True
    allowed_flags = {
        "-a",
        "-r",
        "-v",
        "-vv",
        "--all",
        "--remotes",
        "--list",
        "--show-current",
        "--color",
        "--no-color",
    }
    value_flags = {
        "--sort",
        "--format",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
    }
    mutating_flags = {
        "-d",
        "-D",
        "-m",
        "-M",
        "-c",
        "-C",
        "--delete",
        "--move",
        "--copy",
        "--edit-description",
        "--set-upstream-to",
        "--unset-upstream",
        "--track",
        "--no-track",
    }
    saw_list_mode = any(arg == "--list" for arg in args)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in mutating_flags or any(arg.startswith(f"{flag}=") for flag in mutating_flags):
            return False
        if arg in allowed_flags:
            index += 1
            continue
        if arg in value_flags:
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in value_flags):
            index += 1
            continue
        if arg.startswith("-") and not arg.startswith("--") and set(arg[1:]) <= {"a", "r", "v"}:
            index += 1
            continue
        if saw_list_mode and not arg.startswith("-"):
            index += 1
            continue
        return False
    return True


def git_remote_read_only(args: list[str]) -> bool:
    if not args:
        return True
    if args in (["-v"], ["--verbose"]):
        return True
    if args[0] == "show":
        return True
    if args[0] == "get-url":
        return True
    return False


def simple_command_read_only(tokens: list[str]) -> bool:
    command = tokens[0]
    if command == "find" and ("-exec" in tokens or "-delete" in tokens):
        return False
    if command == "sed" and any(
        arg == "-i" or arg.startswith("-i") or arg == "--in-place" or arg.startswith("--in-place=")
        for arg in tokens[1:]
    ):
        return False
    return command in READ_ONLY_SIMPLE_COMMANDS


def read_only_tokens(tokens: list[str]) -> bool:
    if not tokens:
        return False
    command = tokens[0]
    if command == "cd":
        return len(tokens) == 2
    if command == "git":
        if len(tokens) < 2:
            return False
        subcommand = tokens[1]
        if subcommand in READ_ONLY_GIT_SUBCOMMANDS:
            return True
        if subcommand == "branch":
            return git_branch_read_only(tokens[2:])
        if subcommand == "remote":
            return git_remote_read_only(tokens[2:])
        return False
    if command == "bd":
        return len(tokens) >= 2 and tokens[1] in READ_ONLY_BD_SUBCOMMANDS
    if command in READ_ONLY_SIMPLE_COMMANDS:
        return simple_command_read_only(tokens)
    return False


def normalize_read_only_tokens(tokens: list[str]) -> list[str] | None:
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "2" and index + 2 < len(tokens) and tokens[index + 1] in {">", ">>"} and tokens[index + 2] == "&1":
            # stderr-to-stdout is still read-only and commonly appears before a
            # pipe to head/tail in recovery probes.
            index += 3
            continue
        if token == "2" and index + 2 < len(tokens) and tokens[index + 1] == ">&" and tokens[index + 2] == "1":
            index += 3
            continue
        if token == "2" and index + 2 < len(tokens) and tokens[index + 1] in {">", ">>"} and tokens[index + 2] in {"/dev/null", "dev/null"}:
            index += 3
            continue
        if token == "<" and index + 1 < len(tokens) and tokens[index + 1] in {"/dev/null", "dev/null"}:
            index += 2
            continue
        if token in {"&1", "1"}:
            index += 1
            continue
        normalized.append(token)
        index += 1
    return normalized


def split_pipeline_segments(tokens: list[str]) -> list[list[str]] | None:
    segments: list[list[str]] = []
    current: list[str] = []
    previous_separator = ""
    for token in tokens:
        if token in SHELL_SEPARATORS:
            if token not in READ_ONLY_SEPARATORS:
                return None
            if token == "&&" and previous_separator == "|":
                return None
            if not current:
                return None
            segments.append(current)
            current = []
            previous_separator = token
            continue
        current.append(token)
    if not current:
        return None
    segments.append(current)
    return segments


def is_read_only_shell_command(cmd: str) -> bool:
    tokens = shell_tokens(cmd.strip())
    if not tokens:
        return False
    tokens = normalize_read_only_tokens(tokens)
    if not tokens:
        return False
    segments = split_pipeline_segments(tokens)
    if not segments:
        return False
    return all(read_only_tokens(segment) for segment in segments)


def clear_helper_args_allowed(args: list[str]) -> bool:
    if not args:
        return False
    seen_reason = False
    seen_session = False
    index = 0
    allowed_value_args = {"--reason", "--session-id", "--transcript-path", "--wait-seconds"}
    allowed_bool_args = {"--check-only"}
    while index < len(args):
        arg = args[index]
        if arg in allowed_bool_args:
            index += 1
            continue
        if arg in allowed_value_args:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                return False
            seen_reason = seen_reason or arg == "--reason"
            seen_session = seen_session or arg == "--session-id"
            index += 2
            continue
        if arg.startswith("--reason="):
            seen_reason = bool(arg[len("--reason="):])
            index += 1
            continue
        if arg.startswith("--session-id="):
            seen_session = bool(arg[len("--session-id="):])
            index += 1
            continue
        if arg.startswith("--transcript-path="):
            if not arg[len("--transcript-path="):]:
                return False
            index += 1
            continue
        if arg.startswith("--wait-seconds="):
            if not arg[len("--wait-seconds="):]:
                return False
            index += 1
            continue
        return False
    return seen_reason and seen_session


def schema_helper_args_allowed(args: list[str]) -> bool:
    return args == ["--template"]


def redactor_helper_args_allowed(args: list[str]) -> bool:
    return bool(args) and all(not re.fullmatch(r"[;&|<>`]+", arg) for arg in args)


def python_helper_segment_allowed(tokens: list[str]) -> bool:
    if len(tokens) < 3 or tokens[0] not in {"python", "python3"}:
        return False
    helper = tokens[1]
    args = tokens[2:]
    approved_clear_paths = {
        REHYDRATION_CLEAR_HELPER,
        "~/.claude/hooks/rehydration-clear.py",
        "$HOME/.claude/hooks/rehydration-clear.py",
        "${HOME}/.claude/hooks/rehydration-clear.py",
    }
    if helper in approved_clear_paths:
        return clear_helper_args_allowed(args)
    if helper == REHYDRATION_SCHEMA_HELPER:
        return schema_helper_args_allowed(args)
    if helper == SECRET_REDACTOR_HELPER:
        return redactor_helper_args_allowed(args)
    return False


def is_audited_read_only_helper(cmd: str) -> bool:
    tokens = shell_tokens(cmd.strip())
    if not tokens:
        return False
    tokens = normalize_read_only_tokens(tokens)
    if not tokens:
        return False
    segments = split_pipeline_segments(tokens)
    if not segments or not python_helper_segment_allowed(segments[0]):
        return False
    return all(read_only_tokens(segment) for segment in segments[1:])


def is_audited_clear(cmd: str) -> bool:
    tokens = shell_tokens(cmd.strip())
    if not tokens or any(re.fullmatch(r"[;&|<>`]+", token) for token in tokens):
        return False
    approved_paths = {
        REHYDRATION_CLEAR_HELPER,
        "~/.claude/hooks/rehydration-clear.py",
        "$HOME/.claude/hooks/rehydration-clear.py",
        "${HOME}/.claude/hooks/rehydration-clear.py",
    }
    if len(tokens) < 4 or tokens[0] != "python3" or tokens[1] not in approved_paths:
        return False

    return clear_helper_args_allowed(tokens[2:])


def is_read_only_allowed(cmd: str) -> bool:
    if is_audited_clear(cmd) or is_audited_read_only_helper(cmd):
        return True
    if SHELL_MUTATION_RE.search(cmd):
        return False
    return is_read_only_shell_command(cmd)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not cmd:
        return 0

    if not mutates_rehydration_target(cmd):
        active, scope = active_rehydration_flag(payload)
        if active and not is_read_only_allowed(cmd):
            deny(
                f"Rehydration flag active for {scope}; Bash is temporarily read-only. "
                "Allowed before clearing: git status/diff/log/show/rev-parse, optional cd <dir> && read-only "
                "rg/grep/sed/head/tail/cat/jq/wc/find/ls/pwd/shasum commands joined with &&, ;, or pipes, "
                "bd prime/ready/show/list/memories/search/stats, "
                "or the audited rehydration-clear.py helper. Produce the recovery packet first."
            )
        return 0

    deny(
        "Direct shell mutation of Claude rehydration flags is blocked. "
        "After reconstructing ACTIVE_CONTRACT/LAST_USER_INTENT/CURRENT_FRONTIER, run: "
        'python3 __CLAUDE_HOME__/hooks/rehydration-clear.py --session-id "<session_id>" '
        '--reason "<what was reconstructed>"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
