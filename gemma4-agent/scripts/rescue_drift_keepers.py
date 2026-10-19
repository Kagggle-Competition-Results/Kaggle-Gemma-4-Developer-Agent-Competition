#!/usr/bin/env python3
"""Rescue Wave1 assertion_fail rollouts that match A40 gold-host baseline drift.

Criteria (all required):
  1) fail_bucket == assertion_fail
  2) FAIL_TO_PASS / test_patch target tests have 0 failures
  3) every failing node id ⊆ a40_gold_baseline_failures.json[task_id]
  4) submitted patch is clean (no scratch files)
Cap: max 2 per task_id (prefer 1-file / minimal patch_chars / fewer tool calls).
Appends rendered rows to data_train/sft.jsonl.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_drift import (  # noqa: E402
    drift_immune_resolved,
    fail_bucket_from_meta,
    load_gold_baseline,
    patch_files,
)
from render_trajectories import render_run  # noqa: E402


def _tool_calls(run_dir: Path) -> int:
    trace = None
    if (run_dir / "traces").is_dir():
        hits = sorted((run_dir / "traces").glob("trace_*.json"))
        if hits:
            trace = json.loads(hits[0].read_text())
    if not trace:
        return 10**9
    n = 0
    for step in trace.get("steps") or []:
        n += len(step.get("tool_calls") or [])
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--raw-root",
        type=Path,
        required=True,
        help="Wave1 traces/bon/raw directory",
    )
    ap.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="a40_gold_baseline_failures.json",
    )
    ap.add_argument(
        "--tasks-jsonl",
        type=Path,
        required=True,
    )
    ap.add_argument(
        "--sft-out",
        type=Path,
        default=None,
    )
    ap.add_argument("--max-per-task", type=int, default=2)
    ap.add_argument(
        "--staging",
        type=Path,
        default=None,
        help="where to stage rescued keep dirs",
    )
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    sft_out = args.sft_out or (root / "data_train" / "sft.jsonl")
    staging = args.staging or (root / "results" / "p51a_r8_p314a" / "rescued" / "drift_keep")
    staging.mkdir(parents=True, exist_ok=True)

    baseline = load_gold_baseline(args.baseline)
    tasks = {}
    for ln in args.tasks_jsonl.read_text().splitlines():
        if not ln.strip():
            continue
        row = json.loads(ln)
        tasks[row["instance_id"]] = row

    candidates: dict[str, list[tuple[int, int, int, Path, dict]]] = {}
    for meta_p in sorted(args.raw_root.glob("*/n*/meta.json")):
        run_dir = meta_p.parent
        meta = json.loads(meta_p.read_text())
        tid = str(meta.get("task_id") or run_dir.parent.name)
        bucket = fail_bucket_from_meta(meta)
        if bucket != "assertion_fail":
            continue
        if tid not in baseline:
            continue
        test_output = str(meta.get("test_output") or meta.get("test_output_tail") or "")
        patch = str(meta.get("patch") or "")
        test_patch = str((tasks.get(tid) or {}).get("test_patch") or "")
        ok, debug = drift_immune_resolved(
            raw_resolved=False,
            test_output=test_output,
            agent_patch=patch,
            test_patch=test_patch,
            task_id=tid,
            gold_baseline=baseline,
        )
        if not ok:
            continue
        n_files = len(patch_files(patch))
        chars = int(meta.get("patch_chars") or len(patch))
        calls = _tool_calls(run_dir)
        candidates.setdefault(tid, []).append((n_files, chars, calls, run_dir, {**meta, "_drift": debug}))

    rescued_rows = []
    manifest = {"rescued": [], "skipped_existing": [], "by_task": {}}
    existing = []
    if sft_out.is_file():
        existing = [json.loads(l) for l in sft_out.read_text().splitlines() if l.strip()]
    existing_keys = {
        (r.get("task_id"), r.get("sample"))
        for r in existing
        if (r.get("meta") or {}).get("rescue")
    }
    # Count prior rescues per task so max-per-task is a hard total, not per-run.
    existing_rescue_count: dict[str, int] = {}
    for r in existing:
        if not (r.get("meta") or {}).get("rescue"):
            continue
        tid0 = str(r.get("task_id") or "")
        existing_rescue_count[tid0] = existing_rescue_count.get(tid0, 0) + 1

    for tid, items in sorted(candidates.items()):
        items.sort(key=lambda x: (x[0], x[1], x[2]))
        already = existing_rescue_count.get(tid, 0)
        slots = max(0, args.max_per_task - already)
        picked = items[:slots]
        manifest["by_task"][tid] = {
            "n_candidates": len(items),
            "already_rescued": already,
            "slots": slots,
            "picked": [p[3].name for p in picked],
        }
        for n_files, chars, calls, run_dir, meta in picked:
            sample = meta.get("sample") or int(run_dir.name.replace("n", "") or 0)
            key = (tid, sample)
            if key in existing_keys:
                manifest["skipped_existing"].append({"task_id": tid, "sample": sample})
                continue
            dest = staging / f"{tid}_n{int(sample):02d}"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(run_dir, dest)
            meta2 = dict(meta)
            meta2.pop("_drift", None)
            meta2["resolved"] = True
            meta2["resolved_raw_harness"] = False
            meta2["rescue"] = {
                "reason": "host_drift_match_gold_baseline",
                "rescued_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "n_files": n_files,
                "patch_chars": chars,
                "tool_calls": calls,
                "drift": meta.get("_drift"),
            }
            (dest / "meta.json").write_text(json.dumps(meta2, indent=2) + "\n")
            row = render_run(dest, mask_tool_results=True)
            if not row:
                continue
            row["meta"]["rescue"] = meta2["rescue"]
            row["meta"]["resolved"] = True
            row["meta"]["resolved_raw_harness"] = False
            rescued_rows.append(row)
            manifest["rescued"].append({"task_id": tid, "sample": sample, "dir": str(dest)})

    if rescued_rows:
        with sft_out.open("a", encoding="utf-8") as f:
            for row in rescued_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    all_rows = [json.loads(l) for l in sft_out.read_text().splitlines() if l.strip()] if sft_out.is_file() else []
    summary = {
        **manifest,
        "n_added": len(rescued_rows),
        "sft_rows": len(all_rows),
        "sft_tasks": len({r.get("task_id") for r in all_rows}),
        "baseline": str(args.baseline),
        "sft_out": str(sft_out),
    }
    man_path = staging.parent / "drift_rescue_manifest.json"
    man_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
