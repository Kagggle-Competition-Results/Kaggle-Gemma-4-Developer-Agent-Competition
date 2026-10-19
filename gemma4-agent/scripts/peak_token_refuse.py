#!/usr/bin/env python3
"""Scan traces for peak token usage and vLLM/context refuse signals."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REFUSE_RE = re.compile(
    r"context.?window|maximum context|max_model_len|refuse|token.?limit|ContextWindow",
    re.I,
)


def scan_trace(path: Path) -> dict:
    t = json.loads(path.read_text())
    tid = t.get("task_id") or path.stem.replace("trace_", "")
    fm = t.get("final_metrics") or {}
    peaks = []
    refuse_hits = 0
    refuse_samples = []
    for step in t.get("steps") or []:
        for k in ("prompt_tokens", "total_tokens", "input_tokens"):
            if isinstance(step.get(k), (int, float)):
                peaks.append(int(step[k]))
        obs = step.get("observation") or {}
        content = obs.get("content") if isinstance(obs, dict) else obs
        text = str(content or "")
        if REFUSE_RE.search(text):
            refuse_hits += 1
            if len(refuse_samples) < 3:
                refuse_samples.append(text[:160])
        # also check step errors
        err = str(step.get("error") or "")
        if REFUSE_RE.search(err):
            refuse_hits += 1
    # metrics aggregate
    for k in ("total_prompt_tokens", "total_tokens", "peak_prompt_tokens"):
        if isinstance(fm.get(k), (int, float)):
            peaks.append(int(fm[k]))
    return {
        "task_id": tid,
        "peak_tokens_proxy": max(peaks) if peaks else None,
        "total_prompt_tokens": fm.get("total_prompt_tokens"),
        "total_tokens": fm.get("total_tokens"),
        "total_steps": fm.get("total_steps"),
        "refuse_hits": refuse_hits,
        "refuse_samples": refuse_samples,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--traces", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--label", default="")
    args = p.parse_args()
    rows = [scan_trace(tp) for tp in sorted(args.traces.glob("trace_*.json"))]
    peaks = [r["peak_tokens_proxy"] for r in rows if r["peak_tokens_proxy"] is not None]
    out = {
        "label": args.label or args.traces.name,
        "n": len(rows),
        "n_with_token_metrics": len(peaks),
        "peak_tokens_max": max(peaks) if peaks else None,
        "peak_tokens_mean": round(sum(peaks) / len(peaks), 1) if peaks else None,
        "refuse_tasks": sum(1 for r in rows if r["refuse_hits"] > 0),
        "refuse_rate": round(sum(1 for r in rows if r["refuse_hits"] > 0) / len(rows), 4)
        if rows
        else 0,
        "tasks": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in out if k != "tasks"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
