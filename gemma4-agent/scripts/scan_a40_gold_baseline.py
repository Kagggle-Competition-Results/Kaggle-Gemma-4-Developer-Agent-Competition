#!/usr/bin/env python3
"""Scan Wave1 tasks: apply gold patch on current host and record failing node ids.

Intended to run with `nice -n 19` on the harvest pod without touching BON/vLLM.
Writes results/p51a_r8_p314a/a40_gold_baseline_failures.json (or --out).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_drift import parse_failed_node_ids  # noqa: E402


async def verify_gold(root: Path, task_id: str) -> dict:
    from swegemma.config import EvalConfig
    from swegemma.harness.verification import verify_task
    from swegemma.models import load_tasks
    from swegemma.sandbox.subprocess import SubprocessManager

    data = root / "data"
    tasks = {t.instance_id: t for t in load_tasks(data / "tasks.jsonl")}
    task = tasks[task_id]
    gold = getattr(task, "patch", None) or ""
    if not gold and isinstance(task, dict):
        gold = task.get("patch") or ""
    # load_tasks returns objects; patch attr should exist
    if not gold:
        # fallback from jsonl
        for ln in (data / "tasks.jsonl").read_text().splitlines():
            row = json.loads(ln)
            if row["instance_id"] == task_id:
                gold = row.get("patch") or ""
                break

    class Dummy:
        def get(self, *a, **k):
            raise KeyError("no model")

    out = Path(f"/tmp/a40_gold_baseline_{task_id}")
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
    snap = data / "snapshots" / f"{task_id}.tgz"
    t0 = time.perf_counter()
    res = await verify_task(
        task=task,
        config=cfg,
        docker=mgr,
        snapshot_path=snap,
        agent_patch=gold,
        start_time=t0,
    )
    tout = str(getattr(res, "test_output", "") or "")
    err = str(getattr(res, "error", "") or "")
    blob = tout or err
    failed = parse_failed_node_ids(blob)
    return {
        "task_id": task_id,
        "resolved": bool(getattr(res, "resolved", False)),
        "test_exit_code": getattr(res, "test_exit_code", None),
        "failing_tests": failed,
        "seconds": round(time.perf_counter() - t0, 1),
        "error_head": err[:200],
        "n_failed": len(failed),
        "test_output_tail": blob[-4000:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("/workspace/gemma4-p51"))
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    task_ids = [
        ln.strip()
        for ln in args.tasks.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]

    mapping: dict = {
        "_meta": {
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": "a40_subprocess",
            "python": None,
            "n_tasks": len(task_ids),
        }
    }
    try:
        import sys as _sys

        mapping["_meta"]["python"] = _sys.version.split()[0]
    except Exception:
        pass

    args.out.parent.mkdir(parents=True, exist_ok=True)

    async def run_all():
        for tid in task_ids:
            print(f"GOLD_SCAN_START {tid}", flush=True)
            try:
                rec = await verify_gold(args.root, tid)
            except Exception as e:  # noqa: BLE001
                rec = {
                    "task_id": tid,
                    "resolved": False,
                    "failing_tests": [],
                    "error_head": str(e)[:300],
                    "scan_error": True,
                }
            mapping[tid] = rec["failing_tests"]
            mapping.setdefault("_details", {})[tid] = rec
            args.out.write_text(json.dumps(mapping, indent=2) + "\n")
            print(
                f"GOLD_SCAN_DONE {tid} resolved={rec.get('resolved')} "
                f"n_failed={len(rec.get('failing_tests') or [])}",
                flush=True,
            )

    asyncio.run(run_all())
    print(json.dumps({"out": str(args.out), "n": len(task_ids)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
