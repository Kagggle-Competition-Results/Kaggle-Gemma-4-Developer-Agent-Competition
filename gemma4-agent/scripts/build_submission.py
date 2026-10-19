#!/usr/bin/env python3
"""Pack gemma4-agent/agent/ into submission.zip (< 3 GiB unpacked).

G5: zip = agent/ only (no scripts/, IDE config, credentials).
G6: single model variant (agent.yaml model pin).

CLI:
  python scripts/build_submission.py --agent-dir agent --out submission.zip
  python scripts/build_submission.py --agent-dir agent --check   # read-only G5 audit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

MAX_UNPACKED = 3 * 1024**3
FORBIDDEN_NAME_PARTS = {
    "__pycache__",
    ".DS_Store",
    ".git",
    ".idea",
    ".vscode",
    ".cursor",
    "scripts",
    ".env",
}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".pyc", ".pyo"}
ALLOWED_BASES = {
    "gemma-4-31b-it-qat-w4a16-ct",
    "gemma-4-12b-it-qat-w4a16-ct",
    "gemma-4-e4b-it-qat-w4a16-ct",
    "gemma-4-e2b-it-qat-w4a16-ct",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_forbidden(path: Path, agent_dir: Path) -> str | None:
    rel = path.relative_to(agent_dir).as_posix()
    parts = set(rel.split("/"))
    if path.name in FORBIDDEN_NAME_PARTS or parts & FORBIDDEN_NAME_PARTS:
        return f"forbidden_path:{rel}"
    if path.suffix in FORBIDDEN_SUFFIXES or path.name.startswith(".env"):
        return f"secret_or_bytecode:{rel}"
    if path.name.endswith(".code-workspace") or path.name == "Thumbs.db":
        return f"ide_junk:{rel}"
    return None


def collect_files(agent_dir: Path) -> tuple[list[Path], list[str], int]:
    files: list[Path] = []
    violations: list[str] = []
    unpacked = 0
    for path in sorted(agent_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name == ".gitkeep":
            continue
        why = _is_forbidden(path, agent_dir)
        if why:
            violations.append(why)
            continue
        unpacked += path.stat().st_size
        files.append(path)
    return files, violations, unpacked


def check_g6_model(agent_dir: Path) -> dict:
    yaml_p = agent_dir / "agent.yaml"
    model = None
    if yaml_p.exists():
        text = yaml_p.read_text(errors="replace")
        for line in text.splitlines():
            if "model" in line and ":" in line and not line.strip().startswith("#"):
                # crude parse: model: value
                if line.strip().startswith("model:") or "model_name" in line or "model_id" in line:
                    model = line.split(":", 1)[1].strip().strip("\"'")
                    break
        # also search for known base string
        for b in ALLOWED_BASES:
            if b in text:
                model = b
                break
    ok = model in ALLOWED_BASES if model else False
    return {"model": model, "g6_single_allowed_variant": ok, "allowed": sorted(ALLOWED_BASES)}


def run_check(agent_dir: Path) -> dict:
    required = [agent_dir / "agent.yaml", agent_dir / "prompts"]
    missing = [str(r) for r in required if not r.exists()]
    files, violations, unpacked = collect_files(agent_dir)
    g6 = check_g6_model(agent_dir)
    report = {
        "check_only": True,
        "agent_dir": str(agent_dir),
        "g5_agent_only": True,
        "n_files": len(files),
        "unpacked_bytes": unpacked,
        "unpacked_gib": unpacked / (1024**3),
        "under_3gib": unpacked < MAX_UNPACKED,
        "violations": violations,
        "missing_required": missing,
        "g6": g6,
        "pass": (not missing)
        and (not violations)
        and unpacked < MAX_UNPACKED
        and bool(g6.get("g6_single_allowed_variant")),
    }
    return report


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent-dir", type=Path, default=Path("agent"))
    p.add_argument("--out", type=Path, default=Path("submission.zip"))
    p.add_argument("--check", action="store_true", help="read-only G5/G6 audit (no zip write)")
    args = p.parse_args()

    agent_dir = args.agent_dir.resolve()
    if not agent_dir.is_dir():
        print(f"missing agent dir: {agent_dir}", file=sys.stderr)
        return 2

    if args.check:
        report = run_check(agent_dir)
        print(json.dumps(report, indent=2))
        print(f"BUILD_SUBMISSION_CHECK_PASS={report['pass']}", flush=True)
        return 0 if report["pass"] else 2

    required = [agent_dir / "agent.yaml", agent_dir / "prompts"]
    for req in required:
        if not req.exists():
            print(f"missing required: {req}", file=sys.stderr)
            return 2

    files, violations, unpacked = collect_files(agent_dir)
    if violations:
        print("G5 violations:\n  " + "\n  ".join(violations[:20]), file=sys.stderr)
        return 2

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
