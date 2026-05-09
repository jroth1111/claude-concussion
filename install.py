#!/usr/bin/env python3
"""Install the Claude-only compaction hook control layer."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PAYLOAD = ROOT / "payload"
SETTINGS_FRAGMENT = ROOT / "templates" / "settings.fragment.json"
PACKAGE_ID = "claude_compaction_hooks_installer"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def render_text(text: str, claude_home: Path) -> str:
    return text.replace("__CLAUDE_HOME__", str(claude_home))


def copy_tree(src: Path, dst: Path, claude_home: Path, dry_run: bool) -> list[str]:
    copied: list[str] = []
    if not src.exists():
        return copied
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        target = dst / rel
        copied.append(str(target))
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            shutil.copy2(path, target)
        else:
            target.write_text(render_text(text, claude_home))
            shutil.copystat(path, target, follow_symlinks=True)
        if target.suffix in {".py", ".sh"} or os.access(path, os.X_OK):
            mode = target.stat().st_mode
            target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return copied


def owned_hook_entry(entry: dict[str, Any], claude_home: Path) -> bool:
    commands = [
        str(hook.get("command") or "")
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    ]
    needles = [
        f"{claude_home}/hooks/",
        "__CLAUDE_HOME__/hooks/",
    ]
    return any(any(needle in command for needle in needles) for command in commands)


def merge_hooks(existing: dict[str, Any], fragment: dict[str, Any], claude_home: Path) -> dict[str, Any]:
    merged = dict(existing)
    merged_hooks = dict(merged.get("hooks") or {})
    for event, new_entries in (fragment.get("hooks") or {}).items():
        old_entries = merged_hooks.get(event) or []
        if not isinstance(old_entries, list):
            old_entries = []
        kept = [
            entry
            for entry in old_entries
            if not (isinstance(entry, dict) and owned_hook_entry(entry, claude_home))
        ]
        merged_hooks[event] = kept + new_entries
    merged["hooks"] = merged_hooks
    return merged


def render_json(value: Any, claude_home: Path) -> Any:
    if isinstance(value, str):
        return render_text(value, claude_home)
    if isinstance(value, list):
        return [render_json(item, claude_home) for item in value]
    if isinstance(value, dict):
        return {key: render_json(child, claude_home) for key, child in value.items()}
    return value


def install(args: argparse.Namespace) -> int:
    claude_home = Path(args.claude_home).expanduser().resolve()
    settings_path = claude_home / "settings.json"
    fragment = render_json(load_json(SETTINGS_FRAGMENT), claude_home)

    copied: list[str] = []
    copied.extend(copy_tree(PAYLOAD / "hooks", claude_home / "hooks", claude_home, args.dry_run))
    copied.extend(copy_tree(PAYLOAD / "agent-control", claude_home / "agent-control", claude_home, args.dry_run))
    copied.extend(copy_tree(PAYLOAD / "docs", claude_home / "agent-control" / "docs", claude_home, args.dry_run))

    existing = load_json(settings_path)
    merged = merge_hooks(existing, fragment, claude_home)
    merged.setdefault("env", {})
    merged["env"].setdefault("CLAUDE_STATE_DIR", str(claude_home / "state"))

    if args.dry_run:
        print(f"dry-run target: {claude_home}")
        print(f"would copy {len(copied)} files")
        print(f"would merge hook events: {', '.join(sorted(fragment.get('hooks', {}).keys()))}")
        return 0

    claude_home.mkdir(parents=True, exist_ok=True)
    if settings_path.exists() and not args.no_backup:
        backup = settings_path.with_suffix(settings_path.suffix + f".backup-{int(time.time())}")
        shutil.copy2(settings_path, backup)
        print(f"settings backup: {backup}")
    settings_path.write_text(json.dumps(merged, indent=2, sort_keys=False) + "\n")

    receipt = claude_home / "state" / "compaction-hooks-installer.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "package": PACKAGE_ID,
        "installed_at_epoch": int(time.time()),
        "claude_home": str(claude_home),
        "copied_files": len(copied),
        "settings": str(settings_path),
    }, indent=2) + "\n")
    print(f"installed {len(copied)} files into {claude_home}")
    print(f"settings merged: {settings_path}")
    print(f"receipt: {receipt}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-home", default="~/.claude", help="Claude settings/home directory to install into")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without writing")
    parser.add_argument("--no-backup", action="store_true", help="do not backup an existing settings.json")
    args = parser.parse_args()
    return install(args)


if __name__ == "__main__":
    raise SystemExit(main())
