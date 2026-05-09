#!/usr/bin/env python3
"""General Bash safety gate for dangerous irreversible commands."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path


RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgit\s+commit\b[^\n;|&]*(?:\s--no-verify\b|\s-n(?:\s|$))", re.I), "git commit --no-verify/-n bypasses verification hooks"),
    (re.compile(r"\bgit\s+push\b[^\n;|&]*(?:\s--force(?:-with-lease)?\b|\s-f(?:\s|$))", re.I), "force push requires explicit user approval"),
    (re.compile(r"\bgit\s+push\b[^\n;|&]*\s--mirror\b", re.I), "git push --mirror can overwrite remote refs"),
    (re.compile(r"\bgit\s+push\b[^\n;|&]*\s\+[^\s;|&]+", re.I), "git push with +refspec can force-update remote refs"),
    (re.compile(r"\bgit\s+reset\b[^\n;|&]*\s--hard\b", re.I), "git reset --hard is destructive"),
    (re.compile(r"\bgit\s+clean\b[^\n;|&]*\s-[^\n;|&]*[fdx]", re.I), "git clean removing files is destructive"),
    (re.compile(r"\bgit\s+branch\b[^\n;|&]*(?:-D|--delete\s+--force)\s+(?:main|master|trunk|develop|prod|production)\b", re.I), "force-deleting protected branches requires explicit approval"),
    (re.compile(r"\b(?:curl|wget)\b[^\n|;]+[|]\s*(?:sudo\s+)?(?:/usr/bin/|/bin/)?(?:sh|bash)\b", re.I), "curl/wget piped to shell is blocked"),
)
PROTECTED_SUFFIXES = (".claude", ".ssh", ".gnupg")
EPHEMERAL_RM_TARGETS = {
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".turbo",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "target",
    "temp",
    "tmp",
}
CLAUDE_CONTROL_PATHS = (
    "agent-control",
    "hooks",
    "projects",
    "state",
    "scheduled_tasks.json",
    "settings.json",
    "settings.local.json",
    "mcp.json",
    "CLAUDE.md",
    "AGENTS.md",
    "RTK.md",
)

def deny(reason: str) -> None:
    print(json.dumps({
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }))
    raise SystemExit(0)


def shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except Exception:
        return []


def normalize_path(raw: str, cwd: str) -> Path:
    value = raw.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        expanded = Path(cwd or os.getcwd()) / expanded
    try:
        return expanded.resolve(strict=False)
    except Exception:
        return expanded.absolute()


def rm_flags_destructive(flags: list[str]) -> bool:
    recursive = False
    force = False
    for flag in flags:
        if flag in {"--recursive", "-r", "-R"}:
            recursive = True
        elif flag == "--force":
            force = True
        elif flag.startswith("-") and not flag.startswith("--"):
            recursive = recursive or "r" in flag.lower()
            force = force or "f" in flag.lower()
    return recursive and force


def path_inside(path: Path, base: Path) -> bool:
    try:
        return path == base or path.is_relative_to(base)
    except Exception:
        return False


def safe_ephemeral_rm_target(target: Path, home: Path) -> bool:
    if target.name not in EPHEMERAL_RM_TARGETS:
        return False
    never_ephemeral = (
        home / ".claude" / "projects",
        home / ".claude" / "state",
    )
    return not any(path_inside(target, protected) for protected in never_ephemeral)


def protected_rm_target(target: Path, cwd: str) -> bool:
    home = Path.home().resolve(strict=False)
    root = Path("/")
    cwd_path = normalize_path(cwd, cwd)
    protected = {
        root,
        home,
        cwd_path,
        cwd_path / ".git",
        home / ".claude",
        home / ".ssh",
        home / ".gnupg",
    }
    if target in protected:
        return True
    if safe_ephemeral_rm_target(target, home):
        return False
    protected_roots = (
        cwd_path / ".git",
        home / ".claude",
        home / ".ssh",
        home / ".gnupg",
    )
    if any(path_inside(target, protected_path) for protected_path in protected_roots):
        return True
    control_paths = tuple(home / ".claude" / rel for rel in CLAUDE_CONTROL_PATHS)
    try:
        if any(path_inside(target, protected_path) for protected_path in control_paths):
            return True
    except Exception:
        pass
    return target.name in PROTECTED_SUFFIXES


def scan_rm(tokens: list[str], cwd: str) -> str | None:
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token in {";", "&&", "||", "|"}:
            idx += 1
            continue
        if token in {"sudo", "doas", "command"}:
            idx += 1
            continue
        if token == "env":
            idx += 1
            while idx < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[idx]):
                idx += 1
            continue
        if token != "rm":
            idx += 1
            continue
        flags: list[str] = []
        targets: list[str] = []
        idx += 1
        while idx < len(tokens) and tokens[idx] not in {";", "&&", "||", "|"}:
            current = tokens[idx]
            if current.startswith("-") and current != "--":
                flags.append(current)
            else:
                targets.append(current)
            idx += 1
        if rm_flags_destructive(flags):
            for target in targets:
                normalized = normalize_path(target, cwd)
                if protected_rm_target(normalized, cwd):
                    return f"rm -rf on protected path is destructive: {target}"
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    for pattern, reason in RULES:
        if pattern.search(command):
            deny(reason)
    rm_reason = scan_rm(shell_tokens(command), str(payload.get("cwd") or ""))
    if rm_reason:
        deny(rm_reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
