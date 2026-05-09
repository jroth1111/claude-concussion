#!/usr/bin/env python3
"""Regression probes for v4 control-layer hook fixes.

This script invokes hook handlers with synthetic JSON only. It does not execute the
Bash commands contained in the payloads and does not read secret file contents.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / ".claude" / "hooks"


def run_hook(
    script: str,
    payload: dict,
    *,
    args: list[str] | None = None,
    env: dict | None = None,
    cwd: str | Path | None = None,
) -> dict:
    merged_env = os.environ.copy()
    if env:
        merged_env.update({key: str(value) for key, value in env.items()})
    proc = subprocess.run(
        ["python3", str(HOOKS / script), *(args or [])],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd or ROOT),
        env=merged_env,
        timeout=10,
    )
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "blocked": bool(proc.stdout.strip()),
    }


def record(rows: list[dict], name: str, result: dict, expected_block: bool) -> None:
    rows.append(
        {
            "probe": name,
            "expected_block": expected_block,
            "outcome": "OK" if result["blocked"] == expected_block else "FAIL",
            "exit_code": result["exit_code"],
            "stdout_excerpt": result["stdout"][:260],
            "stderr_excerpt": result["stderr"][:160],
        }
    )


def main() -> int:
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="v4-hook-home-") as home, tempfile.TemporaryDirectory(prefix="v4-hook-cwd-") as cwd:
        Path(cwd, ".git", "objects").mkdir(parents=True, exist_ok=True)
        env = {"HOME": home}

        def bash_payload(command: str) -> dict:
            return {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}

        for name, command, expected in [
            ("BASH_PROTECTED_CLAUDE_GLOB_BLOCKED", "rm -rf ~/.claude/*", True),
            ("BASH_PROTECTED_SSH_GLOB_BLOCKED", "rm -rf ~/.ssh/*", True),
            ("BASH_PROTECTED_GIT_SUBTREE_BLOCKED", "rm -rf .git/objects", True),
            ("BASH_GIT_FORCE_REFSPEC_BLOCKED", "git push origin +main", True),
            ("BASH_NODE_MODULES_ALLOWED", "rm -rf node_modules", False),
            ("BASH_NON_FORCE_PUSH_ALLOWED", "git push origin main", False),
        ]:
            record(rows, name, run_hook("bash-safety-gate.py", bash_payload(command), env=env, cwd=cwd), expected)

        state = Path(home, ".claude", "state")
        (state / "rehydration").mkdir(parents=True, exist_ok=True)
        (state / "rehydration" / "sid.json").write_text('{"armed_at":"2026-05-09T00:00:00Z"}', encoding="utf-8")
        reh_env = {"HOME": home, "CLAUDE_STATE_DIR": str(state)}

        def reh_payload(command: str) -> dict:
            return {"tool_name": "Bash", "cwd": cwd, "session_id": "sid", "tool_input": {"command": command}}

        for name, command, expected in [
            ("REHYDRATION_GIT_BRANCH_DELETE_BLOCKED", "git branch -D feature", True),
            ("REHYDRATION_GIT_BRANCH_CREATE_BLOCKED", "git branch feature-x", True),
            ("REHYDRATION_GIT_REMOTE_REMOVE_BLOCKED", "git remote remove origin", True),
            ("REHYDRATION_SED_IN_PLACE_BLOCKED", "sed -i.bak 's/a/b/' file.txt", True),
            ("REHYDRATION_SCHEMA_TEMPLATE_ALLOWED", "python3 __CLAUDE_HOME__/hooks/rehydration_schema.py --template 2>&1 | head -40", False),
            ("REHYDRATION_CLEAR_CHECK_ONLY_ALLOWED", 'python3 __CLAUDE_HOME__/hooks/rehydration-clear.py --check-only --session-id sid --reason "packet check"', False),
            ("REHYDRATION_REDACTOR_PIPE_ALLOWED", "python3 __CLAUDE_HOME__/hooks/secret-redactor.py __CLAUDE_HOME__/projects/session.jsonl 2>/dev/null | tail -20", False),
            ("REHYDRATION_GIT_STATUS_ALLOWED", "git status --short", False),
            ("REHYDRATION_GIT_BRANCH_SHOW_ALLOWED", "git branch --show-current", False),
            ("REHYDRATION_GIT_REMOTE_V_ALLOWED", "git remote -v", False),
            ("REHYDRATION_SED_READ_ALLOWED", "sed -n '1,5p' file.txt", False),
            ("REHYDRATION_SEMICOLON_READ_CHAIN_ALLOWED", "rg -n alpha file.txt; rg -n beta file.txt; sed -n '1,5p' file.txt", False),
            ("REHYDRATION_SEMICOLON_MUTATION_CHAIN_BLOCKED", "rg -n alpha file.txt; rm -f workspace-mutation", True),
        ]:
            record(rows, name, run_hook("rehydration-clear-guard.py", reh_payload(command), env=reh_env, cwd=cwd), expected)

        no_evidence_payload = {
            "hook_event_name": "TaskCompleted",
            "session_id": "sid",
            "task_id": "task-001",
            "task_subject": "Implement authentication",
            "task_description": "Add login and signup endpoints",
            "cwd": cwd,
        }
        evidence_payload = dict(no_evidence_payload, task_description="Evidence: pytest exit_code 0")
        blocked_payload = dict(no_evidence_payload, task_description="Blocked: dependency unavailable")
        record(
            rows,
            "TASK_COMPLETED_WITHOUT_EVIDENCE_BLOCKED",
            run_hook("task-lifecycle.py", no_evidence_payload, args=["--event", "completed"], env=env, cwd=cwd),
            True,
        )
        record(
            rows,
            "TASK_COMPLETED_WITH_EVIDENCE_ALLOWED",
            run_hook("task-lifecycle.py", evidence_payload, args=["--event", "completed"], env=env, cwd=cwd),
            False,
        )
        record(
            rows,
            "TASK_COMPLETED_BLOCKED_STATUS_ALLOWED",
            run_hook("task-lifecycle.py", blocked_payload, args=["--event", "completed"], env=env, cwd=cwd),
            False,
        )

        record(rows, "SECRETS_SSH_PRIVATE_KEY_BLOCKED", run_hook("secrets-guard.py", bash_payload("cat ~/.ssh/id_rsa"), env=env, cwd=cwd), True)
        record(rows, "SECRETS_PUBLIC_KEY_ALLOWED", run_hook("secrets-guard.py", bash_payload("cat ~/.ssh/id_rsa.pub"), env=env, cwd=cwd), False)
        record(rows, "SECRETS_ENV_BLOCKED", run_hook("secrets-guard.py", bash_payload("cat .env"), env=env, cwd=cwd), True)
        redactor_pipe = "python3 __CLAUDE_HOME__/hooks/secret-redactor.py __CLAUDE_HOME__/projects/session.jsonl 2>/dev/null | tail -30 | grep -o '\"id\":\"[^\"]*\"' | head -20"
        record(rows, "SECRETS_REDACTOR_PIPE_ALLOWED", run_hook("secrets-guard.py", bash_payload(redactor_pipe), env=env, cwd=cwd), False)

    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0 if all(row["outcome"] == "OK" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
