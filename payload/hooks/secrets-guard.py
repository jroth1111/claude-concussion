#!/usr/bin/env python3
"""PreToolUse guard for secrets and archive/export surfaces."""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any


SENSITIVE_PATH_RE = re.compile(
    r"(?ix)"
    r"("
    r"(?:^|[/\s'\"=])\.?claude/(?:mcp\.json|settings\.json|projects/[^;\n|&`$()<>]*\.jsonl|state/[^;\n|&`$()<>]*\.jsonl)"
    r"|(?:^|[/\s'\"=])\.env(?:\.[A-Za-z0-9_.-]+)?"
    r"|(?:^|[/\s'\"=])\.?ssh/(?:id_(?:rsa|dsa|ecdsa|ed25519)|identity)(?!\.pub)(?=$|[/\s'\";|&`$()<>])"
    r"|(?:^|[/\s'\"=])\.?gnupg/(?:private-keys-v1\.d|secring\.gpg)(?=$|[/\s'\";|&`$()<>])"
    r"|(?:^|[/\s'\"=])[^/\s'\";|&`$()<>]+\.(?:pem|key|p12|pfx)"
    r")"
)
SECRET_VALUE_RE = re.compile(
    r"(?ix)"
    r"([\"']?\bAuthorization[\"']?\s*[:=]\s*[\"']?\s*Bearer\s+(?!\$\{)[A-Za-z0-9._~+/=-]{20,})"
    r"|([\"']?\b[A-Z0-9_]*(?:API|AUTH|BEARER|KEY|SECRET|TOKEN)[A-Z0-9_]*[\"']?\s*[:=]\s*[\"']?(?!\$\{)[A-Za-z0-9._~+/=-]{20,})"
    r"|(\bsk-[A-Za-z0-9_-]{20,})"
    r"|(\bsk-(?:proj|ant|svcacct)-[A-Za-z0-9_-]{20,})"
    r"|(\bgh[opsur]_[A-Za-z0-9_]{20,})"
    r"|(\bgithub_pat_[A-Za-z0-9_]{40,})"
    r"|(\bxox[abp]-[A-Za-z0-9-]{20,})"
    r"|(\b(?:AKIA|ASIA)[A-Z0-9]{16}\b)"
    r"|(\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b)"
    r"|(-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
ARCHIVE_RE = re.compile(r"\b(?:zip|tar|gzip|bzip2|xz|7z|ditto|rsync|scp|sftp|gh\s+gist|curl\s+-F|python3?\s+-m\s+zipfile)\b", re.I)
EXPORT_RE = re.compile(r"\b(?:cat|sed|awk|grep|rg|jq|cp|mv)\b", re.I)
APPROVED_REDACTOR = "__CLAUDE_HOME__/hooks/secret-redactor.py"
SHELL_META_RE = re.compile(r"[;&|<>`]|[$][(]")
PIPELINE_FILTERS = {"head", "tail", "grep", "rg", "sed", "jq", "wc"}


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


def text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for child in value.values():
            out.extend(text_values(child))
        return out
    if isinstance(value, list):
        out = []
        for child in value:
            out.extend(text_values(child))
        return out
    return []


def pathish(tool_input: dict[str, Any]) -> str:
    keys = ("file_path", "notebook_path", "path", "pattern")
    return " ".join(str(tool_input.get(key) or "") for key in keys)


def shell_tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>`")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def normalize_safe_redirections(tokens: list[str]) -> list[str] | None:
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "2" and index + 2 < len(tokens) and tokens[index + 1] in {">", ">>"} and tokens[index + 2] in {"/dev/null", "dev/null"}:
            index += 3
            continue
        if token == "2" and index + 2 < len(tokens) and tokens[index + 1] == ">&" and tokens[index + 2] == "1":
            index += 3
            continue
        normalized.append(token)
        index += 1
    return normalized


def split_pipeline(tokens: list[str]) -> list[list[str]] | None:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token == "|":
            if not current:
                return None
            segments.append(current)
            current = []
            continue
        if token in {";", "&&", "||", "<", ">", ">>", "`"}:
            return None
        current.append(token)
    if not current:
        return None
    segments.append(current)
    return segments


def is_redactor_segment(tokens: list[str]) -> bool:
    if len(tokens) < 3:
        return False
    if tokens[0] not in {"python", "python3"} or tokens[1] != APPROVED_REDACTOR:
        return False
    return all(token and not re.fullmatch(r"[;&|<>`]+", token) for token in tokens[2:])


def is_safe_filter_segment(tokens: list[str]) -> bool:
    if not tokens or tokens[0] not in PIPELINE_FILTERS:
        return False
    if tokens[0] == "sed" and any(arg == "-i" or arg.startswith("-i") or arg == "--in-place" or arg.startswith("--in-place=") for arg in tokens[1:]):
        return False
    return all(not re.fullmatch(r"[;&|<>`]+", token) for token in tokens[1:])


def is_exact_redactor_command(command: str) -> bool:
    tokens = shell_tokens(command.strip())
    if not tokens:
        return False
    tokens = normalize_safe_redirections(tokens)
    if not tokens:
        return False
    segments = split_pipeline(tokens)
    if not segments or not is_redactor_segment(segments[0]):
        return False
    return all(is_safe_filter_segment(segment) for segment in segments[1:])


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    blob = "\n".join(text_values(tool_input))

    if SECRET_VALUE_RE.search(blob):
        deny("Cleartext secret/token material in tool input is blocked. Use environment variables or secret-redactor.py.")

    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        if not command:
            return 0
        if is_exact_redactor_command(command):
            return 0
        mentions_sensitive = bool(SENSITIVE_PATH_RE.search(command))
        if mentions_sensitive:
            deny(
                "Protected Claude transcript or secret path blocked. Use the redacted reader instead: "
                "python3 __CLAUDE_HOME__/hooks/secret-redactor.py <path> "
                "[| head/tail/rg/grep/sed/jq/wc]. Raw cat/python/cp/archive access stays denied."
            )
        if ARCHIVE_RE.search(command) and re.search(r"(?i)(\.claude|sessions|state|mcp\.json|\.env)", command):
            deny("Archive/export command touches Claude sensitive surfaces; use a redacted archive workflow.")
        return 0

    if tool in {"Read", "Grep", "Glob"} and SENSITIVE_PATH_RE.search(pathish(tool_input)):
        deny("Protected Claude transcript or secret path blocked. Use secret-redactor.py for redacted inspection.")

    if tool in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        content_blob = blob
        path_blob = pathish(tool_input)
        if SENSITIVE_PATH_RE.search(path_blob) and SECRET_VALUE_RE.search(content_blob):
            deny("Writing cleartext secrets into sensitive config files is blocked; use ${ENV_VAR} expansion.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
