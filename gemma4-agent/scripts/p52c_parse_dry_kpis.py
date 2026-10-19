#!/usr/bin/env python3
"""Parse multiline JSON dry-run reports from train_qlora / train_grpo stdout."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def load_json_blob(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except Exception:
                    return None
    return None


def main() -> int:
    ql_text = Path("/tmp/p52c_qlora_dry.out").read_text(errors="ignore")
    ql_text += Path("/tmp/p52c_qlora_dry.log").read_text(errors="ignore")
    gr_text = Path("/tmp/p52c_grpo_dry.out").read_text(errors="ignore")
    gr_text += Path("/tmp/p52c_grpo_dry.log").read_text(errors="ignore")
    ql = load_json_blob(ql_text) or {}
    gr = load_json_blob(gr_text) or {}
    ls = ql.get("label_stats") or {}
    out = {
        "n_truncated": ql.get("n_truncated"),
        "tool_response_leak": ls.get("tool_response_leak", ql.get("tool_response_leak")),
        "grpo_active": gr.get("n_active_groups"),
        "grpo_mixed": gr.get("n_mixed_polarity_groups"),
        "pos_loop_gt2": gr.get("positive_anchors_with_loop_gt_2"),
        "n_active_eq_n_mixed": gr.get("n_active_eq_n_mixed"),
    }
    if out["n_truncated"] is None:
        m = re.search(r"truncated[=:](\d+)", ql_text)
        if m:
            out["n_truncated"] = int(m.group(1))
    if out["tool_response_leak"] is None:
        out["tool_response_leak"] = False
    Path("/tmp/p52c_dry_kpis.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
