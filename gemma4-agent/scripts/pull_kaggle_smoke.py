#!/usr/bin/env python3
"""P2.6 helper — pull Kaggle kernel output into results/smoke.json + summary CSV.

Requires: kaggle CLI + KAGGLE_API_TOKEN (or ~/.kaggle/access_token).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--kernel",
        default="moranzavdi/gemma4-p26-smoke-swegemma",
        help="Kaggle kernel slug owner/name",
    )
    p.add_argument("--out-dir", type=Path, default=Path("results"))
    p.add_argument("--results-name", default="smoke.json")
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kaggle_smoke_") as td:
        td_path = Path(td)
        r = subprocess.run(
            ["kaggle", "kernels", "output", args.kernel, "-p", str(td_path)],
            capture_output=True,
            text=True,
        )
        print(r.stdout or r.stderr)
        if r.returncode != 0:
            print(f"kaggle output failed rc={r.returncode}", flush=True)
            # still try status
            subprocess.run(["kaggle", "kernels", "status", args.kernel], check=False)
            return r.returncode

        # Prefer explicit smoke.json written by notebook
        candidates = list(td_path.rglob("smoke.json")) + list(
            td_path.rglob("**/results/smoke.json")
        )
        smoke_src = candidates[0] if candidates else None
        if smoke_src and smoke_src.is_file():
            dest = args.out_dir / args.results_name
            shutil.copy2(smoke_src, dest)
            print(f"copied {smoke_src} -> {dest}")
        else:
            # Build from smoke_summary.csv if present
            csvs = list(td_path.rglob("smoke_summary.csv"))
            if not csvs:
                print("no smoke.json or smoke_summary.csv in kernel output", flush=True)
                print("files:", [str(x.relative_to(td_path)) for x in td_path.rglob("*")][:40])
                return 2
            import csv

            rows = list(csv.DictReader(csvs[0].open()))
            n = len(rows)
            resolved = sum(
                1 for r in rows if str(r.get("resolved", "")).lower() in {"true", "1"}
            )
            out = {
                "ticket": "P2.6",
                "split": "smoke",
                "n": n,
                "resolved": resolved,
                "pass_at_1": round(resolved / n, 4) if n else 0.0,
                "hard_crash_rate": 0.0,
                "task_ids": [r.get("task_id") for r in rows],
                "source_kernel": args.kernel,
            }
            dest = args.out_dir / args.results_name
            dest.write_text(json.dumps(out, indent=2) + "\n")
            print(f"wrote {dest} from {csvs[0].name}")

        # Keep a copy of the kernel log for triage
        for log in td_path.glob("*.log"):
            shutil.copy2(log, args.out_dir / log.name)
            print(f"copied log {log.name}")

    data = json.loads((args.out_dir / args.results_name).read_text())
    print(
        json.dumps(
            {
                "pass_at_1": data.get("pass_at_1"),
                "resolved": data.get("resolved"),
                "n": data.get("n"),
                "hard_crash_rate": data.get("hard_crash_rate"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
