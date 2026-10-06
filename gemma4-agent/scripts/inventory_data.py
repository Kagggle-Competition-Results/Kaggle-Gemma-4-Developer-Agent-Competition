#!/usr/bin/env python3
"""P0.4 — Inventory local data/ after download: snapshots, graphs, embeds, zero-byte flags."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--out", type=Path, default=Path("data/_meta/local_inventory.json"))
    args = p.parse_args()

    data = args.data
    if not data.is_dir():
        print(f"missing data dir: {data}", file=sys.stderr)
        return 2

    snaps = sorted((data / "snapshots").glob("*.tgz")) if (data / "snapshots").is_dir() else []
    graphs = sorted((data / "graphs").glob("*.json")) if (data / "graphs").is_dir() else []
    embs = sorted((data / "embeddings").glob("*.npz")) if (data / "embeddings").is_dir() else []
    tasks = data / "tasks.jsonl"

    zero_byte = []
    for path in snaps + graphs + embs:
        if path.stat().st_size == 0:
            zero_byte.append(str(path.relative_to(data)))

    report = {
        "tasks_jsonl_present": tasks.is_file(),
        "n_snapshots": len(snaps),
        "n_graphs": len(graphs),
        "n_embeddings": len(embs),
        "zero_byte_files": zero_byte,
        "integrity_ok": (
            tasks.is_file()
            and len(snaps) == 129
            and len(zero_byte) == 0
        ),
        "note": "Graph/embed stems use commit hashes; join via tasks.jsonl.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["n_snapshots"] or report["tasks_jsonl_present"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
