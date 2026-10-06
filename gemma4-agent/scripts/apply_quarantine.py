#!/usr/bin/env python3
"""P1.2 — Move unusable soundness tasks into splits.json quarantine.

Removes quarantined IDs from smoke/dev/holdout/train_pool; keeps holdout
membership notes via quarantine list (IDs must not be scored).
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path


SPLIT_KEYS = ("smoke", "dev", "holdout", "train_pool")


def load_unusable(csv_path: Path) -> list[str]:
    bad: list[str] = []
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            usable = str(row.get("usable", "")).lower() == "true"
            tid = (row.get("task_id") or "").strip()
            if tid and not usable:
                bad.append(tid)
    return sorted(set(bad))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--soundness", type=Path, required=True)
    p.add_argument("--splits", type=Path, default=Path("configs/splits.json"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    splits = json.loads(args.splits.read_text())
    unusable = load_unusable(args.soundness)
    prev_q = set(splits.get("quarantine") or [])
    quarantine = sorted(prev_q | set(unusable))

    removed: dict[str, list[str]] = {}
    for key in SPLIT_KEYS:
        before = list(splits.get(key) or [])
        after = [t for t in before if t not in quarantine]
        removed[key] = sorted(set(before) - set(after))
        splits[key] = after

    splits["quarantine"] = quarantine
    splits["quarantine_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    splits["quarantine_source"] = str(args.soundness)
    notes = splits.get("notes") or ""
    tag = "Unsound tasks quarantined from P1.1 soundness.csv."
    if tag not in notes:
        splits["notes"] = (notes + " " + tag).strip()

    usable_retained = 0
    with args.soundness.open(newline="") as f:
        for row in csv.DictReader(f):
            if str(row.get("usable", "")).lower() == "true":
                usable_retained += 1

    print(
        f"quarantine={len(quarantine)} (+{len(set(unusable) - prev_q)} new) "
        f"usable_retained={usable_retained} "
        f"removed={{ {', '.join(f'{k}:{len(v)}' for k, v in removed.items())} }}"
    )
    for key, ids in removed.items():
        if ids:
            print(f"  {key} removed: {', '.join(ids[:12])}{'...' if len(ids) > 12 else ''}")

    if args.dry_run:
        print("dry-run: not writing", args.splits)
        return 0

    args.splits.write_text(json.dumps(splits, indent=2) + "\n")
    print(f"wrote {args.splits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
