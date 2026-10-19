#!/usr/bin/env python3
"""Rich failure autopsy over harness trace_*.json (+ optional summary CSV).

Writes results JSON with primary failure modes, edit_file arg hygiene, and wall proxies.
Does not invent host schemas — uses observed tool args / errors only.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path


def _obs_content(step: dict) -> str:
    obs = step.get("observation") or {}
    if isinstance(obs, dict):
        return str(obs.get("content") or "")
    return str(obs or "")


def analyze_trace(path: Path, summary_err: str = "") -> dict:
    t = json.loads(path.read_text())
    task_id = t.get("task_id") or path.stem.replace("trace_", "")
    resolved = bool(t.get("resolved"))
    err = str(t.get("error") or summary_err or "")

    n_steps = 0
    n_edit = n_edit_ok = n_edit_fail = 0
    bad_arg = 0
    n_submit = 0
    n_pytest = 0
    tool_names: list[str] = []
    edit_keysets: Counter[str] = Counter()
    consecutive_dup = 0
    prev: tuple | None = None

    for step in t.get("steps") or []:
        tcs = step.get("tool_calls") or []
        content = _obs_content(step)
        cl = content.lower()
        for tc in tcs:
            name = tc.get("function_name") or tc.get("name") or ""
            args = tc.get("arguments") or {}
            tool_names.append(name)
            n_steps += 1
            key = (name, json.dumps(args, sort_keys=True)[:240])
            if prev == key and name:
                consecutive_dup += 1
            prev = key

            if name == "submit_patch":
                n_submit += 1
            if name == "run_command" and "pytest" in str(args.get("command", "")).lower():
                n_pytest += 1
            if name != "edit_file":
                continue
            n_edit += 1
            keys = set(args.keys()) if isinstance(args, dict) else set()
            edit_keysets[",".join(sorted(keys))] += 1
            required = {"filepath", "old_string", "new_string"}
            # also accept legacy old_str/new_str if host mapped them — count as schema miss if neither
            has_host = required.issubset(keys)
            has_legacy = {"filepath", "old_str", "new_str"}.issubset(keys) or {
                "path",
                "old_str",
                "new_str",
            }.issubset(keys)
            if not has_host and not has_legacy:
                bad_arg += 1
            ok = False
            try:
                o = json.loads(content) if content.strip().startswith("{") else None
                if isinstance(o, dict) and o.get("status") == "ok":
                    ok = True
            except Exception:
                pass
            if "error" in cl or "mandatory" in cl or "fileediterror" in cl:
                ok = False
            if ok:
                n_edit_ok += 1
            else:
                n_edit_fail += 1

    # primary mode (single label for triage)
    el = err.lower()
    if resolved:
        primary = "resolved"
    elif "session timeout" in el or "exceeded session" in el:
        primary = "session_timeout"
    elif "turns budget" in el or "tool" in el and "budget" in el:
        primary = "budget"
    elif n_edit == 0:
        primary = "no_edit"
    elif bad_arg == n_edit and n_edit > 0:
        primary = "edit_bad_args"
    elif n_edit_ok == 0 and n_edit > 0:
        primary = "edit_never_ok"
    elif n_submit == 0:
        primary = "no_submit"
    elif n_pytest > 0:
        primary = "test_fail_after_edit"
    else:
        primary = "other_unresolved"

    return {
        "task_id": task_id,
        "resolved": resolved,
        "error": err[:200],
        "primary": primary,
        "n_steps": n_steps,
        "n_edit": n_edit,
        "n_edit_ok": n_edit_ok,
        "n_edit_fail": n_edit_fail,
        "bad_arg_edits": bad_arg,
        "n_submit": n_submit,
        "n_pytest": n_pytest,
        "consecutive_dup_pairs": consecutive_dup,
        "edit_keysets": dict(edit_keysets),
        "top_tools": dict(Counter(tool_names).most_common(8)),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--traces", type=Path, required=True)
    p.add_argument("--summary", type=Path, default=None, help="optional CSV with task_id,error")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--label", default="")
    args = p.parse_args()

    err_by: dict[str, str] = {}
    resolved_by: dict[str, bool] = {}
    if args.summary and args.summary.is_file():
        for row in csv.DictReader(args.summary.open()):
            tid = row.get("task_id") or row.get("instance_id") or ""
            err_by[tid] = row.get("error") or ""
            rv = str(row.get("resolved", "")).strip().lower()
            if rv in {"true", "1", "yes"}:
                resolved_by[tid] = True
            elif rv in {"false", "0", "no"}:
                resolved_by[tid] = False

    rows = []
    for tp in sorted(args.traces.glob("trace_*.json")):
        tid = tp.stem.replace("trace_", "")
        row = analyze_trace(tp, err_by.get(tid, ""))
        if tid in resolved_by:
            row["resolved"] = resolved_by[tid]
            if row["resolved"]:
                row["primary"] = "resolved"
        rows.append(row)

    prim = Counter(r["primary"] for r in rows)
    n = len(rows)
    n_fail = sum(1 for r in rows if not r["resolved"])
    edits = sum(r["n_edit"] for r in rows)
    edit_fail = sum(r["n_edit_fail"] for r in rows)
    bad = sum(r["bad_arg_edits"] for r in rows)
    steps = sum(r["n_steps"] for r in rows)
    dups = sum(r["consecutive_dup_pairs"] for r in rows)
    step_list = [r["n_steps"] for r in rows]
    out = {
        "label": args.label or args.traces.name,
        "n_traces": n,
        "n_failed": n_fail,
        "pass_at_1": round((n - n_fail) / n, 4) if n else 0.0,
        "primary_counts": dict(prim.most_common()),
        "failed_edit_rate": round(edit_fail / edits, 4) if edits else None,
        "bad_arg_rate": round(bad / edits, 4) if edits else None,
        "loop_rate_proxy": round(dups / steps, 4) if steps else None,
        "mean_steps": round(statistics.mean(step_list), 2) if step_list else 0,
        "p95_steps": sorted(step_list)[int(0.95 * (len(step_list) - 1))] if step_list else 0,
        "tasks": rows,
        "recommendation": _recommend(prim, n_fail),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    summary = {k: out[k] for k in out if k != "tasks"}
    print(json.dumps(summary, indent=2))
    return 0


def _recommend(prim: Counter, n_fail: int) -> str:
    if n_fail == 0:
        return "none"
    top = prim.most_common(3)
    modes = [m for m, _ in top if m != "resolved"]
    if "edit_bad_args" in modes or "edit_never_ok" in modes:
        return "soft_edit_hygiene_short_span_exact_copy"
    if "no_edit" in modes or "no_submit" in modes:
        return "budget_discipline_submit_earlier"
    if "session_timeout" in modes:
        return "wall_or_earlier_submit"
    return "inspect_top_primary"


if __name__ == "__main__":
    raise SystemExit(main())
