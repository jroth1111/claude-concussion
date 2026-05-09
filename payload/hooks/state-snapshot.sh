#!/usr/bin/env bash
# Filesystem + bd + evidence snapshot for SessionStart and PreCompact.
# Goal: maximize rehydration signal across compaction events.
# Output is consumed as additionalContext by the harness.

set -u

# Parse hook payload from stdin so we can distinguish PreCompact (auto-arms
# rehydration flag) from SessionStart (informational only) and detect the
# trigger source.
hook_payload=""
if [ ! -t 0 ]; then
  hook_payload=$(cat)
fi
hook_event=$(printf '%s' "${hook_payload}" | jq -r '.hook_event_name // ""' 2>/dev/null)
hook_trigger=$(printf '%s' "${hook_payload}" | jq -r '.trigger // ""' 2>/dev/null)
hook_source=$(printf '%s' "${hook_payload}" | jq -r '.source // ""' 2>/dev/null)
hook_session_id=$(printf '%s' "${hook_payload}" | jq -r '.session_id // .sessionId // ""' 2>/dev/null)
hook_transcript_path=$(printf '%s' "${hook_payload}" | jq -r '.transcript_path // .transcriptPath // ""' 2>/dev/null)

ts=$(date -u +%FT%TZ)
state_dir="${CLAUDE_STATE_DIR:-${HOME}/.claude/state}"
evidence_file="${state_dir}/evidence-index.jsonl"
heartbeat_file="${state_dir}/heartbeat.jsonl"
rehydration_dir="${state_dir}/rehydration"

safe_session_id=""
if [ -n "${hook_session_id:-}" ]; then
  safe_session_id=$(printf '%s' "${hook_session_id}" | sed 's/[^A-Za-z0-9_.-]/_/g' | cut -c1-160)
fi

if [ -n "${CLAUDE_REHYDRATION_FLAG:-}" ]; then
  rehydration_flag="${CLAUDE_REHYDRATION_FLAG}"
  rehydration_scope="override"
elif [ -n "${safe_session_id}" ]; then
  rehydration_flag="${rehydration_dir}/${safe_session_id}.json"
  rehydration_scope="session:${hook_session_id}"
else
  rehydration_flag="${state_dir}/rehydration-required"
  rehydration_scope="legacy-global:no-session-id"
fi

echo "## state-snapshot ${ts}"
echo "cwd: $(pwd)"

# Layer 2 — auto-warn if prior heartbeat suggests hook inactivity.
# State-snapshot only runs when at least one hook surface is alive, so this
# catches partial inactivity (e.g., PostToolUse dead while SessionStart works).
if [ -f "${heartbeat_file}" ]; then
  last_hb_ts=$(tail -1 "${heartbeat_file}" 2>/dev/null | jq -r '.ts // ""' 2>/dev/null)
  if [ -n "${last_hb_ts}" ]; then
    last_hb_epoch=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "${last_hb_ts}" +%s 2>/dev/null \
      || date -d "${last_hb_ts}" +%s 2>/dev/null \
      || echo 0)
    now_epoch=$(date -u +%s)
    if [ "${last_hb_epoch}" -gt 0 ]; then
      age_h=$(( (now_epoch - last_hb_epoch) / 3600 ))
      if [ "${age_h}" -gt 24 ]; then
        echo "### ⚠ hook freshness"
        echo "prior heartbeat is ${age_h}h old — hooks may have been inactive."
        echo "run ~/.claude/agent-control/check-hooks.sh to verify."
      fi
    fi
  fi
fi

# Rehydration flag — surfaces when write barrier is active
if [ -f "${rehydration_flag}" ]; then
  echo "### rehydration: REQUIRED"
  echo "scope: ${rehydration_scope}"
  echo "Flag at ${rehydration_flag} — Write/Edit/MultiEdit/NotebookEdit blocked for this scope until cleared."
  flag_reason=$(jq -r '"auto-armed by PreCompact at \(.armed_at // .ts // "unknown")\ntrigger: \(.trigger // "unknown")\ntree: \(.tree // "unknown")"' "${rehydration_flag}" 2>/dev/null \
    || cat "${rehydration_flag}" 2>/dev/null | head -3)
  if [ -n "${flag_reason}" ]; then
    echo "reason:"
    echo "${flag_reason}"
  fi
  if [ -d "${rehydration_dir}" ]; then
    other_count=$(find "${rehydration_dir}" -type f -name '*.json' ! -path "${rehydration_flag}" 2>/dev/null | wc -l | tr -d ' ')
    if [ "${other_count:-0}" -gt 0 ]; then
      echo "other_pending_rehydrations: ${other_count} (not blocking this session)"
    fi
  fi
  cat <<'EOF'
