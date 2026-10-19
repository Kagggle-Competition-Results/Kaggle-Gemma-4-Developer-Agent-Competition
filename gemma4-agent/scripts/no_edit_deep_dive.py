#!/usr/bin/env python3
"""Deep-dive: why traces never call edit_file (no_edit cohort).

Quantifies exploration past call #12, tool mix (graph vs rg), and ranked causes.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


GRAPH_TOOLS = {
    "get_code_neighbors",
    "get_code_subgraph",
    "search_similar_code",
}
SEARCH_SHELL = ("rg ", "grep ", "find ", "sed -n")


def tool_calls(trace: dict) -> list[tuple[str, dict]]:
    out = []
    for step in trace.get("steps") or []:
        for tc in step.get("tool_calls") or []:
            name = tc.get("function_name") or tc.get("name") or ""
            args = tc.get("arguments") or {}
            out.append((name, args if isinstance(args, dict) else {}))
    return out


def classify_no_edit(calls: list[tuple[str, dict]]) -> dict:
    n = len(calls)
    names = [c[0] for c in calls]
    n_graph = sum(1 for x in names if x in GRAPH_TOOLS)
    n_read = sum(1 for x in names if x == "read_file")
    n_run = sum(1 for x in names if x == "run_command")
    n_status = sum(1 for x in names if x == "get_status")
    n_submit = sum(1 for x in names if x == "submit_patch")
    n_write = sum(1 for x in names if x == "write_file")

    rg_cmds = 0
    for name, args in calls:
        if name != "run_command":
            continue
        cmd = str(args.get("command", "")).lower()
        if any(tok in cmd for tok in SEARCH_SHELL):
            rg_cmds += 1

    first_edit_idx = next((i for i, (n, _) in enumerate(calls) if n == "edit_file"), None)
    exploring_past_12 = first_edit_idx is None and n >= 12
    never_reached_12 = first_edit_idx is None and n < 12

    # dominant tool family among first 12
    first = names[:12] if n else []
    fam = Counter()
    for x in first:
        if x in GRAPH_TOOLS:
            fam["graph"] += 1
        elif x == "run_command":
            fam["shell"] += 1
        elif x == "read_file":
            fam["read"] += 1
        elif x == "get_status":
            fam["status"] += 1
        else:
            fam[x or "other"] += 1
    top_fam = fam.most_common(1)[0][0] if fam else "empty"

    if first_edit_idx is not None:
        cause = "has_edit"  # not in no_edit cohort
    elif exploring_past_12 and top_fam == "graph":
        cause = "explore_past_12_graph_heavy"
    elif exploring_past_12 and rg_cmds >= 4:
        cause = "explore_past_12_rg_heavy"
    elif exploring_past_12 and top_fam == "read":
        cause = "explore_past_12_read_heavy"
    elif exploring_past_12:
        cause = "explore_past_12_mixed"
    elif never_reached_12 and n_submit:
        cause = "early_stop_with_submit_empty"
    elif never_reached_12:
        cause = "early_stop_under_12_calls"
    else:
        cause = "other_no_edit"

    return {
        "n_calls": n,
        "first_edit_idx": first_edit_idx,
        "exploring_past_12": exploring_past_12,
        "n_graph": n_graph,
        "n_rg_shell": rg_cmds,
        "n_read": n_read,
        "n_run": n_run,
        "n_status": n_status,
        "n_submit": n_submit,
        "n_write": n_write,
        "top_family_first12": top_fam,
        "cause": cause,
        "tool_hist": dict(Counter(names)),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--traces", type=Path, required=True)
    p.add_argument("--summary", type=Path, default=None)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    resolved_by: dict[str, bool] = {}
    if args.summary and args.summary.is_file():
        for row in csv.DictReader(args.summary.open()):
            tid = row.get("task_id") or ""
            resolved_by[tid] = str(row.get("resolved", "")).lower() in {"true", "1", "yes"}

    tasks = []
    no_edit = []
    for tp in sorted(args.traces.glob("trace_*.json")):
        tid = tp.stem.replace("trace_", "")
        tr = json.loads(tp.read_text())
        calls = tool_calls(tr)
        stats = classify_no_edit(calls)
        resolved = resolved_by.get(tid, bool(tr.get("resolved")))
        n_edit = sum(1 for n, _ in calls if n == "edit_file")
        row = {"task_id": tid, "resolved": resolved, "n_edit": n_edit, **stats}
        tasks.append(row)
        if n_edit == 0 and not resolved:
            no_edit.append(row)

    causes = Counter(r["cause"] for r in no_edit)
    past12 = sum(1 for r in no_edit if r["exploring_past_12"])
    out = {
        "n_traces": len(tasks),
        "n_no_edit_unresolved": len(no_edit),
        "pct_no_edit": round(len(no_edit) / len(tasks), 4) if tasks else 0,
        "no_edit_exploring_past_call_12": past12,
        "pct_no_edit_past_12": round(past12 / len(no_edit), 4) if no_edit else 0,
        "ranked_causes": causes.most_common(),
        "mean_calls_no_edit": round(sum(r["n_calls"] for r in no_edit) / len(no_edit), 2)
        if no_edit
        else 0,
        "mean_graph_calls_no_edit": round(sum(r["n_graph"] for r in no_edit) / len(no_edit), 2)
        if no_edit
        else 0,
        "mean_rg_calls_no_edit": round(sum(r["n_rg_shell"] for r in no_edit) / len(no_edit), 2)
        if no_edit
        else 0,
        "recommendation": _rec(causes, past12, len(no_edit)),
        "no_edit_tasks": no_edit,
        "all_tasks_brief": [
            {
                "task_id": t["task_id"],
                "resolved": t["resolved"],
                "n_edit": t["n_edit"],
                "n_calls": t["n_calls"],
                "cause": t["cause"],
            }
            for t in tasks
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in out if k not in {"no_edit_tasks", "all_tasks_brief"}}, indent=2))
    return 0


def _rec(causes: Counter, past12: int, n: int) -> str:
    if n == 0:
        return "none"
    top = causes.most_common(1)[0][0]
    if past12 >= n * 0.5:
        return "strengthen_edit_by_12_enforcement_non_schema"
    if "rg_heavy" in top or "graph_heavy" in top:
        return "cap_localize_then_force_edit"
    return f"investigate_{top}"


if __name__ == "__main__":
    raise SystemExit(main())
