#!/usr/bin/env python3
"""Print redacted views of sensitive Claude files.

This is the approved path for inspecting or archiving files that may contain
tokens, environment values, transcript content, or state ledgers.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


MAX_FILE_BYTES = 2 * 1024 * 1024
SECRET_PATTERNS = (
    re.compile(r"(?i)([\"']?\bAuthorization[\"']?\s*[:=]\s*[\"']?\s*Bearer\s+)(?!\$\{)([^\"'\s,}]+)"),
    re.compile(r"(?i)((?<!\$\{)[\"']?\b[A-Z0-9_]*(?:API|AUTH|BEARER|KEY|SECRET|TOKEN)[A-Z0-9_]*[\"']?\s*[=:]\s*[\"']?)(?!\$\{)([^\"'\s]+)"),
    re.compile(r"(?i)((?:\\[\"']|\b)[A-Z0-9_]*(?:API|AUTH|BEARER|KEY|SECRET|TOKEN)[A-Z0-9_]*(?:\\[\"'])?\s*[=:]\s*(?:\\[\"'])?)(?!\$\{)([^\\\"'\s,}]+)"),
    re.compile(r"(?i)(Bearer\s+)(?!\$\{)([A-Za-z0-9._~+/=-]{20,})"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)\bsk-(?:proj|ant|svcacct)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[opsur]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda m: m.group(1) + "<redacted>", redacted)
        else:
            redacted = pattern.sub("<redacted>", redacted)
    return redacted


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(child) for key, child in value.items()}
    return value


def emit_jsonl_line(lineno: int, raw: str) -> None:
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {
            "redaction_parse_error": True,
            "line": lineno,
            "redacted_text": redact_text(raw),
        }
    print(json.dumps(redact_value(parsed), sort_keys=True, ensure_ascii=False))


def emit_jsonl_text(text: str) -> None:
    for lineno, raw in enumerate(text.splitlines(), 1):
        emit_jsonl_line(lineno, raw)


def emit_jsonl_stream(path: Path) -> None:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for lineno, raw in enumerate(handle, 1):
            emit_jsonl_line(lineno, raw.rstrip("\n"))


def should_stream(path: Path) -> bool:
    return path.suffix == ".jsonl"


def iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    files: list[Path] = []
    for child in sorted(path.rglob("*")):
        if child.is_file():
            files.append(child)
    return files


def emit_file(path: Path) -> None:
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES and not should_stream(path):
            print(f"===== {path} =====")
            print(f"<redacted: file exceeds {MAX_FILE_BYTES} bytes>")
            return
        if should_stream(path):
            print(f"===== {path} =====")
            emit_jsonl_stream(path)
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"===== {path} =====")
        print(f"<unreadable: {exc}>")
        return

    print(f"===== {path} =====")
    if path.suffix == ".jsonl":
        emit_jsonl_text(text)
        return
    try:
        parsed = json.loads(text)
    except Exception:
        print(redact_text(text).rstrip())
    else:
        print(json.dumps(redact_value(parsed), indent=2, sort_keys=True, ensure_ascii=False).rstrip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    any_file = False
    for raw in args.paths:
        for path in iter_files(Path(raw).expanduser()):
            any_file = True
            emit_file(path)
    if not any_file:
        print("no readable files matched", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