### REQUIRED NEXT ACTION
Do not answer substantively, decide no-op, or edit from the compact summary alone.
The compact summary is a projection; the last user intent, tracker state, files, and verification artifacts are authority.

Before any final answer or edit, produce or update this packet. This template is generated from hooks/rehydration-schema.json:
EOF
  python3 __CLAUDE_HOME__/hooks/rehydration_schema.py --template 2>/dev/null || cat <<'EOF'
DIRECTIVE_CANDIDATES:
- latest normal user message:
- latest queued command:
- latest queued-command attachment:
- last-prompt record:
- compact summary claimed current request:
- post-compact user messages:
SELECTED_DIRECTIVE:
- chosen directive:
- why it outranks other candidates:
- conflict/uncertainty:
- confidence:
ACTIVE_CONTRACT:
LAST_USER_INTENT:
BD_ISSUE:
OPEN_ITEMS:
CURRENT_FRONTIER:
LAST_VERIFIED_BOUNDARY:
MODIFIED_FILES:
ACTIVE_BACKGROUND_TASKS:
COMPACT_SUMMARY_STATUS:
EVIDENCE_CONSULTED:
NEXT_ACTION:
STOP_CONDITION:
EOF
  cat <<'EOF'

Directive precedence: post-compact user messages and queued commands outrank compact-summary claims when they conflict.

EOF
  echo "Clear only after reconstruction using:"
  if [ -n "${hook_session_id:-}" ]; then
    printf 'python3 __CLAUDE_HOME__/hooks/rehydration-clear.py --session-id %s --reason "<what was reconstructed>"\n' "${hook_session_id}"
  else
    echo 'python3 __CLAUDE_HOME__/hooks/rehydration-clear.py --reason "<what was reconstructed>"'
  fi
  python3 - "${hook_session_id:-}" "${hook_transcript_path:-}" <<'PY' 2>/dev/null || true
import glob
import json
import os
import sys
from pathlib import Path

session_id = sys.argv[1]
transcript_arg = sys.argv[2]


def clip(text: str, limit: int = 500) -> str:
    text = " ".join((text or "").split())
    return text[: limit - 3] + "..." if len(text) > limit else text


def content_text(message):
    content = (message or {}).get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "tool_result":
                # Tool results are not user intent.
                continue
        return "\n".join(parts)
    return ""


def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)


def has_queued_command_marker(obj):
    haystack = "\n".join(walk_strings(obj)).lower()
    return "queued_command" in haystack or "queued command" in haystack or "queue-operation" in haystack


