#!/usr/bin/env python3
"""P5.6b: edit/submit coverage under turn-aligned multi-window (or header+tail) slicing.

Reports % of edit_file / write_file / submit_patch tool_calls whose full token
span is covered by at least one training window (not naive right-truncation).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from train_qlora import (  # noqa: E402
    apply_chat_template,
    build_grpo_header_tail_window,
    build_labels_assistant_only,
    build_sft_windows,
    edit_coverage_for_windows,
    encode_text,
    find_edit_token_spans,
    load_sft_rows,
    load_tokenizer_backend,
    normalize_messages_for_gemma,
    resolve_model_path,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sft", type=Path, default=ROOT / "data_train" / "sft.jsonl")
    ap.add_argument("--max-seq", type=int, default=6144)
    ap.add_argument("--mode", choices=("sft_multi", "grpo_tail"), default="sft_multi")
    ap.add_argument("--model-path", type=str, default=os.environ.get("MODEL_PATH", ""))
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "results" / "p56_train_pipeline" / "seq6144_edit_coverage.json",
    )
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    cfg = {"base_model": "gemma-4-31b-it-qat-w4a16-ct", "model_path": args.model_path}
    model_dir = resolve_model_path(cfg) if not args.model_path else Path(args.model_path)
    if not (model_dir / "tokenizer_config.json").is_file() and not (model_dir / "tokenizer.json").is_file():
        raise SystemExit(f"tokenizer missing under {model_dir}; set MODEL_PATH")
    backend, tok = load_tokenizer_backend(model_dir)

    rows = load_sft_rows(args.sft)
    if args.limit:
        rows = rows[: args.limit]

    agg = {
        "n_rows": 0,
        "rows_raw_over_max": 0,
        "edit_total": 0,
        "edit_fully_in_window": 0,
        "edit_truncated_beyond": 0,
        "edit_partial_at_boundary": 0,
        "n_windows": 0,
        "token_lens": [],
        "window_lens": [],
    }
    for row in rows:
        norm = normalize_messages_for_gemma(row.get("messages") or [])
        text = apply_chat_template(norm, model_dir)
        ids = encode_text(backend, tok, text)
        labels = build_labels_assistant_only(text, ids, backend, tok)
        edits = find_edit_token_spans(text, backend, tok)
        if args.mode == "sft_multi":
            windows = build_sft_windows(
                text, ids, labels, backend, tok, args.max_seq, multi_window=True
            )
        else:
            windows = [
                build_grpo_header_tail_window(
                    text, ids, labels, backend, tok, args.max_seq
                )
            ]
        cov = edit_coverage_for_windows(edits, windows)
        agg["n_rows"] += 1
        agg["token_lens"].append(len(ids))
        if len(ids) > args.max_seq:
            agg["rows_raw_over_max"] += 1
        agg["edit_total"] += cov["edit_total"]
        agg["edit_fully_in_window"] += cov["edit_fully_in_window"]
        agg["edit_truncated_beyond"] += cov["edit_truncated_beyond"]
        agg["edit_partial_at_boundary"] += cov["edit_partial_at_boundary"]
        agg["n_windows"] += len(windows)
        for w in windows:
            agg["window_lens"].append(len(w["input_ids"]))

    lens = sorted(agg["token_lens"])

    def pct(p: float) -> int | None:
        if not lens:
            return None
        i = min(len(lens) - 1, max(0, int(round((p / 100) * (len(lens) - 1)))))
        return lens[i]

    et = agg["edit_total"] or 1
    report = {
        "max_seq": args.max_seq,
        "mode": args.mode,
        "windowing": (
            "turn_aligned_head_tail_mid_edit"
            if args.mode == "sft_multi"
            else "header_tail"
        ),
        "n_rows": agg["n_rows"],
        "rows_raw_over_max": agg["rows_raw_over_max"],
        "rows_raw_over_max_pct": round(
            100.0 * agg["rows_raw_over_max"] / max(1, agg["n_rows"]), 2
        ),
        "n_windows": agg["n_windows"],
        "token_len_p50": pct(50),
        "token_len_p90": pct(90),
        "token_len_max": lens[-1] if lens else None,
        "window_len_max": max(agg["window_lens"]) if agg["window_lens"] else None,
        "edit_total": agg["edit_total"],
        "edit_fully_in_window": agg["edit_fully_in_window"],
        "edit_truncated_beyond": agg["edit_truncated_beyond"],
        "edit_partial_at_boundary": agg["edit_partial_at_boundary"],
        "edit_in_window_pct": round(100.0 * agg["edit_fully_in_window"] / et, 2),
        "edit_truncated_pct": round(100.0 * agg["edit_truncated_beyond"] / et, 2),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["edit_in_window_pct"] >= 98.0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
