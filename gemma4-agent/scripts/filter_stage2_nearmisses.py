#!/usr/bin/env python3
"""Filter Wave2 Stage-1 (N=4) outputs into Stage-2 near-miss task list.

Keeps task_ids where keepers==0 AND at least one edit was made
(assertion_fail or syntax_apply). Writes wave2_stage2_nearmiss.txt for N=12 @0.75.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _bucket(meta: dict) -> str:
    if meta.get("resolved"):
        return "keeper"
    err = str(meta.get("error") or "")
    tail = str(meta.get("test_output_tail") or "")
    el = err.lower()
    patched = bool(meta.get("patch") or (meta.get("patch_chars") or 0) > 0)
    if "timeout" in el or "max_time" in el or "timed out" in el:
        return "timeout"
    if "test_patch" in el or "harness" in el or "ModuleNotFound" in err:
        return "harness_err"
    if (
        "old_string" in el
        or "syntax" in el
        or "IndentationError" in tail
        or "SyntaxError" in tail
    ):
        return "syntax_apply"
    if not patched:
        return "no_edit"
    return "assertion_fail"


def _iter_metas(roots: list[Path]):
    for root in roots:
        if not root.exists():
            continue
        # Accept either traces/bon/raw/<tid>/n*/meta.json or raw/<tid>/n*/meta.json
        patterns = (
            "**/traces/bon/raw/*/n*/meta.json",
            "**/bon/raw/*/n*/meta.json",
            "**/raw/*/n*/meta.json",
            "*/n*/meta.json",
        )
        seen: set[Path] = set()
        for pat in patterns:
            for path in root.glob(pat):
                rp = path.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                yield path


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Wave2 Stage-2 near-miss task list")
    ap.add_argument(
        "stage1_roots",
        nargs="+",
        type=Path,
        help="one or more Stage-1 result roots (shard dirs or Bon out dirs)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("configs/wave2_shards/wave2_stage2_nearmiss.txt"),
    )
    ap.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="optional JSON summary path (default: <out>.summary.json)",
    )
    args = ap.parse_args()

    by_task: dict[str, list[dict]] = defaultdict(list)
    for meta_path in _iter_metas(args.stage1_roots):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:  # noqa: BLE001
            continue
        tid = meta.get("task_id") or meta_path.parts[-3]
        meta = dict(meta)
        meta["_bucket"] = _bucket(meta)
        meta["_path"] = str(meta_path)
        by_task[str(tid)].append(meta)

    selected: list[str] = []
    detail = []
    for tid in sorted(by_task):
        rows = by_task[tid]
        keepers = sum(1 for r in rows if r.get("resolved") or r["_bucket"] == "keeper")
        edits = sum(1 for r in rows if r["_bucket"] in ("assertion_fail", "syntax_apply"))
        buckets: dict[str, int] = defaultdict(int)
        for r in rows:
            buckets[r["_bucket"]] += 1
        rec = {
            "task_id": tid,
            "n_metas": len(rows),
            "keepers": keepers,
            "edits_made": edits,
            "buckets": dict(buckets),
            "selected": keepers == 0 and edits >= 1,
        }
        detail.append(rec)
        if rec["selected"]:
            selected.append(tid)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(selected) + ("\n" if selected else ""))
    summary_path = args.summary or args.out.with_suffix(".summary.json")
    summary = {
        "n_tasks_seen": len(by_task),
        "n_selected": len(selected),
        "out": str(args.out),
        "rule": "keepers==0 AND edits_made(assertion_fail|syntax_apply)>=1",
        "stage2": {"n": 12, "temp": 0.75},
        "tasks": detail,
        "selected_tasks": selected,
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                "n_tasks_seen": summary["n_tasks_seen"],
                "n_selected": summary["n_selected"],
                "out": summary["out"],
                "summary": str(summary_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