def queued_text(obj):
    preferred = []
    if isinstance(obj, dict):
        for key in ("command", "prompt", "text", "content", "lastPrompt"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                preferred.append(value)
    if preferred:
        return "\n".join(preferred)
    return "\n".join(s for s in walk_strings(obj) if s.strip())


def find_transcript():
    if transcript_arg and Path(transcript_arg).is_file():
        return Path(transcript_arg)
    if session_id:
        matches = glob.glob(str(Path.home() / ".claude" / "projects" / "**" / f"{session_id}.jsonl"), recursive=True)
        if matches:
            return Path(matches[0])
    return None


path = find_transcript()
if not path:
    print("### transcript frontier seed")
    print("transcript: unavailable from hook payload")
    print("action: use visible compact summary + bd + files; if intent conflict remains, inspect ~/.claude/projects JSONL")
    raise SystemExit

last_user_before = ""
last_user_after = ""
last_prompt_record = ""
compact_summary = ""
latest_normal_user = ""
latest_queued_command = ""
latest_queued_attachment = ""
last_compact_line = 0
modified_files = []
post_compact_events = []
post_compact_directives = []

with path.open("r", encoding="utf-8", errors="ignore") as f:
    for lineno, line in enumerate(f, 1):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        typ = obj.get("type")
        if typ == "system" and obj.get("subtype") == "compact_boundary":
            last_compact_line = lineno
            last_user_after = ""
            compact_summary = ""
            post_compact_events.clear()
            post_compact_directives.clear()
            continue
        if typ == "last-prompt":
            last_prompt_record = obj.get("lastPrompt", "") or last_prompt_record
        if typ == "queue-operation" or (typ != "user" and has_queued_command_marker(obj)):
            text = queued_text(obj).strip()
            if text:
                latest_queued_command = text
                if last_compact_line and lineno > last_compact_line:
                    post_compact_directives.append(f"queued@{lineno}: {clip(text, 220)}")
        if typ == "user":
            text = content_text(obj.get("message", {})).strip()
            if not text:
                continue
            if has_queued_command_marker(obj):
                latest_queued_attachment = text
                if last_compact_line and lineno > last_compact_line:
                    post_compact_directives.append(f"queued_attachment@{lineno}: {clip(text, 220)}")
            else:
                latest_normal_user = text
            if last_compact_line and lineno > last_compact_line:
                if not compact_summary and text.startswith("This session is being continued"):
                    compact_summary = text
                else:
                    last_user_after = text
                    post_compact_events.append(f"user@{lineno}: {clip(text, 220)}")
                    post_compact_directives.append(f"user@{lineno}: {clip(text, 220)}")
            else:
                last_user_before = text
        if typ == "assistant":
            msg = obj.get("message", {})
            for item in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    name = item.get("name", "")
                    inp = item.get("input", {}) if isinstance(item.get("input"), dict) else {}
                    file_path = inp.get("file_path") or inp.get("notebook_path")
                    if file_path and file_path not in modified_files and name in {"Write", "Edit", "MultiEdit", "NotebookEdit", "Read"}:
                        modified_files.append(file_path)
                    if last_compact_line and lineno > last_compact_line:
                        target = file_path or inp.get("command", "")
                        post_compact_events.append(f"{name}@{lineno}: {clip(target, 220)}")

print("### transcript frontier seed")
print(f"transcript: {path}")
if last_compact_line:
    print(f"last_compact_line: {last_compact_line}")
print("directive_candidates:")
if latest_normal_user:
    print(f"- latest normal user message: {clip(latest_normal_user, 260)}")
if latest_queued_command:
    print(f"- latest queued command: {clip(latest_queued_command, 260)}")
if latest_queued_attachment:
    print(f"- latest queued-command attachment: {clip(latest_queued_attachment, 260)}")
if last_prompt_record:
    print(f"- last-prompt record: {clip(last_prompt_record, 260)}")
    print(f"last_prompt_record: {clip(last_prompt_record)}")
if compact_summary:
    print(f"- compact summary claimed current request: {clip(compact_summary, 260)}")
if last_user_before:
    print(f"last_user_before_compact: {clip(last_user_before)}")
if last_user_after:
    print(f"last_user_after_compact: {clip(last_user_after)}")
if post_compact_directives:
    print("post_compact_directives:")
    for directive in post_compact_directives[-6:]:
        print(f"- {directive}")
if compact_summary:
    conflict_bits = []
    lower = compact_summary.lower()
    if "text only" in lower or "do not call any tools" in lower or "final turn:" in lower:
        conflict_bits.append("summary_contains_no-tool_or_final-turn_instruction")
    if last_user_before and clip(last_user_before, 120) not in compact_summary:
        conflict_bits.append("summary_may_not_preserve_last_user_prompt_verbatim")
    if conflict_bits:
        print(f"compact_summary_warning: {', '.join(conflict_bits)}")
if modified_files:
    print("modified_or_read_files_tail:")
    for fp in modified_files[-8:]:
        print(f"- {fp}")
if post_compact_events:
    print("post_compact_events_tail:")
    for event in post_compact_events[-6:]:
        print(f"- {event}")
PY
fi

# Git state
if git rev-parse --git-dir >/dev/null 2>&1; then
  echo "### git"
  echo "branch: $(git branch --show-current 2>/dev/null || echo '(detached)')"
  status=$(git status -s 2>/dev/null | head -30)
  if [ -n "$status" ]; then
    echo "status:"
    echo "$status"
    # Uncommitted file count summary
    n_changed=$(git status -s 2>/dev/null | wc -l | tr -d ' ')
    n_untracked=$(git ls-files --others --exclude-standard 2>/dev/null | wc -l | tr -d ' ')
    echo "summary: ${n_changed} changed, ${n_untracked} untracked"
  else
    echo "status: clean"
  fi
  echo "recent:"
  git log --oneline -5 2>/dev/null || true
fi

# Beads in-progress and recent memories (durable across compaction)
if command -v bd >/dev/null 2>&1; then
  in_progress=$(bd list --status=in_progress --json 2>/dev/null \
    | jq -r '.[]? | "- \(.id): \(.title)"' 2>/dev/null)
  if [ -n "$in_progress" ]; then
    echo "### bd in-progress"
    echo "$in_progress"
  fi
  # Recent memories help recover insight that summaries flatten.
  # bd memories JSON is a flat dict keyed by id (with schema_version meta).
  memories=$(bd memories --json 2>/dev/null \
    | jq -r 'to_entries | map(select(.key != "schema_version")) | .[-5:] | .[] | "- [\(.key)] \(.value)"' 2>/dev/null)
  if [ -n "$memories" ]; then
    echo "### bd memories (recent 5)"
    echo "$memories"
  fi
fi

# Evidence index tail — high-signal tool outputs captured this session
if [ -f "${evidence_file}" ]; then
  evidence_since=""
  if [ -f "${rehydration_flag}" ]; then
    evidence_since=$(grep -Eo '20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "${rehydration_flag}" 2>/dev/null | head -1 || true)
  fi
  errors_recent=""
  if [ -n "${hook_session_id:-}" ]; then
    errors_recent=$(tail -300 "${evidence_file}" 2>/dev/null \
      | jq -r --arg sid "${hook_session_id}" --arg since "${evidence_since}" '
          select(.severity=="error")
          | select(.session == $sid)
          | select($since == "" or .ts >= $since)
          | "- \(.ts) \(.tool): \(.summary)"' 2>/dev/null \
      | tail -5)
  fi
  if [ -n "${errors_recent}" ]; then
    echo "### evidence: current-session errors"
    echo "${errors_recent}"
  elif [ -n "${hook_session_id:-}" ]; then
    echo "### evidence: current-session errors"
    echo "none since ${evidence_since:-session start / available index tail}"
  else
    echo "### evidence: current-session errors"
    echo "not shown: hook payload did not include session_id; inspect ${evidence_file} if needed."
  fi
  if [ -n "${hook_session_id:-}" ]; then
    recent_evidence=$(tail -300 "${evidence_file}" 2>/dev/null \
      | jq -c --arg sid "${hook_session_id}" --arg since "${evidence_since}" '
          select(.session == $sid)
          | select($since == "" or .ts >= $since)
	          | {
	              evidence_id: (.evidence_id // ""),
	              ts, tool, severity, summary,
	              file_path: (.file_path // ""),
	              command: (.command // ""),
	              test_outcome: (.test_outcome // ""),
	              exit_code: (.exit_code // ""),
	              background_tasks: (.background_tasks // [])
	            }' 2>/dev/null)
    if [ -n "${recent_evidence}" ]; then
      echo "### recent evidence to consult"
	      printf '%s\n' "${recent_evidence}" | jq -r '
	        select(.severity=="mutation")
	        | "- \(.evidence_id // "legacy") \(.ts) \(.tool): \(.file_path // .summary)"' 2>/dev/null | tail -8 \
	        | sed '1s/^/recent_mutations:\n/' || true
	      printf '%s\n' "${recent_evidence}" | jq -r '
	        select(.tool=="Read")
	        | "- \(.evidence_id // "legacy") \(.ts) \(.file_path)"' 2>/dev/null | tail -8 \
	        | sed '1s/^/recent_reads:\n/' || true
	      printf '%s\n' "${recent_evidence}" | jq -r '
	        select(.tool=="Bash")
	        | "- \(.evidence_id // "legacy") \(.ts) \(.summary)"' 2>/dev/null | tail -8 \
	        | sed '1s/^/recent_bash_commands:\n/' || true
	      printf '%s\n' "${recent_evidence}" | jq -r '
	        select(.tool=="Bash" and ((.test_outcome // "") != "" or (.summary | test("test|pytest|cargo|npm|bun|check|lint|verify|scenario|matrix"; "i"))))
	        | "- \(.evidence_id // "legacy") \(.ts) \(.summary)"' 2>/dev/null | tail -8 \
	        | sed '1s/^/recent_verification_commands:\n/' || true
	      printf '%s\n' "${recent_evidence}" | jq -r '
	        select(((.background_tasks // []) | length) > 0) as $row
	        | $row.background_tasks[]
	        | "- session_id=\(.session_id) state=\(.state // "running") evidence=\($row.evidence_id // "legacy")"' 2>/dev/null | tail -8 \
	        | sed '1s/^/recent_background_tasks:\n/' || true
	      last_write=$(printf '%s\n' "${recent_evidence}" | jq -r '
	        select(.severity=="mutation")
	        | "\(.tool) \(.file_path // "") @ \(.ts)"' 2>/dev/null | tail -1)
      if [ -n "${last_write}" ]; then
        echo "last_write_tool: ${last_write}"
      fi
      edited_files=$(printf '%s\n' "${recent_evidence}" | jq -r '
        select(.severity=="mutation" and (.file_path // "") != "")
        | .file_path' 2>/dev/null | awk '!seen[$0]++' | tail -8)
      if [ -n "${edited_files}" ]; then
        echo "last_edited_files:"
        printf '%s\n' "${edited_files}" | sed 's/^/- /'
      fi
    fi
  fi
  evidence_count=$(wc -l < "${evidence_file}" 2>/dev/null | tr -d ' ')
  evidence_size=$(du -h "${evidence_file}" 2>/dev/null | awk '{print $1}')
  echo "### evidence-index: ${evidence_count} entries (${evidence_size}), tail at ${evidence_file}"
fi

# Layer 1 — heartbeat. Lets check-hooks.sh and Layer-2 self-check detect
# silent inactivity. Written every snapshot run. Rotates at 5MB to mirror
# evidence-index hygiene; growth is bounded but not zero.
mkdir -p "${state_dir}"
heartbeat_max=5242880
if [ -f "${heartbeat_file}" ]; then
  hb_size=$(stat -f%z "${heartbeat_file}" 2>/dev/null \
    || stat -c%s "${heartbeat_file}" 2>/dev/null \
    || echo 0)
  if [ "${hb_size}" -gt "${heartbeat_max}" ]; then
    mv -f "${heartbeat_file}" "${heartbeat_file}.1"
  fi
fi
python3 - \
  "${ts}" \
  "${hook_event:-unknown}" \
  "${hook_source:-}" \
  "$(pwd)" \
  "${hook_session_id:-}" \
  "${hook_transcript_path:-}" \
  "${CLAUDE_LAUNCHER:-}" \
  "${hook_trigger:-}" \
  "${rehydration_scope:-}" <<'PY' >> "${heartbeat_file}"
import json
import sys

print(json.dumps({
    "ts": sys.argv[1],
    "event": sys.argv[2],
    "source": sys.argv[3],
    "cwd": sys.argv[4],
    "session_id": sys.argv[5],
    "transcript_path": sys.argv[6],
    "launcher": sys.argv[7],
    "trigger": sys.argv[8],
    "rehydration_scope": sys.argv[9],
}, ensure_ascii=False))
PY

# Auto-arm rehydration flag on every PreCompact event. Per user decision:
# unconditional — cost of one redundant `rm` is minimal vs. cost of stale
# post-compact writes when arm is forgotten. Tri-state tree marker so the
# flag reason is unambiguous when no git repo is present.
if [ "${hook_event}" = "PreCompact" ]; then
  mkdir -p "$(dirname "${rehydration_flag}")"
  dirty_marker="no_git"
  if git rev-parse --git-dir >/dev/null 2>&1; then
    if [ -n "$(git status -s 2>/dev/null)" ]; then
      dirty_marker="dirty"
    else
      dirty_marker="clean"
    fi
  fi
  in_progress_count=0
  if command -v bd >/dev/null 2>&1; then
    in_progress_count=$(bd list --status=in_progress --json 2>/dev/null \
      | jq 'length' 2>/dev/null || echo 0)
  fi
  python3 - "${rehydration_flag}" "${ts}" "${hook_trigger:-unknown}" "${dirty_marker}" "${in_progress_count}" "${hook_session_id:-}" "$(pwd)" "${hook_transcript_path:-}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
session_id = sys.argv[6]
clear_cmd = "python3 __CLAUDE_HOME__/hooks/rehydration-clear.py"
if session_id:
    clear_cmd += f" --session-id {session_id}"
clear_cmd += ' --reason "<what was reconstructed>"'

path.write_text(json.dumps({
    "armed_at": sys.argv[2],
    "armed_by": "PreCompact",
    "trigger": sys.argv[3],
    "tree": sys.argv[4],
    "bd_in_progress": int(sys.argv[5]) if sys.argv[5].isdigit() else sys.argv[5],
    "session_id": session_id,
    "cwd": sys.argv[7],
    "transcript_path": sys.argv[8] if len(sys.argv) > 8 else "",
    "schema_version": 1,
    "verify": "git status / git diff / reopen modified files",
    "clear": clear_cmd,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi
