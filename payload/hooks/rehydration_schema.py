#!/usr/bin/env python3
"""Shared rehydration packet schema and transcript helpers."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_PATH = Path(__file__).with_name("rehydration-schema.json")
FIELD_RE = re.compile(r"^([A-Z][A-Z0-9_]*):\s*(.*)$", re.MULTILINE)
PROCESS_SESSION_RE = re.compile(r"Process running with session ID\s+([0-9]+)")


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def required_fields(schema: dict[str, Any] | None = None) -> list[str]:
    return list((schema or load_schema()).get("required_fields", []))


def schema_version(schema: dict[str, Any] | None = None) -> int:
    return int((schema or load_schema()).get("schema_version", 1))


def normalize_text(text: str, limit: int | None = None) -> str:
    normalized = " ".join(str(text or "").split())
    if limit is not None and len(normalized) > limit:
        return normalized[: limit - 3] + "..."
    return normalized


def parse_packet_fields(text: str, schema: dict[str, Any] | None = None) -> dict[str, str]:
    schema = schema or load_schema()
    names = set(required_fields(schema))
    matches = [m for m in FIELD_RE.finditer(text or "") if m.group(1) in names]
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1)
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        inline = match.group(2).strip()
        body = text[match.end():next_start].strip()
        fields[name] = "\n".join(part for part in (inline, body) if part).strip()
    return fields


def _placeholder_re(schema: dict[str, Any]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in schema.get("placeholder_patterns", [])), re.I)


def _field_has_allowed_literal(field: str, value: str, schema: dict[str, Any]) -> bool:
    allowed = schema.get("allowed_literal_values", {}).get(field, [])
    lowered = value.strip().lower()
    return lowered in {str(item).lower() for item in allowed}


def _semantic_label_text(text: str) -> str:
    text = str(text or "").lower()
    text = re.sub(r"[-_/]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _semantic_label_present(
    *,
    field: str,
    label: str,
    body: str,
    accepted: list[str],
    schema: dict[str, Any],
) -> bool:
    body_norm = _semantic_label_text(body)
    if any(_semantic_label_text(candidate) in body_norm for candidate in accepted):
        return True

    semantic_aliases = schema.get("semantic_label_aliases", {}).get(field, {}).get(label, [])
    if any(_semantic_label_text(candidate) in body_norm for candidate in semantic_aliases):
        return True

    if field == "DIRECTIVE_CANDIDATES" and label == "latest queued-command attachment":
        # If the packet already identified the queued command, the attachment
        # candidate is semantically covered even when there was no separate
        # attachment to name. This avoids blocking valid recovery packets on a
        # missing "attachment: none" boilerplate line.
        return "latest queued command" in body_norm or "queued command" in body_norm

    return False


def _missing_labels(fields: dict[str, str], schema: dict[str, Any]) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    aliases = schema.get("field_label_aliases", {})
    for field, labels in schema.get("field_labels", {}).items():
        body = fields.get(field, "")
        absent = []
        for label in labels:
            accepted = [label] + list(aliases.get(field, {}).get(label, []))
            if field == "SELECTED_DIRECTIVE" and label == "chosen directive" and _selected_directive_inline_value(body, accepted, schema):
                continue
            if not _semantic_label_present(field=field, label=label, body=body, accepted=accepted, schema=schema):
                absent.append(label)
        if absent:
            missing[field] = absent
    return missing


def _selected_directive_inline_value(body: str, accepted_labels: list[str], schema: dict[str, Any]) -> bool:
    first_line = next((line.strip() for line in str(body or "").splitlines() if line.strip()), "")
    if not first_line or first_line.startswith(("-", "*")):
        return False
    first_norm = _semantic_label_text(first_line.split(":", 1)[0] if ":" in first_line else first_line)
    known_labels = {_semantic_label_text(label) for label in accepted_labels}
    known_labels.update(_semantic_label_text(label) for label in (
        "why it outranks",
        "why it outranks other candidates",
        "conflict/uncertainty",
        "confidence",
    ))
    if first_norm in known_labels:
        return False
    return not _placeholder_re(schema).fullmatch(first_line)


def _has_textual_evidence(value: str, schema: dict[str, Any]) -> bool:
    lowered = str(value or "").lower()
    if not lowered.strip():
        return False
    markers = schema.get("evidence", {}).get("text_markers", [])
    return any(str(marker).lower() in lowered for marker in markers)


def _missing_label_aliases(missing: dict[str, list[str]], schema: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    aliases = schema.get("field_label_aliases", {})
    return {
        field: {label: list(aliases.get(field, {}).get(label, [])) for label in labels}
        for field, labels in missing.items()
    }


def _field_placeholder_sample(value: str, schema: dict[str, Any]) -> str:
    placeholder_re = _placeholder_re(schema)
    lines = [line.strip() for line in str(value or "").splitlines()]
    if len(lines) <= 1 and placeholder_re.fullmatch(str(value or "").strip()):
        return normalize_text(value, 120)
    for line in lines:
        if not line:
            continue
        candidate = re.sub(r"^\s*[-*]\s*", "", line).strip()
        if ":" in candidate:
            candidate = candidate.split(":", 1)[1].strip()
        if candidate and placeholder_re.fullmatch(candidate):
            return normalize_text(line, 120)
    return ""


def evidence_pattern(schema: dict[str, Any] | None = None) -> re.Pattern[str]:
    schema = schema or load_schema()
    return re.compile(schema["evidence"]["id_pattern"])


def evidence_refs(text: str, schema: dict[str, Any] | None = None) -> set[str]:
    return set(evidence_pattern(schema).findall(text or ""))


def validate_packet_text(
    text: str,
    *,
    schema: dict[str, Any] | None = None,
    required_evidence_ids: set[str] | None = None,
) -> dict[str, Any]:
    schema = schema or load_schema()
    fields = parse_packet_fields(text, schema)
    required = required_fields(schema)
    missing = [field for field in required if field not in fields]
    placeholders: dict[str, str] = {}
    for field in required:
        if field not in fields:
            continue
        value = fields[field].strip()
        if _field_has_allowed_literal(field, value, schema):
            continue
        placeholder = _field_placeholder_sample(value, schema)
        if placeholder:
            placeholders[field] = placeholder

    label_gaps = _missing_labels(fields, schema)
    label_aliases = _missing_label_aliases(label_gaps, schema)
    evidence_error = ""
    cited = evidence_refs(fields.get(schema["evidence"]["field"], ""), schema)
    required_evidence_ids = required_evidence_ids or set()
    if required_evidence_ids:
        missing_ids = sorted(required_evidence_ids - cited)
        if missing_ids:
            evidence_error = "missing evidence ids: " + ", ".join(missing_ids[:12])
    else:
        evidence_value = fields.get(schema["evidence"]["field"], "")
        none_token = schema["evidence"]["none_token"]
        if not cited and none_token not in evidence_value and not _has_textual_evidence(evidence_value, schema):
            evidence_error = (
                f"{schema['evidence']['field']} must cite evidence ids, {none_token}, "
                "or concrete textual evidence such as git status, file read, grep, test, compile, or bd output"
            )

    compact_status = _semantic_label_text(fields.get("COMPACT_SUMMARY_STATUS", ""))
    status_keywords = schema.get("compact_summary_status_keywords", [])
    compact_status_error = ""
    if status_keywords and not any(_semantic_label_text(keyword) in compact_status for keyword in status_keywords):
        compact_status_error = "COMPACT_SUMMARY_STATUS lacks projection/conflict/verified_against_transcript/unavailable"

    ok = not (missing or placeholders or label_gaps or evidence_error or compact_status_error)
    return {
        "ok": ok,
        "missing": missing,
        "placeholders": placeholders,
        "missing_labels": label_gaps,
        "missing_label_aliases": label_aliases,
        "evidence_error": evidence_error,
        "compact_summary_status_error": compact_status_error,
        "fields": fields,
        "cited_evidence_ids": sorted(cited),
    }


def format_validation_errors(result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if result.get("missing"):
        errors.append("missing fields: " + ", ".join(result["missing"]))
    if result.get("placeholders"):
        bits = [f"{k}={v!r}" for k, v in result["placeholders"].items()]
        errors.append("placeholder fields: " + "; ".join(bits))
    if result.get("missing_labels"):
        alias_info = result.get("missing_label_aliases") or {}
        bits = []
        for field, labels in result["missing_labels"].items():
            rendered = []
            for label in labels:
                aliases = alias_info.get(field, {}).get(label, [])
                if aliases:
                    rendered.append(f"{label} (aliases: {', '.join(aliases)})")
                else:
                    rendered.append(label)
            bits.append(f"{field}: {', '.join(rendered)}")
        errors.append("missing directive labels: " + "; ".join(bits))
    if result.get("evidence_error"):
        errors.append(str(result["evidence_error"]))
    if result.get("compact_summary_status_error"):
        errors.append(str(result["compact_summary_status_error"]))
    return errors


def packet_template(schema: dict[str, Any] | None = None) -> str:
    schema = schema or load_schema()
    lines = [f"SCHEMA_VERSION: {schema_version(schema)}"]
    for field in required_fields(schema):
        lines.append(f"{field}:")
        for label in schema.get("field_labels", {}).get(field, []):
            lines.append(f"- {label}:")
        if field == "COMPACT_SUMMARY_STATUS":
            lines.append("- use one of: projection, conflict, verified_against_transcript, unavailable")
        elif field == "EVIDENCE_CONSULTED":
            lines.append(f"- cite ev_<id>/legacy-line-<n>, or {schema['evidence']['none_token']}")
    return "\n".join(lines)


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ in {"text", "output_text", "input_text"}:
            parts.append(str(item.get("text") or ""))
        elif typ == "thinking":
            parts.append(str(item.get("thinking") or ""))
        elif typ == "tool_use":
            parts.append(f"<tool_use {item.get('name')}>")
        elif typ == "tool_result":
            parts.append(str(item.get("content") or "<tool_result>"))
    return "\n".join(parts)


def record_text(obj: dict[str, Any]) -> str:
    if obj.get("type") == "response_item" and isinstance(obj.get("payload"), dict):
        payload = obj["payload"]
        if payload.get("type") == "message":
            return text_from_content(payload.get("content"))
        if payload.get("type") == "function_call":
            return str(payload.get("arguments") or payload.get("name") or "")
        if payload.get("type") == "function_call_output":
            return str(payload.get("output") or "")
    message = obj.get("message")
    if isinstance(message, dict):
        return text_from_content(message.get("content"))
    if isinstance(message, str):
        return message
    return text_from_content(obj.get("content"))


def is_assistant_record(obj: dict[str, Any]) -> bool:
    if obj.get("type") == "assistant":
        return True
    payload = obj.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "assistant"


def is_compaction_boundary(obj: dict[str, Any]) -> bool:
    if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
        return True
    if obj.get("type") == "compacted":
        return True
    payload = obj.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "compacted"


@dataclass
class PacketCandidate:
    line: int
    text: str
    result: dict[str, Any]


def read_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for lineno, raw in enumerate(handle, 1):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, dict):
                records.append((lineno, obj))
    return records


def latest_compaction_line(records: list[tuple[int, dict[str, Any]]]) -> int:
    last = 0
    for lineno, obj in records:
        if is_compaction_boundary(obj):
            last = lineno
    return last


def latest_assistant_packet(
    transcript: Path,
    *,
    after_line: int = 0,
    schema: dict[str, Any] | None = None,
    required_evidence_ids: set[str] | None = None,
) -> PacketCandidate | None:
    schema = schema or load_schema()
    latest: PacketCandidate | None = None
    for lineno, obj in read_jsonl(transcript):
        if lineno <= after_line or not is_assistant_record(obj):
            continue
        text = record_text(obj)
        if "ACTIVE_CONTRACT:" not in text and "DIRECTIVE_CANDIDATES:" not in text:
            continue
        result = validate_packet_text(text, schema=schema, required_evidence_ids=required_evidence_ids)
        latest = PacketCandidate(lineno, text, result)
    return latest


def stable_evidence_id(entry: dict[str, Any], line_number: int | None = None) -> str:
    existing = entry.get("evidence_id")
    if isinstance(existing, str) and existing:
        return existing
    if line_number is not None:
        return f"legacy-line-{line_number}"
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


def evidence_since(
    evidence_file: Path,
    *,
    session_id: str = "",
    since_ts: str = "",
    limit: int = 40,
) -> list[dict[str, Any]]:
    if not evidence_file.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = evidence_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for lineno, raw in enumerate(lines, 1):
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        if session_id and entry.get("session") != session_id:
            continue
        if since_ts and str(entry.get("ts") or "") < since_ts:
            continue
        entry = dict(entry)
        entry["evidence_id"] = stable_evidence_id(entry, lineno)
        rows.append(entry)
    return rows[-limit:]


def nonzero_evidence_requires_classification(text: str, evidence_rows: list[dict[str, Any]]) -> str:
    if not re.search(r"\bverified\b", text or "", re.I):
        return ""
    nonzero = [
        row for row in evidence_rows
        if row.get("tool") == "Bash" and (row.get("severity") == "error" or str(row.get("exit_code", "0")) not in {"", "0", "None"})
    ]
    if not nonzero:
        return ""
    if re.search(r"expected[- ]nonzero|nonzero.*expected|failed|failure|inconclusive|fixed by later evidence|classified", text, re.I):
        return ""
    ids = ", ".join(str(row.get("evidence_id")) for row in nonzero[:8])
    return f"verified claim follows nonzero Bash evidence without classification: {ids}"


def background_sessions_from_text(text: str) -> list[str]:
    return PROCESS_SESSION_RE.findall(text or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", action="store_true")
    parser.add_argument("--fields", action="store_true")
    parser.add_argument("--validate-text", default="")
    args = parser.parse_args()
    schema = load_schema()
    if args.template:
        print(packet_template(schema))
        return 0
    if args.fields:
        print("\n".join(required_fields(schema)))
        return 0
    if args.validate_text:
        result = validate_packet_text(Path(args.validate_text).read_text(encoding="utf-8"), schema=schema)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 2
    print(json.dumps(schema, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
