#!/usr/bin/env python3
"""Hard gate: gold patch must resolve=True for all Wave1 tasks via verify_task."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from patch_swegemma_subprocess import apply_patches

    apply_patches(python=None, dry_run=False)
except Exception as e:  # noqa: BLE001
    print("WARN patch", e, flush=True)


async def verify_one(root: Path, tid: str) -> dict:
    from swegemma.config import EvalConfig
    from swegemma.harness.verification import verify_task
    from swegemma.models import load_tasks
    from swegemma.sandbox.subprocess import SubprocessManager

    data = root / "data"
    tasks = {t.instance_id: t for t in load_tasks(data / "tasks.jsonl")}
    task = tasks[tid]
    gold = ""
    for ln in (data / "tasks.jsonl").read_text().splitlines():
        row = json.loads(ln)
        if row["instance_id"] == tid:
            gold = row.get("patch") or ""
            break

    class Dummy:
        def get(self, *a, **k):
            raise KeyError

    out = Path(f"/tmp/gold_gate_{tid}")
    if out.exists():
        import shutil

        shutil.rmtree(out)
    out.mkdir(parents=True)
    cfg = EvalConfig(
        tasks_path=data / "tasks.jsonl",
        snapshots_dir=data / "snapshots",
        results_dir=out,
        submission_dir=root / "agent",
        models=Dummy(),
        sandbox="subprocess",
        timeout_seconds=300,
        max_time_minutes=8,
        max_tool_calls=28,
        wheels_dir=data / "wheels",
    )
    mgr = SubprocessManager(timeout_seconds=300, system_site_packages=True)
    t0 = time.perf_counter()
    res = await verify_task(
        task=task,
        config=cfg,
        docker=mgr,
        snapshot_path=data / "snapshots" / f"{tid}.tgz",
        agent_patch=gold,
        start_time=t0,
    )
    return {
        "task_id": tid,
        "resolved": bool(getattr(res, "resolved", False)),
        "test_exit_code": getattr(res, "test_exit_code", None),
        "error": str(getattr(res, "error", "") or "")[:300],
        "seconds": round(time.perf_counter() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("/workspace/gemma4-p51"))
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    tids = [
        ln.strip()
        for ln in args.tasks.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]

    async def run() -> dict:
        results = []
        for tid in tids:
            print(f"GOLD_GATE {tid}", flush=True)
            try:
                rec = await verify_one(args.root, tid)
            except Exception as e:  # noqa: BLE001
                rec = {"task_id": tid, "resolved": False, "error": str(e)[:300]}
            print(
                f"GOLD_GATE_DONE {tid} resolved={rec.get('resolved')}",
                flush=True,
            )
            results.append(rec)
        n_ok = sum(1 for r in results if r.get("resolved"))
        return {
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_tasks": len(tids),
            "n_pass": n_ok,
            "all_pass": n_ok == len(tids),
            "results": results,
        }

    report = asyncio.run(run())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
