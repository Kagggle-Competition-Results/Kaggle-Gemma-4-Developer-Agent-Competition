#!/usr/bin/env python3
"""Pack gemma4-agent/agent/ into submission.zip (< 3 GiB unpacked)."""
from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

MAX_UNPACKED = 3 * 1024**3


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent-dir", type=Path, default=Path("agent"))
    p.add_argument("--out", type=Path, default=Path("submission.zip"))
    args = p.parse_args()

    agent_dir = args.agent_dir.resolve()
    if not agent_dir.is_dir():
        print(f"missing agent dir: {agent_dir}", file=sys.stderr)
        return 2

    required = [agent_dir / "agent.yaml", agent_dir / "prompts"]
    for req in required:
        if not req.exists():
            print(f"missing required: {req}", file=sys.stderr)
            return 2

    files: list[Path] = []
    unpacked = 0
    for path in sorted(agent_dir.rglob("*")):
        if path.is_file():
            if path.name == ".gitkeep":
                continue
            # never pack credentials
            if path.suffix in {".pem", ".key"} or path.name.startswith(".env"):
                print(f"refusing to pack secret-like file: {path}", file=sys.stderr)
                return 2
            unpacked += path.stat().st_size
            files.append(path)

    if unpacked >= MAX_UNPACKED:
        print(f"unpacked size {unpacked} >= 3 GiB", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arc = path.relative_to(agent_dir).as_posix()
            zf.write(path, arcname=arc)

    digest = sha256_file(args.out)
    print(f"wrote {args.out} files={len(files)} unpacked_bytes={unpacked} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
