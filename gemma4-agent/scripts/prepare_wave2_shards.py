#!/usr/bin/env python3
"""Build Wave 2 Stage-1 task shards for multi-pod N=4 screening.

Excludes SFT tasks, P5.1a-r8 Wave1 tasks, holdout/quarantine, and known-broken
harness tasks. Writes up to 160 clean candidates into 4 shard files (or fewer
if the allowed pool is smaller — reported in the meta JSON).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


BROKEN_DEFAULT = (
    "fastapi_14953",
    "fastapi_14492",
    "rich_4076",
)


def _load_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return {str(x) for x in data}
        if isinstance(data, dict):
            for key in ("tasks_wave1", "tasks", "task_ids"):
                if key in data:
                    return {str(x) for x in data[key]}
        return set()
    return {
        ln.strip()
        for ln in path.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare Wave2 Stage-1 shard task lists")
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="gemma4-agent root",
    )
    ap.add_argument("--n-shards", type=int, default=4)
    ap.add_argument("--target-tasks", type=int, default=160)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="default: <root>/configs/wave2_shards/",
    )
    args = ap.parse_args()
    root: Path = args.root
    out_dir = args.out_dir or (root / "configs" / "wave2_shards")
    out_dir.mkdir(parents=True, exist_ok=True)

    splits = json.loads((root / "configs" / "splits.json").read_text())
    holdout = set(splits.get("holdout") or [])
    quarantine = set(splits.get("quarantine") or [])

    sft_tasks = {
        json.loads(ln)["task_id"]
        for ln in (root / "data_train" / "sft.jsonl").read_text().splitlines()
        if ln.strip()
    }
    wave1 = _load_ids(root / "configs" / "p51a_r8_p314a_wave1.txt")
    wave1 |= _load_ids(root / "configs" / "p51a_r8_p314a_tasks_meta.json")
    broken = set(BROKEN_DEFAULT)

    all_ids: list[str] = []
    for ln in (root / "data" / "tasks.jsonl").read_text().splitlines():
        if not ln.strip():
            continue
        all_ids.append(json.loads(ln)["instance_id"])

    excl = sft_tasks | wave1 | broken | holdout | quarantine
    clean = sorted(set(all_ids) - excl)
    selected = clean[: args.target_tasks]

    n_shards = max(1, int(args.n_shards))
    shards: list[list[str]] = [[] for _ in range(n_shards)]
    for i, tid in enumerate(selected):
        shards[i % n_shards].append(tid)

    shard_paths = []
    for i, tasks in enumerate(shards):
        path = out_dir / f"wave2_shard_{i}.txt"
        path.write_text("\n".join(tasks) + ("\n" if tasks else ""))
        shard_paths.append({"path": str(path.relative_to(root)), "n_tasks": len(tasks)})

    meta = {
        "ticket": "P5.1a-r8_wave2_prep",
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_tasks": args.target_tasks,
        "available_clean": len(clean),
        "selected": len(selected),
        "shortfall": max(0, args.target_tasks - len(selected)),
        "n_shards": n_shards,
        "stage1": {"n": 4, "temp": 0.4, "concurrency": 2},
        "exclusions": {
            "sft_tasks": sorted(sft_tasks),
            "wave1_tasks": sorted(wave1),
            "broken": sorted(broken),
            "holdout_n": len(holdout),
            "quarantine_n": len(quarantine),
        },
        "note": (
            "Competition tasks.jsonl has 129 IDs; after G4 holdout/quarantine + SFT + "
            "Wave1 + broken, clean pool is <<160. Shards use all available clean tasks."
        ),
        "shards": shard_paths,
        "selected_tasks": selected,
    }
    meta_path = out_dir / "wave2_shards_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps({k: meta[k] for k in meta if k != "selected_tasks"}, indent=2))
    print(f"wrote {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
