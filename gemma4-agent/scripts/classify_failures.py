#!/usr/bin/env python3
"""P2.6+ / P3 — Tag trajectories with failure_tags (agent-dev-spec §3.3).

Heuristic rules over trace JSON / JSONL. Does not invent new tag names.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

# Locked vocabulary from agent-dev-spec
ALLOWED_TAGS = frozenset(
    {
        "localize",
        "understand",
        "edit_fail",
        "test_fail",
        "loop",
        "oom_context",
        "budget",
        "scratch_pollution",
        "harness",
        "other",
    }
)


def _load_trace(path: Path) -> dict:
    text = path.read_text(errors="replace")
    if path.suffix == ".jsonl":
        steps = []
        meta: dict = {"task_id": path.stem, "resolved": False}
        for line in text.splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("type") == "result":
                meta.update(
                    {
                        "task_id": obj.get("task_id") or meta["task_id"],
                        "resolved": bool(obj.get("resolved")),
                        "error": obj.get("error"),
                    }
                )
            else:
                steps.append(obj)
        meta["steps"] = steps
        return meta
    return json.loads(text)


def _commands(trace: dict) -> list[str]:
    cmds: list[str] = []
    for step in trace.get("steps") or []:
        for call in step.get("tool_calls") or []:
            name = call.get("function_name") or call.get("name") or ""
            args = call.get("arguments") or {}
            if name == "run_command" and isinstance(args, dict):
                cmds.append(re.sub(r"\s+", " ", str(args.get("command", ""))).strip()[:240])
    return cmds


def classify_one(trace: dict) -> list[str]:
    tags: list[str] = []
    if trace.get("resolved"):
        return []

    err = str(trace.get("error") or "").lower()
    cmds = _commands(trace)
    counts = Counter(cmds)
    max_rep = max(counts.values()) if counts else 0

    if max_rep >= 5:
        tags.append("loop")
    if "timeout" in err or "budget" in err or "max_tool" in err:
        tags.append("budget")
    if "oom" in err or "context" in err and "overflow" in err:
        tags.append("oom_context")
    if "scratch" in err or "untracked" in err:
        tags.append("scratch_pollution")
    if "harness" in err or "sandbox" in err:
        tags.append("harness")
    if any("pytest" in c for c in cmds) and not trace.get("resolved"):
        tags.append("test_fail")
    if any(
        (c.get("function_name") or c.get("name")) in {"edit_file", "write_file"}
        for s in (trace.get("steps") or [])
        for c in (s.get("tool_calls") or [])
    ):
        # edits happened but still failed → likely edit quality or tests
        if "test_fail" not in tags and "edit_fail" not in tags:
            # if no pytest seen, lean edit_fail / understand
            if not any("pytest" in c for c in cmds):
                tags.append("edit_fail")
    if not cmds:
        tags.append("localize")
    if not tags:
        tags.append("other")

    return [t for t in tags if t in ALLOWED_TAGS]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--traces", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    paths = sorted(args.traces.glob("**/*"))
    traces = [
        x
        for x in paths
        if x.is_file() and x.suffix in {".json", ".jsonl"} and "smoke_summary" not in x.name
    ]
    rows = []
    tag_counts: Counter[str] = Counter()
    for path in traces:
        try:
            tr = _load_trace(path)
        except Exception as e:
            rows.append(
                {
                    "task_id": path.stem,
                    "path": str(path),
                    "resolved": False,
                    "failure_tags": ["harness"],
                    "error": f"parse:{e}"[:120],
                }
            )
            tag_counts["harness"] += 1
            continue
        tags = classify_one(tr)
        for t in tags:
            tag_counts[t] += 1
        rows.append(
            {
                "task_id": tr.get("task_id") or path.stem,
                "path": str(path),
                "resolved": bool(tr.get("resolved")),
                "failure_tags": tags,
                "error": str(tr.get("error") or "")[:200],
            }
        )

    out = {
        "n_traces": len(rows),
        "n_failed": sum(1 for r in rows if not r["resolved"]),
        "tag_counts": dict(tag_counts),
        "tasks": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({"out": str(args.out), **{k: out[k] for k in ("n_traces", "n_failed", "tag_counts")}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
