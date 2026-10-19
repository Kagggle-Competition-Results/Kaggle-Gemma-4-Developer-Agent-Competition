#!/usr/bin/env python3
"""Re-check quarantined requests/fastapi gold patches under srcpath+starlette.

For each task:
  1) gold patch via verify_task → must resolved=True
  2) empty agent_patch → must resolved=False (FAIL_TO_PASS / unpatched fails)

Writes a JSON report. Does not mutate splits/shards (caller does).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
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

DEFAULT_TASKS = (
    "requests_6589",
    "requests_6592",
    "requests_6629",
    "requests_6757",
    "fastapi_14077",
    "fastapi_14356",
    "fastapi_15745",
    "fastapi_15763",
    "fastapi_15785",
)


async def _verify(root: Path, tid: str, agent_patch: str) -> dict:
    from swegemma.config import EvalConfig
    from swegemma.harness.verification import verify_task
    from swegemma.models import load_tasks
    from swegemma.sandbox.subprocess import SubprocessManager

    data = root / "data"
    tasks = {t.instance_id: t for t in load_tasks(data / "tasks.jsonl")}
    task = tasks[tid]

    class Dummy:
        def get(self, *a, **k):
            raise KeyError

    out = Path(f"/tmp/qunlock_{tid}_{'gold' if agent_patch.strip() else 'base'}")
    if out.exists():
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
    snap = data / "snapshots" / f"{tid}.tgz"
    if not snap.is_file():
        return {
            "resolved": False,
            "error": f"missing_snapshot:{snap}",
            "test_exit_code": None,
            "seconds": 0.0,
        }
    t0 = time.perf_counter()
    res = await verify_task(
        task=task,
        config=cfg,
        docker=mgr,
        snapshot_path=snap,
        agent_patch=agent_patch,
        start_time=t0,
    )
    return {
        "resolved": bool(getattr(res, "resolved", False)),
        "test_exit_code": getattr(res, "test_exit_code", None),
        "error": str(getattr(res, "error", "") or "")[:400],
        "seconds": round(time.perf_counter() - t0, 1),
    }


async def check_one(root: Path, tid: str, gold: str) -> dict:
    print(f"QUNLOCK {tid} gold...", flush=True)
    gold_res = await _verify(root, tid, gold)
    print(
        f"QUNLOCK {tid} gold resolved={gold_res['resolved']} {gold_res.get('error','')[:80]}",
        flush=True,
    )
    print(f"QUNLOCK {tid} base...", flush=True)
    base_res = await _verify(root, tid, "")
    print(
        f"QUNLOCK {tid} base resolved={base_res['resolved']} {base_res.get('error','')[:80]}",
        flush=True,
    )
    unlock = bool(gold_res["resolved"]) and (not bool(base_res["resolved"]))
    return {
        "task_id": tid,
        "gold": gold_res,
        "base": base_res,
        "unlock": unlock,
        "layout_hint": "src" if tid.startswith("requests_") else "flat_fastapi",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("/workspace/gemma4-p51"))
    ap.add_argument("--tasks", nargs="*", default=list(DEFAULT_TASKS))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    root: Path = args.root
    data = root / "data"

    gold_by: dict[str, str] = {}
    for ln in (data / "tasks.jsonl").read_text().splitlines():
        if not ln.strip():
            continue
        row = json.loads(ln)
        if row["instance_id"] in args.tasks:
            gold_by[row["instance_id"]] = row.get("patch") or ""

    async def run() -> dict:
        results = []
        for tid in args.tasks:
            if tid not in gold_by:
                results.append(
                    {
                        "task_id": tid,
                        "unlock": False,
                        "error": "missing_task_row",
                    }
                )
                continue
            results.append(await check_one(root, tid, gold_by[tid]))
        unlocked = [r["task_id"] for r in results if r.get("unlock")]
        return {
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_tasks": len(args.tasks),
            "n_unlock": len(unlocked),
            "unlocked": unlocked,
            "results": results,
        }

    report = asyncio.run(run())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("n_tasks", "n_unlock", "unlocked")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
