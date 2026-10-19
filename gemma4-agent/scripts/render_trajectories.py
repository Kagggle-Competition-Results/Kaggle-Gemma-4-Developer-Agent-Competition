#!/usr/bin/env python3
"""P5.5 — Render BoN/STaR keep traces to SFT JSONL (agent-dev-spec §5).

CLI:
  python scripts/render_trajectories.py --in traces/... --out data_train/sft.jsonl --mask-tool-results

Each line: {"task_id","sample","messages":[...],"meta":{...}}
Assistant turns are tool-call JSON (no invented chat template).
With --mask-tool-results, tool observation bodies become a short placeholder.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MASK = "[TOOL_RESULT_MASKED]"


def _find_trace(run_dir: Path) -> Path | None:
    if (run_dir / "traces").is_dir():
        hits = sorted((run_dir / "traces").glob("trace_*.json"))
        if hits:
            return hits[0]
    hits = sorted(run_dir.glob("trace_*.json"))
    return hits[0] if hits else None


def _iter_runs(inp: Path) -> list[Path]:
    keep = inp / "keep" if (inp / "keep").is_dir() else inp
    if keep.is_dir() and any(p.is_dir() for p in keep.iterdir()):
        return sorted(p for p in keep.iterdir() if p.is_dir())
    if _find_trace(inp):
        return [inp]
    return []


def _assistant_content(step: dict) -> str:
    """Serialize model action: prefer tool_calls; else message text."""
    msg = (step.get("message") or "").strip()
    tcs = step.get("tool_calls") or []
    if tcs:
        slim = []
        for tc in tcs:
            slim.append(
                {
                    "name": tc.get("function_name") or tc.get("name") or "",
                    "arguments": tc.get("arguments") or {},
                }
            )
        payload = {"tool_calls": slim}
        if msg:
            payload["text"] = msg
        return json.dumps(payload, ensure_ascii=False)
    return msg


def _tool_content(step: dict, mask: bool) -> str:
    obs = step.get("observation") or {}
    raw = obs.get("content") if isinstance(obs, dict) else obs
    if mask:
        tool = ""
        if isinstance(obs, dict):
            tool = str((obs.get("extra") or {}).get("tool_name") or "")
        tcs = step.get("tool_calls") or []
        if not tool and tcs:
            tool = str(tcs[0].get("function_name") or "")
        n = len(str(raw or ""))
        return json.dumps({"tool": tool or "unknown", "status": "masked", "chars": n})
    return str(raw or "")


def render_run(run_dir: Path, mask_tool_results: bool) -> dict | None:
    meta_p = run_dir / "meta.json"
    trace_p = _find_trace(run_dir)
    if trace_p is None:
        return None
    trace = json.loads(trace_p.read_text())
    meta = json.loads(meta_p.read_text()) if meta_p.is_file() else {}
    if meta.get("resolved") is False:
        return None
    messages: list[dict] = []
    for step in trace.get("steps") or []:
        src = (step.get("source") or "").lower()
        if src in ("system", "user"):
            content = (step.get("message") or "").strip()
            if content:
                messages.append({"role": src, "content": content})
            continue
        if src != "agent":
            continue
        asst = _assistant_content(step)
        if asst:
            messages.append({"role": "assistant", "content": asst})
        if step.get("observation") is not None or (step.get("tool_calls") or []):
            messages.append(
                {
                    "role": "tool",
                    "content": _tool_content(step, mask=mask_tool_results),
                }
            )
    if len(messages) < 2:
        return None
    task_id = str(meta.get("task_id") or trace.get("task_id") or run_dir.name)
    return {
        "task_id": task_id,
        "sample": meta.get("sample"),
        "messages": messages,
        "meta": {
            "resolved": bool(meta.get("resolved", True)),
            "n_steps": len(trace.get("steps") or []),
            "n_messages": len(messages),
            "seconds": meta.get("seconds"),
            "temperature": meta.get("temperature"),
            "source": meta.get("source"),
            "license": meta.get("license"),
            "feedback_injected": meta.get("feedback_injected"),
            "sample": meta.get("sample"),
            "attempt": meta.get("attempt"),
            "source_dir": str(run_dir),
            "mask_tool_results": mask_tool_results,
            "final_metrics": trace.get("final_metrics") or {},
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="P5.5 render trajectories to SFT jsonl")
    ap.add_argument("--in", dest="inp", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--mask-tool-results", action="store_true")
    args = ap.parse_args()

    rows = []
    for run_dir in _iter_runs(args.inp):
        row = render_run(run_dir, mask_tool_results=args.mask_tool_results)
        if row:
            rows.append(row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    n_tasks = len({r["task_id"] for r in rows})
    print(
        json.dumps(
            {
                "n_rows": len(rows),
                "n_tasks": n_tasks,
                "out": str(args.out),
                "mask_tool_results": args.mask_tool_results,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
