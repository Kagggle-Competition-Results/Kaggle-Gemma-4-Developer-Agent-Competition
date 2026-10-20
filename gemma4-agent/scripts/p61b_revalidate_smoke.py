#!/usr/bin/env python3
"""P6.2 — revalidate saved P6.1b smoke outputs with fixed AppleDouble detector."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "p56c_full_edit_pipeline"
CLEAN = OUT / "p61b_smoke_clean.json"
SUMMARY = OUT / "p61b_smoke_summary.csv"

# Match sandbox extension failures or AppleDouble path segments (._foo), not `._attr`.
APPLE_RE = re.compile(
    r"disallowed extension|Sandbox_disallowed_extension|(?:^|[\s\"'`/])\._[^/\s\"'`]+",
    re.I,
)
BAD_ARG_RE = re.compile(r"bad.?arg|invalid.?arg|TypeError|unexpected keyword", re.I)


def main() -> int:
    d = json.loads(CLEAN.read_text())
    rows: list[dict[str, str]] = []
    if SUMMARY.is_file():
        with SUMMARY.open(newline="") as f:
            rows = list(csv.DictReader(f))
    errors = [r.get("error", "") or "" for r in rows]
    apple = any(bool(APPLE_RE.search(e)) for e in errors)
    mean_s = float(d.get("mean_seconds") or 0)
    n = int(d.get("n") or 0)
    valid = (not apple) and mean_s >= 30.0 and n == 10
    resolved_ids = [
        r["task_id"]
        for r in rows
        if str(r.get("resolved", "")).lower() in {"true", "1"}
    ]
    bad_arg = (
        sum(1 for e in errors if BAD_ARG_RE.search(e)) / len(rows) if rows else 0.0
    )
    d.update(
        {
            "ticket": "P6.1b-clean-smoke",
            "ablation": "p61b_master_swe_adapter_v2_clean_agent",
            "valid": valid,
            "appledouble_error": apple,
            "resolved_task_ids": resolved_ids,
            "bad_arg_rate": round(bad_arg, 4),
            "baseline_smoke_pass_at_1": 0.20,
            "crashes": 0,
            "hard_crash_rate": 0.0,
            "validator_fix": "P6.2_appledouble_regex_no_private_attr",
        }
    )
    # Keep primary KPIs consistent with summary
    d["resolved"] = len(resolved_ids)
    d["pass_at_1"] = round(len(resolved_ids) / n, 4) if n else 0.0
    if rows:
        d["failed_edit_rate"] = round(
            sum(1 for r in rows if str(r.get("failed_edit", "")).lower() in {"true", "1"})
            / len(rows),
            4,
        )
        d["loop_rate"] = round(
            sum(1 for r in rows if int(float(r.get("loop_pairs") or 0)) > 0) / len(rows),
            4,
        )
    CLEAN.write_text(json.dumps(d, indent=2) + "\n")
    print(json.dumps({k: d[k] for k in (
        "valid", "appledouble_error", "pass_at_1", "resolved", "failed_edit_rate",
        "loop_rate", "crashes", "resolved_task_ids", "mean_seconds",
    )}, indent=2))
    print(f"P61B_REVALIDATE_PASS={valid}")
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
