#!/usr/bin/env python3
"""Aggregate failed_edit / loop / bad-arg rates from harness trace_*.json dirs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def metrics(traces_dir: Path, label: str) -> dict:
    edit_total = edit_fail = steps = loop_pairs = bad_args = 0
    for tp in sorted(traces_dir.glob("trace_*.json")):
        t = json.loads(tp.read_text())
        calls: list[tuple[str, str]] = []
        for step in t.get("steps") or []:
            tcs = step.get("tool_calls") or []
            obs = step.get("observation") or {}
            content = obs.get("content") if isinstance(obs, dict) else ""
            for tc in tcs:
                name = tc.get("function_name") or ""
                args = tc.get("arguments") or {}
                key = (name, json.dumps(args, sort_keys=True)[:200])
                calls.append(key)
                steps += 1
                if name != "edit_file":
                    continue
                edit_total += 1
                keys = set(args.keys())
                if not {"filepath", "old_string", "new_string"}.issubset(keys):
                    bad_args += 1
                ok = False
                try:
                    o = json.loads(content) if isinstance(content, str) else content
                    if isinstance(o, dict) and o.get("status") == "ok":
                        ok = True
                except Exception:
                    pass
                text = str(content).lower()
                if "error" in text or "mandatory" in text:
                    ok = False
                if not ok:
                    edit_fail += 1
        for a, b in zip(calls, calls[1:]):
            if a == b and a[0]:
                loop_pairs += 1
    fe = round(edit_fail / edit_total, 4) if edit_total else None
    lr = round(loop_pairs / steps, 4) if steps else None
    ba = round(bad_args / edit_total, 4) if edit_total else None
    return {
        "label": label,
        "failed_edit": fe,
        "loop_rate": lr,
        "bad_arg_rate": ba,
        "edits": edit_total,
        "steps": steps,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--traces-root", type=Path, required=True, help="parent of trace dirs")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    rows = []
    for d in sorted(args.traces_root.iterdir()):
        if d.is_dir() and list(d.glob("trace_*.json")):
            rows.append(metrics(d, d.name))
    out = {"trace_kpis": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
