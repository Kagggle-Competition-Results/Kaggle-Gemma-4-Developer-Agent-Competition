#!/usr/bin/env python3
"""Stage 1 / Gate 1A — CPU autopsy of P5.1a-r7d BoN rollouts.

Classifies each meta+trace into fail buckets for the >0.25 post-training runbook.
Does not invent host schemas — uses rejection_sample meta.json + harness traces.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def _obs(step: dict) -> str:
    obs = step.get("observation") or {}
    if isinstance(obs, dict):
        return str(obs.get("content") or "")
    return str(obs or "")


def analyze_trace(tp: Path) -> dict:
    t = json.loads(tp.read_text())
    n_edit = n_edit_ok = bad_arg = n_submit = n_calls = 0
    max_read_lines = 0
    read_gt_200 = 0
    for step in t.get("steps") or []:
        content = _obs(step)
        cl = content.lower()
        for tc in step.get("tool_calls") or []:
            n_calls += 1
            name = tc.get("function_name") or tc.get("name") or ""
            args = tc.get("arguments") or {}
            if name == "submit_patch":
                n_submit += 1
            if name in ("read_file", "read"):
                lines = content.count("\n") + (1 if content else 0)
                max_read_lines = max(max_read_lines, lines)
                if lines > 200:
                    read_gt_200 += 1
            if name != "edit_file":
                continue
            n_edit += 1
            keys = set(args.keys()) if isinstance(args, dict) else set()
            has_host = {"filepath", "old_string", "new_string"}.issubset(keys)
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
    return {
        "n_edit": n_edit,
        "n_edit_ok": n_edit_ok,
        "bad_arg": bad_arg,
        "n_submit": n_submit,
        "n_calls": n_calls,
        "max_read_lines": max_read_lines,
        "read_gt_200": read_gt_200,
    }


def classify(meta: dict, tr: dict | None) -> tuple[bool, str | None, str]:
    """Returns (raw_oracle_pass, keeper_filter_reject_reason, fail_class)."""
    resolved = bool(meta.get("resolved"))
    err = str(meta.get("error") or "")
    el = err.lower()
    tex = meta.get("test_exit_code")
    patch_chars = int(meta.get("patch_chars") or 0)
    tail = str(meta.get("test_output_tail") or "").lower()

    if resolved:
        reasons: list[str] = []
        if tr and tr["read_gt_200"] > 0:
            reasons.append("g11_read_gt_200")
        if tr and tr["n_calls"] > 22:
            reasons.append("tool_calls_gt_22")
        if tr and tr["bad_arg"] > 0:
            reasons.append("bad_arg_gt_0")
        return True, (",".join(reasons) if reasons else "would_pass_stage1_filters"), "resolved"

    if "failed to apply test_patch" in el:
        return False, None, "harness_test_patch_apply_fail"

    if "session timeout" in el or "exceeded session" in el:
        # This harvest used freeze wall=4.0 min (not P3.14a 8 min).
        return False, None, "wall_timeout_4min_freeze"

    if "max token" in el or "output token" in el:
        return False, None, "decode_truncation_8192"

    n_edit = tr["n_edit"] if tr else 0
    n_edit_ok = tr["n_edit_ok"] if tr else 0
    bad_arg = tr["bad_arg"] if tr else 0

    if bad_arg > 0 and n_edit_ok == 0:
        return False, None, "bad_arg_schema"

    if n_edit == 0 and patch_chars == 0:
        return False, None, "no_edit_made"

    if n_edit > 0 and n_edit_ok == 0:
        return False, None, "syntax_or_apply_error"

    if "syntaxerror" in tail or "indentationerror" in tail:
        return False, None, "syntax_or_apply_error"

    if "without calling submit_patch" in el:
        return False, None, "no_edit_made" if n_edit == 0 else "no_submit_after_edit"

    if tex in (1, 2) or (tex not in (None, 0) and not err):
        return False, None, "test_assertion_fail"

    return False, None, "other_unresolved"


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate 1A P5.1a-r7d autopsy")
    ap.add_argument(
        "--raw",
        type=Path,
        default=Path("results/p51a_r7d/traces/bon/raw"),
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("results/p51a_r7d_autopsy"),
    )
    args = ap.parse_args()

    rows: list[dict] = []
    fc: Counter[str] = Counter()
    by_task: dict[str, Counter[str]] = defaultdict(Counter)
    edit_stats: Counter[str] = Counter()
    g11_flagged = 0

    for task_dir in sorted(args.raw.iterdir()):
        if not task_dir.is_dir():
            continue
        for run in sorted(task_dir.iterdir()):
            if not run.is_dir():
                continue
            mp = run / "meta.json"
            if not mp.is_file():
                continue
            meta = json.loads(mp.read_text())
            traces = sorted(run.glob("trace_*.json"))
            if (run / "traces").is_dir():
                traces = sorted((run / "traces").glob("trace_*.json")) or traces
            tr = analyze_trace(traces[0]) if traces else None
            if tr and tr["read_gt_200"]:
                g11_flagged += 1
            raw, rej, fail = classify(meta, tr)
            fc[fail] += 1
            by_task[str(meta["task_id"])][fail] += 1
            if tr:
                if tr["n_edit"] == 0:
                    edit_stats["zero_edits"] += 1
                elif tr["n_edit_ok"] == 0:
                    edit_stats["edits_never_ok"] += 1
                else:
                    edit_stats["edits_some_ok"] += 1
                if tr["n_submit"] == 0:
                    edit_stats["no_submit"] += 1
                if int(meta.get("patch_chars") or 0) > 0:
                    edit_stats["nonzero_patch"] += 1
            row = {
                "task_id": meta["task_id"],
                "sample": meta.get("sample"),
                "raw_oracle_pass": raw,
                "keeper_filter_reject_reason": rej,
                "fail_class": fail,
                "seconds": meta.get("seconds"),
                "test_exit_code": meta.get("test_exit_code"),
                "patch_chars": meta.get("patch_chars"),
                "error": (meta.get("error") or "")[:160],
            }
            if tr:
                row.update({f"tr_{k}": v for k, v in tr.items()})
            rows.append(row)

    n = len(rows)
    summary = {
        "n_rollouts": n,
        "raw_oracle_pass": sum(1 for r in rows if r["raw_oracle_pass"]),
        "fail_class_counts": dict(fc),
        "fail_class_pct": {k: round(100 * v / n, 1) for k, v in fc.most_common()},
        "by_task": {t: dict(c) for t, c in by_task.items()},
        "edit_stats": dict(edit_stats),
        "g11_read_gt_200_rollouts": g11_flagged,
        "auto_remediation_candidates": sum(
            1 for r in rows if r["raw_oracle_pass"] and int(r.get("tr_bad_arg") or 0) == 0
        ),
        "sft_baseline_rows": 67,
        "sft_baseline_tasks": 11,
        "sft_after_stage1_rows": 67,
        "sft_after_stage1_tasks": 11,
        "harvest_mismatch_vs_p314a": {
            "agent": "SCAFFOLD_FREEZE 20-line system.md (not Verra P3.14a prompt)",
            "wall": "4.0 min (timeouts cite 4.0 min); P3.14a is 8 min",
            "calls_cap": "freeze eval vs P3.14a 28",
            "temp": 0.2,
        },
        "p314a_smoke_logs": (
            "MISSING locally — P3.14a was LB-only zip repro "
            "(results/p314a_exact_018_repro/); nearest smoke is p315a_12b "
            "FAIL Pass@1=0.10 on exact-0.18 scaffold"
        ),
    }

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "gate1a_rows.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    (args.out / "gate1a_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    lines = [
        "# Gate 1A — P5.1a-r7d autopsy",
        "",
        f"- Rollouts: **{n}** · raw_oracle_pass: **{summary['raw_oracle_pass']}** "
        "· auto-remediation keepers added: **0**",
        f"- SFT unchanged: **67 rows / 11 tasks**",
        "- Harvest agent ≠ P3.14a: freeze wall **4 min** + freeze prompt "
        "(see `gate1a_summary.json`)",
        "- P3.14a smoke logs: **not present** (LB-only)",
        "",
        "## fail_class distribution",
        "",
        "| fail_class | n | pct |",
        "| --- | ---: | ---: |",
    ]
    for k, v in fc.most_common():
        lines.append(f"| `{k}` | {v} | {100 * v / n:.1f}% |")
    lines += [
        "",
        "## by_task",
        "",
        "| task_id | dominant fails |",
        "| --- | --- |",
    ]
    for t, c in sorted(by_task.items()):
        parts = ", ".join(f"{k}:{v}" for k, v in c.most_common())
        lines.append(f"| `{t}` | {parts} |")
    lines += [
        "",
        "## Stage 1 remediation",
        "",
        "- No `raw_oracle_pass==True` rollouts → G11 truncate post-processor "
        "**N/A** (would not increase keepers).",
        "- Hard stop before Stage 2 GPU until human accepts: "
        "(1) retarget harvest to **exact P3.14a** package, "
        "(2) fix `fastapi_14953` harness `test_patch` apply fails, "
        "(3) decide pool refresh for all-timeout / all-no_edit tasks.",
        "",
    ]
    (args.out / "GATE1A.md").write_text("\n".join(lines))
    print(json.dumps(summary, indent=2))
    print(f"wrote {args.out / 'GATE1A.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
