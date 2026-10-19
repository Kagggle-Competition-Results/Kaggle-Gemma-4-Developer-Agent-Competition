#!/usr/bin/env python3
"""Re-verify completed BoN rollouts with PYTHONPATH=src (true oracle).

Does not modify SubprocessManager; applies gold-style verify: agent_patch +
test_patch, pytest with PYTHONPATH=/workspace/src:/workspace.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_drift import parse_failed_node_ids, patch_is_clean, target_tests_failed
from render_trajectories import render_run
from swegemma.harness import container_setup as cs
from swegemma.harness.container_setup import apply_patch_in_container
from swegemma.models import extract_test_files_from_patch
from swegemma.sandbox.subprocess import SubprocessManager


def _task_row(tasks_jsonl: Path, tid: str) -> dict:
    for ln in tasks_jsonl.read_text().splitlines():
        if not ln.strip():
            continue
        row = json.loads(ln)
        if row["instance_id"] == tid:
            return row
    raise KeyError(tid)


async def verify_patch(
    *,
    data: Path,
    tid: str,
    agent_patch: str,
    test_patch: str,
) -> dict:
    mgr = SubprocessManager(timeout_seconds=300, system_site_packages=True)
    sid = await asyncio.to_thread(mgr.start)
    try:
        await asyncio.to_thread(
            cs.extract_snapshot, mgr, sid, data / "snapshots" / f"{tid}.tgz"
        )
        for patch in (agent_patch, test_patch):
            if not (patch or "").strip():
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
                f.write(patch if patch.endswith("\n") else patch + "\n")
                hp = Path(f.name)
            await asyncio.to_thread(mgr.copy_to, sid, hp, "/tmp/")
            await asyncio.to_thread(
                apply_patch_in_container, mgr, sid, f"/tmp/{hp.name}"
            )
            hp.unlink(missing_ok=True)
        files = [
            p
            for p in (extract_test_files_from_patch(test_patch) or ["tests"])
            if not str(p).endswith("conftest.py")
        ]
        files_str = " ".join(files) if files else "tests"
        cmd = (
            "cd /workspace && PYTHONPATH=/workspace/src:/workspace "
            "PYTHONNOUSERSITE=1 python3 -m pytest "
            f"{files_str} -q --tb=no 2>&1 | tail -n 60"
        )
        rr = await asyncio.to_thread(mgr.exec, sid, cmd, timeout=240)
        out = (rr.stdout or "") + "\n" + (rr.stderr or "")
        failed = parse_failed_node_ids(out)
        ftp_fail = target_tests_failed(failed, test_patch)
        has_fail_count = bool(re.search(r"(?m)^\d+ failed", out))
        has_err_count = bool(re.search(r"(?m)^\d+ error", out))
        has_passed = bool(re.search(r"\d+ passed", out))
        passed = (
            len(failed) == 0
            and not has_fail_count
            and not has_err_count
            and has_passed
        )
        return {
            "resolved": bool(passed)
            and not ftp_fail
            and patch_is_clean(agent_patch),
            "n_failed": len(failed),
            "failed": failed,
            "ftp_fail": ftp_fail,
            "clean_patch": patch_is_clean(agent_patch),
            "tail": out[-1200:],
            "exit": rr.exit_code,
        }
    finally:
        await asyncio.to_thread(mgr.stop, sid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("/workspace/gemma4-p51"))
    ap.add_argument("--raw-root", type=Path, required=True)
    ap.add_argument("--tasks", nargs="+", required=True)
    ap.add_argument("--max-keepers", type=int, default=2)
    ap.add_argument("--sft-out", type=Path, default=None)
    ap.add_argument(
        "--staging",
        type=Path,
        default=None,
        help="where to copy true keepers",
    )
    args = ap.parse_args()
    root = args.root
    data = root / "data"
    sft_out = args.sft_out or (root / "data_train" / "sft.jsonl")
    # also allow local path
    if not sft_out.is_file() and (Path("data_train") / "sft.jsonl").is_file():
        sft_out = Path("data_train") / "sft.jsonl"
    staging = args.staging or (
        root / "results" / "p51a_r8_p314a" / "rescued" / "srcpath_keep"
    )
    staging.mkdir(parents=True, exist_ok=True)

    existing = []
    if sft_out.is_file():
        existing = [
            json.loads(l) for l in sft_out.read_text().splitlines() if l.strip()
        ]
    existing_keys = {(r.get("task_id"), r.get("sample")) for r in existing}
    existing_count: dict[str, int] = {}
    for r in existing:
        tid = str(r.get("task_id") or "")
        # count true keepers for these tasks
        if tid in args.tasks:
            existing_count[tid] = existing_count.get(tid, 0) + 1

    report: dict = {
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tasks": {},
        "added": [],
    }

    async def run_all() -> None:
        for tid in args.tasks:
            row = _task_row(data / "tasks.jsonl", tid)
            test_patch = row.get("test_patch") or ""
            task_dir = args.raw_root / tid
            metas = sorted(task_dir.glob("n*/meta.json"))
            candidates: list[tuple[int, int, Path, dict, dict]] = []
            print(f"REVERIFY_TASK {tid} n_metas={len(metas)}", flush=True)
            for meta_p in metas:
                meta = json.loads(meta_p.read_text())
                patch = meta.get("patch") or ""
                if not patch.strip():
                    continue
                if not patch_is_clean(patch):
                    print(f"  skip dirty {meta_p.parent.name}", flush=True)
                    continue
                sample = int(meta.get("sample") or meta_p.parent.name.replace("n", ""))
                print(f"  verify {meta_p.parent.name} ...", flush=True)
                res = await verify_patch(
                    data=data,
                    tid=tid,
                    agent_patch=patch,
                    test_patch=test_patch,
                )
                print(
                    f"  done {meta_p.parent.name} resolved={res['resolved']} "
                    f"n_failed={res['n_failed']}",
                    flush=True,
                )
                (meta_p.parent / "reverify_srcpath.json").write_text(
                    json.dumps(res, indent=2) + "\n"
                )
                if res["resolved"]:
                    chars = len(patch)
                    n_files = patch.count("diff --git ")
                    candidates.append(
                        (n_files or 99, chars, meta_p.parent, meta, res)
                    )
            candidates.sort(key=lambda x: (x[0], x[1]))
            already = sum(
                1
                for r in existing
                if r.get("task_id") == tid
                and (r.get("meta") or {}).get("oracle_srcpath")
            )
            # also count any existing rows for tid toward cap if already keepers
            slots = max(0, args.max_keepers - already)
            # Prefer not exceeding max_keepers total oracle_srcpath for task
            picked = candidates[:slots]
            report["tasks"][tid] = {
                "n_metas": len(metas),
                "n_passed": len(candidates),
                "picked": [p[2].name for p in picked],
                "already_oracle_srcpath": already,
            }
            for n_files, chars, run_dir, meta, res in picked:
                sample = int(meta.get("sample") or run_dir.name.replace("n", ""))
                if (tid, sample) in existing_keys:
                    # update in place? skip duplicate
                    continue
                dest = staging / f"{tid}_n{sample:02d}"
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(run_dir, dest)
                meta2 = dict(meta)
                meta2["resolved"] = True
                meta2["resolved_raw_harness"] = bool(meta.get("resolved"))
                meta2["oracle_srcpath"] = {
                    "reason": "reverify_PYTHONPATH_src",
                    "verified_utc": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "n_failed": 0,
                    "n_files": n_files,
                    "patch_chars": chars,
                }
                (dest / "meta.json").write_text(json.dumps(meta2, indent=2) + "\n")
                srow = render_run(dest, mask_tool_results=True)
                if not srow:
                    continue
                srow["meta"]["resolved"] = True
                srow["meta"]["oracle_srcpath"] = meta2["oracle_srcpath"]
                with sft_out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(srow, ensure_ascii=False) + "\n")
                existing_keys.add((tid, sample))
                report["added"].append({"task_id": tid, "sample": sample})

    asyncio.run(run_all())
    all_rows = (
        [json.loads(l) for l in sft_out.read_text().splitlines() if l.strip()]
        if sft_out.is_file()
        else []
    )
    report["sft_rows"] = len(all_rows)
    report["sft_tasks"] = len({r.get("task_id") for r in all_rows})
    out_p = staging.parent / "reverify_srcpath_manifest.json"
    out_p.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
