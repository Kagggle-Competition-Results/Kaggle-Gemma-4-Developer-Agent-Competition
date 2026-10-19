#!/usr/bin/env python3
"""P5.2 — Hard-filter BoN keepers (agent-dev-spec / roadmap).

Drop trajectories with spiral tool-arg loops (loop_pairs > 2), scratch pollution,
or peak prompt >28k. Within loop_pairs <= 2, rank and keep up to --max-keepers
per task by (loop ASC, tool_calls ASC, diff_size ASC) so loop==0 wins first while
loop in {1,2} true-oracle keepers are retained for task coverage / recovery SFT.

Dedupe by (task_id, patch_sha1) across multiple keep roots.
Never includes holdout IDs from configs/splits.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path


def _obs(step: dict) -> str:
    obs = step.get("observation") or {}
    if isinstance(obs, dict):
        return str(obs.get("content") or "")
    return str(obs or "")


def peak_prompt_tokens(trace: dict) -> int:
    peak = 0
    for step in trace.get("steps") or []:
        m = step.get("metrics") or {}
        peak = max(peak, int(m.get("prompt_tokens") or 0))
        for tc in step.get("tool_calls") or []:
            u = ((tc.get("extra") or {}).get("usage") or {})
            peak = max(peak, int(u.get("prompt_tokens") or 0))
    return peak


def loop_pairs(trace: dict) -> int:
    calls: list[tuple[str, str]] = []
    for step in trace.get("steps") or []:
        for tc in step.get("tool_calls") or []:
            name = tc.get("function_name") or ""
            args = tc.get("arguments") or {}
            calls.append((name, json.dumps(args, sort_keys=True)[:240]))
    n = 0
    for a, b in zip(calls, calls[1:]):
        if a == b and a[0]:
            n += 1
    return n


def n_tool_calls(trace: dict) -> int:
    n = 0
    for step in trace.get("steps") or []:
        n += len(step.get("tool_calls") or [])
    return n


def has_scratch(trace: dict, meta: dict) -> bool:
    blob = " ".join(
        [
            str(meta.get("error") or ""),
            str(meta.get("test_output_tail") or ""),
            json.dumps(trace.get("final_metrics") or {}),
        ]
    ).lower()
    if "scratch" in blob or "untracked" in blob:
        return True
    for step in trace.get("steps") or []:
        for tc in step.get("tool_calls") or []:
            args = tc.get("arguments") or {}
            cmd = str(args.get("command") or args.get("filepath") or "").lower()
            if "/scratch" in cmd or "scratch/" in cmd:
                return True
        cl = _obs(step).lower()
        if "scratch_pollution" in cl or "untracked files" in cl:
            return True
    return False


def load_patch(run_dir: Path) -> str:
    for name in ("patch.diff", "agent.patch", "patch.txt"):
        p = run_dir / name
        if p.is_file():
            return p.read_text(errors="replace")
    # some keeps only have meta; try meta.patch
    meta_p = run_dir / "meta.json"
    if meta_p.is_file():
        m = json.loads(meta_p.read_text())
        if m.get("patch"):
            return str(m["patch"])
    return ""


def find_trace(run_dir: Path) -> Path | None:
    hits = sorted((run_dir / "traces").glob("trace_*.json")) if (run_dir / "traces").is_dir() else []
    if hits:
        return hits[0]
    hits = sorted(run_dir.glob("trace_*.json"))
    return hits[0] if hits else None


def iter_keep_runs(root: Path) -> list[Path]:
    keep = root / "keep" if (root / "keep").is_dir() else root
    if not keep.is_dir():
        return []
    return sorted(p for p in keep.iterdir() if p.is_dir())


def main() -> int:
    ap = argparse.ArgumentParser(description="P5.2 filter + rank + cap BoN keepers")
    ap.add_argument(
        "--in",
        dest="inputs",
        action="append",
        type=Path,
        required=True,
        help="traces/bon dir (with keep/) or keep/ itself; repeatable",
    )
    ap.add_argument("--out", type=Path, required=True, help="clean keep output dir")
    ap.add_argument("--splits", type=Path, default=Path("configs/splits.json"))
    ap.add_argument("--max-prompt-tokens", type=int, default=28000)
    ap.add_argument(
        "--max-loop-pairs",
        type=int,
        default=2,
        help="hard-drop if consecutive tool-arg dups > this (P5.1c spiral = loop>2)",
    )
    ap.add_argument(
        "--max-keepers",
        type=int,
        default=2,
        help="max keepers per task after ranking (global SFT cap)",
    )
    ap.add_argument("--summary", type=Path, default=None)
    args = ap.parse_args()

    holdout: set[str] = set()
    if args.splits.is_file():
        sp = json.loads(args.splits.read_text())
        holdout = set(sp.get("holdout") or [])

    reasons: Counter[str] = Counter()
    candidates: list[dict] = []

    for root in args.inputs:
        for run_dir in iter_keep_runs(root):
            meta_p = run_dir / "meta.json"
            trace_p = find_trace(run_dir)
            if not meta_p.is_file() or trace_p is None:
                reasons["missing_meta_or_trace"] += 1
                continue
            meta = json.loads(meta_p.read_text())
            if not meta.get("resolved"):
                reasons["not_resolved"] += 1
                continue
            task_id = str(meta.get("task_id") or "")
            if task_id in holdout:
                reasons["holdout"] += 1
                continue
            trace = json.loads(trace_p.read_text())
            peak = peak_prompt_tokens(trace)
            if peak > args.max_prompt_tokens:
                reasons["peak_tokens"] += 1
                continue
            lp = loop_pairs(trace)
            if lp > args.max_loop_pairs:
                reasons["loop_spiral"] += 1
                continue
            if has_scratch(trace, meta):
                reasons["scratch"] += 1
                continue
            patch = load_patch(run_dir)
            patch_sha = hashlib.sha1(patch.encode("utf-8", errors="replace")).hexdigest() if patch else ""
            n_steps = len(trace.get("steps") or [])
            n_tools = n_tool_calls(trace)
            diff_size = len(patch)
            seconds = float(meta.get("seconds") or 0)
            candidates.append(
                {
                    "task_id": task_id,
                    "sample": meta.get("sample"),
                    "run_dir": run_dir,
                    "trace_p": trace_p,
                    "meta": meta,
                    "patch_sha": patch_sha or hashlib.sha1(trace_p.read_bytes()).hexdigest(),
                    "peak": peak,
                    "loop_pairs": lp,
                    "n_tool_calls": n_tools,
                    "diff_size": diff_size,
                    "n_steps": n_steps,
                    "seconds": seconds,
                    "source_root": str(root),
                }
            )
            reasons["pass_filters"] += 1

    # Dedupe: one per (task_id, patch_sha); prefer lower loop then tools then diff
    best: dict[tuple[str, str], dict] = {}
    for c in candidates:
        key = (c["task_id"], c["patch_sha"])
        prev = best.get(key)
        rank = (c["loop_pairs"], c["n_tool_calls"], c["diff_size"], c["n_steps"], c["seconds"])
        if prev is None or rank < (
            prev["loop_pairs"],
            prev["n_tool_calls"],
            prev["diff_size"],
            prev["n_steps"],
            prev["seconds"],
        ):
            best[key] = c
    reasons["dedupe_drop"] = len(candidates) - len(best)

    # Per-task rank + cap (--max-keepers)
    by_task: dict[str, list[dict]] = defaultdict(list)
    for c in best.values():
        by_task[c["task_id"]].append(c)

    selected: list[dict] = []
    cap_drop = 0
    for tid in sorted(by_task):
        ranked = sorted(
            by_task[tid],
            key=lambda x: (
                x["loop_pairs"],
                x["n_tool_calls"],
                x["diff_size"],
                x["n_steps"],
                x["seconds"],
                int(x.get("sample") or 0),
            ),
        )
        take = ranked[: max(0, int(args.max_keepers))]
        cap_drop += len(ranked) - len(take)
        selected.extend(take)
    reasons["cap_drop"] = cap_drop

    out_keep = args.out
    if out_keep.name != "keep":
        out_keep = args.out / "keep"
    if out_keep.exists():
        shutil.rmtree(out_keep)
    out_keep.mkdir(parents=True, exist_ok=True)

    kept_rows = []
    for c in sorted(selected, key=lambda x: (x["task_id"], x["sample"] or 0)):
        name = f"{c['task_id']}_n{int(c['sample'] or 0):02d}_{c['patch_sha'][:10]}"
        dest = out_keep / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(c["run_dir"], dest)
        kept_rows.append(
            {
                "name": name,
                "task_id": c["task_id"],
                "sample": c["sample"],
                "patch_sha": c["patch_sha"],
                "peak_prompt_tokens": c["peak"],
                "loop_pairs": c["loop_pairs"],
                "n_tool_calls": c["n_tool_calls"],
                "diff_size": c["diff_size"],
                "n_steps": c["n_steps"],
                "seconds": c["seconds"],
                "source_root": c["source_root"],
            }
        )

    by_task_out = Counter(r["task_id"] for r in kept_rows)
    loop_hist = Counter(r["loop_pairs"] for r in kept_rows)
    summary = {
        "n_in_pass_filters": reasons["pass_filters"],
        "n_out_unique": len(kept_rows),
        "n_tasks": len(by_task_out),
        "by_task": dict(sorted(by_task_out.items())),
        "loop_histogram": dict(sorted(loop_hist.items())),
        "drop_reasons": dict(reasons),
        "max_prompt_tokens": args.max_prompt_tokens,
        "max_loop_pairs": args.max_loop_pairs,
        "max_keepers": args.max_keepers,
        "rank_key": ["loop_pairs", "n_tool_calls", "diff_size", "n_steps", "seconds"],
        "out": str(out_keep),
    }
    summary_path = args.summary or (args.out / "filter_summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
