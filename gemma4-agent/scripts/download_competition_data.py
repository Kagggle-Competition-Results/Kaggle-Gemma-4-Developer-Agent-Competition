#!/usr/bin/env python3
"""P0.3 — Download official competition data after rules are accepted.

Requires: KAGGLE_API_TOKEN or ~/.kaggle/access_token
Fails with clear message if 403 (rules not accepted / not entered).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("data"))
    p.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Optional specific files (e.g. tasks.jsonl HARNESS_README.md). Default: full competition download.",
    )
    args = p.parse_args()

    token_path = Path.home() / ".kaggle" / "access_token"
    if "KAGGLE_API_TOKEN" not in os.environ and token_path.is_file():
        os.environ["KAGGLE_API_TOKEN"] = token_path.read_text().strip()

    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
    except ImportError:
        print("install kaggle package: pip install kaggle", file=sys.stderr)
        return 2

    api = KaggleApi()
    api.authenticate()
    args.out.mkdir(parents=True, exist_ok=True)

    try:
        if args.files:
            for f in args.files:
                print(f"downloading {f} …")
                api.competition_download_file(
                    "gemma-4-developer-agent", f, path=str(args.out), quiet=False
                )
        else:
            print("downloading full competition dataset (~22 GB) …")
            api.competition_download_files(
                "gemma-4-developer-agent", path=str(args.out), quiet=False
            )
    except Exception as e:
        msg = str(e)
        if "403" in msg or "Forbidden" in msg:
            print(
                "DOWNLOAD BLOCKED (403). Join the competition and accept rules on Kaggle "
                "(ticket P0.1): https://www.kaggle.com/competitions/gemma-4-developer-agent/rules "
                "Then re-run this script.",
                file=sys.stderr,
            )
            return 3
        raise

    print(f"done → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
