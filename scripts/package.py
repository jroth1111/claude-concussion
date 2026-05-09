#!/usr/bin/env python3
"""Create a zip archive for this installer directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import zipfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--out", default=str(Path.cwd() / "dist" / "claude_concussion.zip"))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    def distributable_file(path: Path) -> bool:
        if not path.is_file() or path.name == "MANIFEST.json":
            return False
        if path.resolve() == out:
            return False
        rel_parts = path.relative_to(root).parts
        if rel_parts and rel_parts[0] == "dist":
            return False
        return not any(part in {".git", "__pycache__", ".DS_Store"} for part in rel_parts)

    entries = []
    for path in sorted(p for p in root.rglob("*") if distributable_file(p)):
        rel = path.relative_to(root).as_posix()
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append({"path": rel, "bytes": path.stat().st_size, "sha256": h})

    (root / "MANIFEST.json").write_text(json.dumps({
        "name": "claude_concussion",
        "generated_at_epoch": int(time.time()),
        "file_count_excluding_manifest": len(entries),
        "total_bytes_excluding_manifest": sum(e["bytes"] for e in entries),
        "vanilla_baseline": "@anthropic-ai/claude-code",
        "entries": entries,
    }, indent=2, sort_keys=True) + "\n")

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in root.rglob("*") if p.is_file() and (p.name == "MANIFEST.json" or distributable_file(p))):
            rel = Path(root.name) / path.relative_to(root)
            zf.write(path, rel.as_posix())
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
