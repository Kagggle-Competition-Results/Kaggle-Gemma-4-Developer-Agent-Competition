#!/usr/bin/env python3
"""P5.10–13 — GRPO with ORM + rule heuristics only (agent-dev-spec §5–§6, G13).

CLI:
  python scripts/train_grpo.py --config configs/train_grpo.yaml --out agent/adapters/<name>
  python scripts/train_grpo.py --config configs/train_grpo.yaml --out /tmp/unused --dry-run

Reward (NO PRM):
  R = R_exec + R_syntax + R_heuristic - R_penalty
  R_exec: ORM from meta.resolved / verify_task
  R_heuristic: +0.1 graph/AST before first edit; -0.2 exact-repeat tool args
  R_penalty: lambda_t * tokens + lambda_u * n_tools

Loss masking: identical assistant-only / no-<|tool_response|> policy as
scripts/train_qlora.py (P5.3b).

--dry-run: CPU group-advantage + reward + mask checks on Wave1 metas (no GPU).
SimPO is backlog only (P5.SimPO) — not implemented on the critical path (G12).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Before train_qlora / torch imports: reduce CUDA allocator fragmentation on 48GB.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import yaml

# Reuse P5.3b mask helpers (same process, no new schema).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from train_qlora import (  # noqa: E402
    IGNORE_INDEX,
    apply_chat_template,
    build_grpo_windows,
    build_labels_assistant_only,
    classify_edit_roles,
    classify_supervised_leakage,
    edit_coverage_for_windows,
    encode_text,
    find_edit_calls_detailed,
    find_edit_token_spans,
    load_tokenizer_backend,
    mask_superseded_edit_labels,
    normalize_messages_for_gemma,
    resolve_model_path,
    save_peft_adapter_bf16,
    validate_config as validate_qlora_peft_subset,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text()) or {}
    if not isinstance(cfg, dict):
        raise SystemExit(f"config must be a mapping: {path}")
    return cfg


def validate_grpo_config(cfg: dict[str, Any]) -> dict[str, Any]:
    # Reuse rank / peft guards from train_qlora validator fields
    peft_cfg = {
        "peft_type": cfg.get("peft_type", "dora"),
        "rank": cfg.get("rank", 64),
        "lora_alpha": cfg.get("lora_alpha", 128),
        "learning_rate": cfg.get("learning_rate", 1e-5),
        "max_seq_length": cfg.get("max_seq_length", 24576),
        "target_modules": cfg.get("target_modules") or ["q_proj", "v_proj"],
        "base_model": cfg.get("base_model") or "gemma-4-31b-it-qat-w4a16-ct",
        "assistant_only_loss": cfg.get("assistant_only_loss", True),
        "model_path": cfg.get("model_path", ""),
    }
    peft_cfg = validate_qlora_peft_subset(peft_cfg)
    g = int(cfg.get("group_size", 4))
    if g < 2:
        raise SystemExit(f"group_size must be >=2, got {g}")
    beta = float(cfg.get("beta", 0.04))
    reward = dict(cfg.get("reward") or {})
    for key, default in (
        ("r_exec_pass", 1.0),
        ("r_exec_fail", 0.0),
        ("r_syntax_ok", 0.1),
        ("r_syntax_bad", 0.0),
        ("r_heuristic_graph_before_edit", 0.1),
        ("r_heuristic_repeat_tool_args", -0.2),
        ("r_heuristic_edit_sprawl", -0.15),
        ("r_heuristic_diff_bloat", -0.15),
        ("r_heuristic_syntax_import_error", -0.25),
        ("lambda_t", 1e-5),
        ("lambda_u", 0.01),
    ):
        reward.setdefault(key, default)
    out = dict(cfg)
    out.update(peft_cfg)
    out["group_size"] = g
    out["beta"] = beta
    out["reward"] = reward
    out["require_positive_in_group"] = bool(cfg.get("require_positive_in_group", True))
    out["require_negative_in_group"] = bool(cfg.get("require_negative_in_group", True))
    out["require_reward_variance"] = bool(cfg.get("require_reward_variance", True))
    out["clamp_unresolved_advantage"] = bool(cfg.get("clamp_unresolved_advantage", True))
    out["enforce_polarity_margin"] = bool(cfg.get("enforce_polarity_margin", True))
    margin = float(cfg.get("advantage_margin", 0.05))
    if margin <= 0:
        raise SystemExit(f"advantage_margin must be >0, got {margin}")
    out["advantage_margin"] = margin
    # Align with filter_bon_keepers / P5.1c spiral: resolved+loop>max cannot be +adv anchors.
    max_loop = int(cfg.get("max_loop_pairs_positive", 2))
    if max_loop < 0:
        raise SystemExit(f"max_loop_pairs_positive must be >=0, got {max_loop}")
    out["max_loop_pairs_positive"] = max_loop
    max_edits_pos = int(cfg.get("max_edits_positive", 8))
    max_patch_pos = int(cfg.get("max_patch_chars_positive", 4500))
    if max_edits_pos < 1:
        raise SystemExit(f"max_edits_positive must be >=1, got {max_edits_pos}")
    if max_patch_pos < 1:
        raise SystemExit(f"max_patch_chars_positive must be >=1, got {max_patch_pos}")
    out["max_edits_positive"] = max_edits_pos
    out["max_patch_chars_positive"] = max_patch_pos
    out["mask_superseded_edits"] = bool(cfg.get("mask_superseded_edits", True))
    out["max_windows_per_row"] = int(cfg.get("max_windows_per_row", 2))
    out["allow_mid_edit_windows"] = bool(cfg.get("allow_mid_edit_windows", False))
    # ORM dominance: r_exec_pass must exceed max bounded heuristic uplift on fails
    reward = out["reward"]
    max_fail_uplift = (
        abs(float(reward["r_syntax_ok"]))
        + abs(float(reward["r_heuristic_graph_before_edit"]))
        + abs(float(reward["r_heuristic_repeat_tool_args"]))
        + abs(float(reward["r_heuristic_edit_sprawl"]))
        + abs(float(reward["r_heuristic_diff_bloat"]))
        + abs(float(reward["r_heuristic_syntax_import_error"]))
    )
    if float(reward["r_exec_pass"]) - float(reward["r_exec_fail"]) <= max_fail_uplift:
        print(
            f"WARN ORM margin weak: r_exec_pass-fail="
            f"{float(reward['r_exec_pass']) - float(reward['r_exec_fail'])} "
            f"<= heuristic_bound={max_fail_uplift}",
            flush=True,
        )
    return out


def _find_trace(run_dir: Path) -> Path | None:
    hits = sorted((run_dir / "traces").glob("trace_*.json")) if (run_dir / "traces").is_dir() else []
    if hits:
        return hits[0]
    hits = sorted(run_dir.glob("trace_*.json"))
    return hits[0] if hits else None


def _task_id_from_meta_path(meta: dict[str, Any], meta_p: Path) -> str:
    tid = meta.get("task_id")
    if tid:
        return str(tid)
    # bon: raw/<task>/nXX/meta.json or keep/<task>_nXX/meta.json
    # star: <task>/attemptN/meta.json or keep/<task>_attemptN/meta.json
    parent = meta_p.parent.name
    if parent.startswith("attempt") or parent.startswith("n"):
        grand = meta_p.parent.parent.name
        return grand.split("_n")[0].split("_attempt")[0]
    return parent.split("_n")[0].split("_attempt")[0]


def _is_keep_mirror(meta_p: Path) -> bool:
    """True if path sits under a keep/ (or keep_clean/) mirror tree."""
    parts = {p.lower() for p in meta_p.parts}
    return "keep" in parts or "keep_clean" in parts


def _rollout_dedupe_key(row: dict[str, Any]) -> str:
    """Identity for excluding keep/ mirrors and exact trajectory clones."""
    patch = row.get("patch") or ""
    patch_h = hashlib.sha256(patch.encode("utf-8", errors="replace")).hexdigest()[:24]
    # Prefer patch hash; fall back to run_dir leaf when patch empty
    sample = row.get("sample")
    resolved = bool(row.get("resolved"))
    if patch.strip():
        return f"{row['task_id']}|{int(resolved)}|{patch_h}"
    return f"{row['task_id']}|{int(resolved)}|{sample}|{patch_h}"


def dedupe_rollouts(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop keep/ mirror copies and identical patch clones; prefer non-keep paths.

    When keep/ holds the only full trace (raw often has meta-only), merge max(loop_pairs)
    onto the surviving row so spiral keepers cannot slip in as loop=0 positive anchors.
    """
    # Sort: non-keep first, then shorter path (raw/attempt over nested keep_clean)
    ranked = sorted(
        rows,
        key=lambda r: (
            1 if _is_keep_mirror(Path(r["meta_path"])) else 0,
            len(r["meta_path"]),
            r["meta_path"],
        ),
    )
    by_key: dict[str, dict[str, Any]] = {}
    n_keep_dropped = 0
    n_clone_dropped = 0
    n_loop_merged = 0
    for r in ranked:
        key = _rollout_dedupe_key(r)
        if key not in by_key:
            row = dict(r)
            row["dedupe_key"] = key
            row["is_keep_mirror"] = _is_keep_mirror(Path(r["meta_path"]))
            row["loop_pairs"] = int(r.get("loop_pairs") or 0)
            by_key[key] = row
            continue
        prev = by_key[key]
        lp_new = int(r.get("loop_pairs") or 0)
        lp_old = int(prev.get("loop_pairs") or 0)
        if lp_new > lp_old:
            prev["loop_pairs"] = lp_new
            n_loop_merged += 1
        if _is_keep_mirror(Path(r["meta_path"])):
            n_keep_dropped += 1
        else:
            n_clone_dropped += 1
    out = list(by_key.values())
    stats = {
        "n_in": len(rows),
        "n_out": len(out),
        "n_keep_mirror_dropped": n_keep_dropped,
        "n_clone_dropped": n_clone_dropped,
        "n_loop_pairs_merged_from_dup": n_loop_merged,
    }
    return out, stats


def iter_rollout_metas(traces_roots: list[Path]) -> list[dict[str, Any]]:
    """Collect BoN + STaR metas so keepers can pair with failed near-misses by task_id.

    keep/ mirrors are collected then removed by dedupe_rollouts (prefer attempt/raw).
    """
    rows: list[dict[str, Any]] = []
    seen: set[Path] = set()
    globs = (
        "raw/*/n*/meta.json",
        "keep/*/meta.json",
        "keep/*/*/meta.json",
        "*/attempt*/meta.json",
        "keep_clean/keep/*/meta.json",
    )
    for root in traces_roots:
        if not root.exists():
            continue
        meta_paths: list[Path] = []
        for pattern in globs:
            meta_paths.extend(root.glob(pattern))
        for meta_p in meta_paths:
            meta_p = meta_p.resolve()
            if meta_p in seen:
                continue
            seen.add(meta_p)
            try:
                meta = json.loads(meta_p.read_text())
            except json.JSONDecodeError:
                continue
            tid = _task_id_from_meta_path(meta, meta_p)
            sample = meta.get("sample")
            if sample is None:
                # derive from attemptN / nXX
                leaf = meta_p.parent.name
                if leaf.startswith("attempt"):
                    try:
                        sample = int(leaf.replace("attempt", ""))
                    except ValueError:
                        sample = leaf
                elif leaf.startswith("n") and leaf[1:].isdigit():
                    sample = int(leaf[1:])
                else:
                    sample = leaf
            run_dir = meta_p.parent
            rows.append(
                {
                    "task_id": tid,
                    "sample": sample,
                    "meta_path": str(meta_p),
                    "run_dir": str(run_dir),
                    "resolved": bool(meta.get("resolved")),
                    "loop_pairs": loop_pairs_for_run(run_dir),
                    "test_exit_code": meta.get("test_exit_code"),
                    "patch": meta.get("patch") or "",
                    "patch_chars": int(meta.get("patch_chars") or len(meta.get("patch") or "")),
                    "seconds": float(meta.get("seconds") or 0.0),
                    "error": meta.get("error") or meta.get("error_tail") or "",
                    "source_root": str(root),
                }
            )
    deduped, _ = dedupe_rollouts(rows)
    return deduped


def _tool_calls_from_trace(run_dir: Path) -> list[dict[str, Any]]:
    trace_p = _find_trace(run_dir)
    if not trace_p:
        return []
    try:
        trace = json.loads(trace_p.read_text())
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for step in trace.get("steps") or []:
        for tc in step.get("tool_calls") or []:
            out.append(
                {
                    "name": tc.get("function_name") or tc.get("name") or "",
                    "arguments": tc.get("arguments") or {},
                    "step_id": step.get("step_id"),
                }
            )
    return out


def loop_pairs_from_trace(trace: dict[str, Any]) -> int:
    """Consecutive duplicate tool-arg pairs (same definition as filter_bon_keepers)."""
    calls: list[tuple[str, str]] = []
    for step in trace.get("steps") or []:
        for tc in step.get("tool_calls") or []:
            name = tc.get("function_name") or tc.get("name") or ""
            args = tc.get("arguments") or {}
            calls.append((name, json.dumps(args, sort_keys=True)[:240]))
    n = 0
    for a, b in zip(calls, calls[1:]):
        if a == b and a[0]:
            n += 1
    return n


def loop_pairs_for_run(run_dir: Path) -> int:
    trace_p = _find_trace(run_dir)
    if not trace_p:
        return 0
    try:
        trace = json.loads(trace_p.read_text())
    except json.JSONDecodeError:
        return 0
    return loop_pairs_from_trace(trace)


def _n_edits_from_tool_calls(tool_calls: list[dict[str, Any]]) -> int:
    return sum(
        1
        for tc in tool_calls
        if str(tc.get("name") or "") in {"edit_file", "write_file"}
    )


def _has_syntax_or_escaped_docstring(row: dict[str, Any], tool_calls: list[dict[str, Any]]) -> bool:
    blob = " ".join(
        [
            str(row.get("error") or ""),
            str(row.get("patch") or "")[:8000],
            json.dumps(tool_calls)[:20000],
        ]
    )
    if "SyntaxError" in blob or "syntaxerror" in blob.lower():
        return True
    if '\\"\\"\\"' in blob or '\\\\\\"\\\\\\"\\\\\\"' in blob:
        return True
    # Literal escaped docstring pattern seen on rich_3930 agent patches
    if '\\\\"\\\\"\\\\"' in blob:
        return True
    return False


def is_positive_anchor(
    row: dict[str, Any],
    max_loop_pairs: int,
    *,
    max_edits_positive: int | None = None,
    max_patch_chars_positive: int | None = None,
) -> bool:
    """True-oracle pass, non-spiral, and under P6.5 sprawl caps when set."""
    if not bool(row.get("resolved")):
        return False
    if int(row.get("loop_pairs") or 0) > int(max_loop_pairs):
        return False
    if max_edits_positive is not None:
        n_edits = int(row.get("n_edits") or 0)
        if n_edits > int(max_edits_positive):
            return False
    if max_patch_chars_positive is not None:
        if int(row.get("patch_chars") or 0) > int(max_patch_chars_positive):
            return False
    return True


def score_rollout(row: dict[str, Any], reward: dict[str, Any]) -> dict[str, float]:
    """ORM + rule heuristics (G13). No learned PRM. P6.5 adds sprawl/syntax penalties."""
    r_exec = float(reward["r_exec_pass"] if row["resolved"] else reward["r_exec_fail"])
    patch = row.get("patch") or ""
    syntax_ok = ("diff --git" in patch or patch.startswith("---")) and ("+++" in patch or "@@" in patch)
    if patch.strip() == "":
        syntax_ok = False
    r_syntax = float(reward["r_syntax_ok"] if syntax_ok else reward["r_syntax_bad"])

    tool_calls = _tool_calls_from_trace(Path(row["run_dir"]))
    n_tools = len(tool_calls)
    n_edits = _n_edits_from_tool_calls(tool_calls)
    row["n_edits"] = n_edits
    # Estimate tokens from patch + rough tool budget when no tokenizer available
    tokens = max(row.get("patch_chars") or 0, 1) + 64 * n_tools

    r_h = 0.0
    # +0.1 graph/AST before first edit
    first_edit_i = None
    first_graph_i = None
    for i, tc in enumerate(tool_calls):
        name = str(tc.get("name") or "")
        if first_edit_i is None and name in {"edit_file", "write_file", "submit_patch"}:
            first_edit_i = i
        if first_graph_i is None and any(
            k in name for k in ("graph", "ast", "get_code_subgraph", "get_code_neighbors", "search_similar")
        ):
            first_graph_i = i
    if first_graph_i is not None and (first_edit_i is None or first_graph_i < first_edit_i):
        r_h += float(reward["r_heuristic_graph_before_edit"])

    # -0.2 exact-repeat tool args
    seen: set[str] = set()
    for tc in tool_calls:
        key = json.dumps({"n": tc.get("name"), "a": tc.get("arguments")}, sort_keys=True, ensure_ascii=False)
        if key in seen:
            r_h += float(reward["r_heuristic_repeat_tool_args"])
            break
        seen.add(key)

    # P6.5: edit sprawl / diff bloat / syntax-escape penalties
    if n_edits > 4:
        r_h += float(reward.get("r_heuristic_edit_sprawl", -0.15))
    if int(row.get("patch_chars") or 0) > 3000:
        r_h += float(reward.get("r_heuristic_diff_bloat", -0.15))
    if _has_syntax_or_escaped_docstring(row, tool_calls):
        r_h += float(reward.get("r_heuristic_syntax_import_error", -0.25))

    r_pen = float(reward["lambda_t"]) * float(tokens) + float(reward["lambda_u"]) * float(n_tools)
    total = r_exec + r_syntax + r_h - r_pen
    return {
        "R": total,
        "R_exec": r_exec,
        "R_syntax": r_syntax,
        "R_heuristic": r_h,
        "R_penalty": r_pen,
        "n_tools": float(n_tools),
        "n_edits": float(n_edits),
        "tokens_est": float(tokens),
    }


def group_advantages(
    rows: list[dict[str, Any]],
    group_size: int,
    *,
    require_positive_in_group: bool = True,
    require_negative_in_group: bool = True,
    require_reward_variance: bool = True,
    clamp_unresolved_advantage: bool = True,
    enforce_polarity_margin: bool = True,
    advantage_margin: float = 0.05,
    max_loop_pairs_positive: int = 2,
    max_edits_positive: int = 8,
    max_patch_chars_positive: int = 4500,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build mixed-polarity GRPO groups with ORM-strict polarity margins.

    Eligibility:
      - ≥1 non-spiral true-oracle resolved (loop_pairs <= max_loop_pairs_positive)
      - under sprawl hard caps (n_edits / patch_chars) when set (P6.6: 8 / 4500)
      - ≥1 true-oracle fail when require_negative_in_group (skip all-pass)
      - ≥2 rollouts; reward variance > 0 when require_reward_variance
    Resolved edit-spirals (loop_pairs > max) are never positive anchors (no +adv).
    Zero-pass / all-pass groups are skipped (SFT still trains on all-pass keepers).
    Mixed-polarity groups: positive-anchor adv >= +margin, fail adv <= -margin.
    """
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_task[r["task_id"]].append(r)

    active: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    n_fail_pos_before_clamp = 0
    n_fail_pos_after_clamp = 0
    n_zero_adv = 0
    n_spiral_resolved_excluded = 0
    n_positive_anchors_loop_gt_max = 0
    n_sprawl_resolved_excluded = 0
    margin = float(advantage_margin)
    max_loop = int(max_loop_pairs_positive)
    max_edits = int(max_edits_positive)
    max_patch = int(max_patch_chars_positive)

    def _pos(x: dict[str, Any]) -> bool:
        return is_positive_anchor(
            x,
            max_loop,
            max_edits_positive=max_edits,
            max_patch_chars_positive=max_patch,
        )

    for tid, items in sorted(by_task.items()):
        # Second-pass dedupe inside task (belt-and-suspenders)
        uniq: list[dict[str, Any]] = []
        seen_k: set[str] = set()
        for x in items:
            k = x.get("dedupe_key") or _rollout_dedupe_key(x)
            if k in seen_k:
                continue
            seen_k.add(k)
            uniq.append(x)
        items = uniq

        spiral_resolved = [
            x for x in items if x["resolved"] and int(x.get("loop_pairs") or 0) > max_loop
        ]
        n_spiral_resolved_excluded += len(spiral_resolved)
        sprawl_resolved = [
            x
            for x in items
            if x["resolved"]
            and int(x.get("loop_pairs") or 0) <= max_loop
            and not _pos(x)
        ]
        n_sprawl_resolved_excluded += len(sprawl_resolved)
        # Positive anchors: resolved, non-spiral, under sprawl caps (P6.5).
        # Prefer cleaner keepers (fewest edits, then smallest patch) as primary +anchor.
        keeps = sorted(
            [x for x in items if _pos(x)],
            key=lambda x: (int(x.get("n_edits") or 0), int(x.get("patch_chars") or 0)),
        )
        fails = [x for x in items if not x["resolved"]]
        n_ok_pool = len(keeps)
        n_fail_pool = len(fails)

        if require_positive_in_group and n_ok_pool < 1:
            if spiral_resolved and not sprawl_resolved:
                reason = "zero_pass_spiral_only"
            elif sprawl_resolved:
                reason = "zero_pass_sprawl_only"
            else:
                reason = "zero_pass"
            skipped.append(
                {
                    "task_id": tid,
                    "reason": reason,
                    "n_resolved_pool": n_ok_pool,
                    "n_fail_pool": n_fail_pool,
                    "n_spiral_resolved": len(spiral_resolved),
                    "n_sprawl_resolved": len(sprawl_resolved),
                    "spiral_loop_pairs": [int(x.get("loop_pairs") or 0) for x in spiral_resolved],
                }
            )
            continue

        if require_negative_in_group and n_fail_pool < 1:
            skipped.append(
                {
                    "task_id": tid,
                    "reason": "all_pass",
                    "n_resolved_pool": n_ok_pool,
                    "n_fail_pool": n_fail_pool,
                }
            )
            continue

        # Prefer 1 unique non-spiral keeper + (G-1) distinct fails when enough negatives exist.
        # Never clone a positive to pad G. Never pick spiral/sprawl-resolved as +anchor.
        picked: list[dict[str, Any]] = []
        if n_ok_pool >= 1 and n_fail_pool >= max(1, group_size - 1):
            picked.append(keeps[0])
            picked.extend(fails[: group_size - 1])
        else:
            # Mixed but short on fails: 1 keeper + all fails + extra distinct keepers
            picked.append(keeps[0])
            picked.extend(fails)
            for k in keeps[1:]:
                if len(picked) >= group_size:
                    break
                picked.append(k)

        # Deduplicate picked by trajectory key (never clone)
        picked_u: list[dict[str, Any]] = []
        seen_pick: set[str] = set()
        for p in picked:
            k = p.get("dedupe_key") or _rollout_dedupe_key(p)
            if k in seen_pick:
                continue
            seen_pick.add(k)
            picked_u.append(p)
        picked = picked_u[:group_size]

        if require_positive_in_group and not any(_pos(p) for p in picked):
            skipped.append({"task_id": tid, "reason": "pick_lost_positive", "n_resolved_pool": n_ok_pool})
            continue
        if require_negative_in_group and not any(not p["resolved"] for p in picked):
            skipped.append({"task_id": tid, "reason": "pick_lost_negative", "n_fail_pool": n_fail_pool})
            continue
        if len(picked) < 2:
            skipped.append({"task_id": tid, "reason": "group_too_small", "n": len(picked)})
            continue

        if require_positive_in_group and sum(1 for p in picked if _pos(p)) < 1:
            skipped.append({"task_id": tid, "reason": "no_positive_in_picked"})
            continue
        if require_negative_in_group and sum(1 for p in picked if not p["resolved"]) < 1:
            skipped.append({"task_id": tid, "reason": "no_negative_in_picked"})
            continue

        rewards = [float(x["scores"]["R"]) for x in picked]
        mean = statistics.mean(rewards)
        std = statistics.pstdev(rewards) if len(rewards) > 1 else 0.0
        if require_reward_variance and std < 1e-8:
            skipped.append(
                {
                    "task_id": tid,
                    "reason": "zero_reward_variance",
                    "n_resolved": sum(1 for p in picked if _pos(p)),
                }
            )
            continue

        if std < 1e-8:
            adv = [0.0 for _ in rewards]
            std_used = 0.0
        else:
            adv = [(r - mean) / std for r in rewards]
            std_used = std

        n_res = sum(1 for x in picked if _pos(x))
        n_fail = sum(1 for x in picked if not x["resolved"])
        mixed = n_res >= 1 and n_fail >= 1
        pos_flags = [_pos(p) for p in picked]

        # Count inversion before polarity enforcement
        for p, a, pos in zip(picked, adv, pos_flags):
            if (not pos) and a > 0:
                n_fail_pos_before_clamp += 1

        if mixed and enforce_polarity_margin:
            # Strict polarity: only non-spiral resolved get +margin; fails get -margin.
            # Spiral-resolved must never appear in picked (guarded above).
            adv = [
                max(a, margin) if pos else min(a, -margin)
                for a, pos in zip(adv, pos_flags)
            ]
        elif clamp_unresolved_advantage:
            adv = [min(a, 0.0) if not pos else a for a, pos in zip(adv, pos_flags)]

        # All-pass: avoid exact-zero advantages (no gradient)
        if not mixed:
            adv = [
                (margin * 0.5 if (i % 2 == 0) else -margin * 0.5) if abs(a) < 1e-12 else a
                for i, a in enumerate(adv)
            ]

        for p, a, pos in zip(picked, adv, pos_flags):
            if (not pos) and a > 0:
                n_fail_pos_after_clamp += 1
            if abs(a) < 1e-12:
                n_zero_adv += 1
            if pos and int(p.get("loop_pairs") or 0) > max_loop:
                n_positive_anchors_loop_gt_max += 1

        adv_mean = statistics.mean(adv) if adv else 0.0
        adv_std = statistics.pstdev(adv) if len(adv) > 1 else 0.0
        dedupe_keys = [x.get("dedupe_key") or _rollout_dedupe_key(x) for x in picked]
        res_advs = [a for pos, a in zip(pos_flags, adv) if pos]
        fail_advs = [a for p, a in zip(picked, adv) if not p["resolved"]]
        active.append(
            {
                "task_id": tid,
                "n": len(picked),
                "rewards": rewards,
                "advantage": adv,
                "reward_mean": mean,
                "reward_std": std_used,
                "adv_mean": adv_mean,
                "adv_std": adv_std,
                "n_resolved": n_res,
                "n_failed": n_fail,
                "mixed_polarity": mixed,
                "min_resolved_adv": min(res_advs) if res_advs else None,
                "max_unresolved_adv": max(fail_advs) if fail_advs else None,
                "samples": [x.get("sample") for x in picked],
                "resolved_flags": [bool(x["resolved"]) for x in picked],
                "positive_anchor_flags": pos_flags,
                "loop_pairs": [int(x.get("loop_pairs") or 0) for x in picked],
                "dedupe_keys": dedupe_keys,
                "n_unique_trajectories": len(set(dedupe_keys)),
                "has_duplicate_trajectories": len(set(dedupe_keys)) != len(dedupe_keys),
                "source_roots": sorted({x.get("source_root", "") for x in picked}),
                "meta_paths": [x.get("meta_path") for x in picked],
            }
        )

    stats = {
        "n_active_groups": len(active),
        "n_skipped_groups": len(skipped),
        "skipped_reasons": {
            k: sum(1 for s in skipped if s.get("reason") == k)
            for k in sorted({s.get("reason", "") for s in skipped})
        },
        "failed_rollouts_with_positive_adv_before_clamp": n_fail_pos_before_clamp,
        "failed_rollouts_with_positive_adv": n_fail_pos_after_clamp,
        "require_positive_in_group": require_positive_in_group,
        "require_negative_in_group": require_negative_in_group,
        "require_reward_variance": require_reward_variance,
        "clamp_unresolved_advantage": clamp_unresolved_advantage,
        "enforce_polarity_margin": enforce_polarity_margin,
        "advantage_margin": margin,
        "max_loop_pairs_positive": max_loop,
        "max_edits_positive": max_edits,
        "max_patch_chars_positive": max_patch,
        "n_spiral_resolved_excluded": n_spiral_resolved_excluded,
        "n_sprawl_resolved_excluded": n_sprawl_resolved_excluded,
        "positive_anchors_with_loop_gt_2": n_positive_anchors_loop_gt_max,
        "zero_adv_rollouts": n_zero_adv,
        "groups_with_duplicate_trajectories": sum(
            1 for g in active if g.get("has_duplicate_trajectories")
        ),
        "skipped_sample": skipped[:8],
    }
    return active, stats


def dry_run_mask_check(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ensure tool_response masking still holds (reuse train_qlora helpers)."""
    try:
        model_dir = resolve_model_path(cfg)
        backend, tok = load_tokenizer_backend(model_dir)
    except SystemExit as exc:
        return {"mask_check": "skipped", "reason": str(exc)}

    # Synthetic chat with tool_response inside model turn
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fix bug"},
        {
            "role": "assistant",
            "content": json.dumps(
                {"tool_calls": [{"name": "run_command", "arguments": {"command": "pytest tests/test_x.py"}}]}
            ),
        },
        {"role": "tool", "content": json.dumps({"tool": "run_command", "status": "masked", "chars": 99})},
        {
            "role": "assistant",
            "content": json.dumps(
                {"tool_calls": [{"name": "submit_patch", "arguments": {"patch": "diff --git a/x b/x\n"}}]}
            ),
        },
    ]
    norm = normalize_messages_for_gemma(messages)
    text = apply_chat_template(norm, model_dir)
    ids = encode_text(backend, tok, text)
    labels = build_labels_assistant_only(text, ids, backend, tok)
    leak = classify_supervised_leakage(text, ids, labels, backend, tok)
    return {
        "mask_check": "ran",
        "n_tokens": len(ids),
        "n_supervised": leak["supervised_tokens"],
        "tool_response_leak": leak["tool_response_leak"],
        "supervised_in_tool_response_blocks": leak["supervised_in_tool_response_blocks"],
        "pass": not leak["tool_response_leak"],
    }


def dry_run(cfg: dict[str, Any]) -> dict[str, Any]:
    roots_cfg = cfg.get("traces_roots") or ["results/p51a_r8_p314a/traces/bon"]
    env_root = os.environ.get("GRPO_TRACES")
    roots: list[Path] = []
    if env_root:
        # Colon-separated multi-root override
        for part in env_root.split(":"):
            part = part.strip()
            if part:
                roots.append(Path(part))
    for r in roots_cfg:
        p = Path(r)
        if not p.is_absolute():
            p = _repo_root() / p
        if p not in roots:
            roots.append(p)

    rollouts = iter_rollout_metas(roots)
    if not rollouts:
        raise SystemExit(f"no rollout metas under {roots}")

    for row in rollouts:
        row["scores"] = score_rollout(row, cfg["reward"])

    groups, filt = group_advantages(
        rollouts,
        int(cfg["group_size"]),
        require_positive_in_group=bool(cfg.get("require_positive_in_group", True)),
        require_negative_in_group=bool(cfg.get("require_negative_in_group", True)),
        require_reward_variance=bool(cfg.get("require_reward_variance", True)),
        clamp_unresolved_advantage=bool(cfg.get("clamp_unresolved_advantage", True)),
        enforce_polarity_margin=bool(cfg.get("enforce_polarity_margin", True)),
        advantage_margin=float(cfg.get("advantage_margin", 0.05)),
        max_loop_pairs_positive=int(cfg.get("max_loop_pairs_positive", 2)),
        max_edits_positive=int(cfg.get("max_edits_positive", 8)),
        max_patch_chars_positive=int(cfg.get("max_patch_chars_positive", 4500)),
    )

    # ORM strict: no unresolved rollout may keep advantage > 0
    failed_pos = int(filt["failed_rollouts_with_positive_adv"])
    zero_pass_active = sum(1 for g in groups if g["n_resolved"] < 1)
    all_pass_active = sum(1 for g in groups if g.get("n_failed", 0) < 1)
    mixed_sources = sum(1 for g in groups if len(g.get("source_roots") or []) > 1)
    dup_groups = int(filt.get("groups_with_duplicate_trajectories") or 0)
    no_dups = dup_groups == 0 and all(
        g.get("n_unique_trajectories") == g.get("n") for g in groups
    )
    zero_adv = int(filt.get("zero_adv_rollouts") or 0)
    margin = float(cfg.get("advantage_margin", 0.05))
    max_loop = int(cfg.get("max_loop_pairs_positive", 2))
    # Audit active groups: no positive anchor may have loop_pairs > max
    positive_anchors_loop_gt = sum(
        1
        for g in groups
        for pos, lp in zip(g.get("positive_anchor_flags") or [], g.get("loop_pairs") or [])
        if pos and int(lp) > max_loop
    )
    # Diagnostic only (not a pass gate): spiral-only tasks should not be active.
    fastapi_14455_active = any(g["task_id"] == "fastapi_14455" for g in groups)
    mixed_groups = [g for g in groups if g.get("mixed_polarity")]
    active_eq_mixed = len(groups) == len(mixed_groups) and all_pass_active == 0
    res_mins = [g["min_resolved_adv"] for g in mixed_groups if g.get("min_resolved_adv") is not None]
    fail_maxs = [g["max_unresolved_adv"] for g in groups if g.get("max_unresolved_adv") is not None]
    min_resolved_adv = min(res_mins) if res_mins else None
    max_unresolved_adv = max(fail_maxs) if fail_maxs else None
    polarity_ok = (
        zero_adv == 0
        and active_eq_mixed
        and (max_unresolved_adv is not None and max_unresolved_adv < -margin + 1e-12)
        and (min_resolved_adv is not None and min_resolved_adv >= margin - 1e-12)
    )

    # Active groups must all carry ≥1 non-spiral oracle pass and ≥1 fail.
    all_positive_anchor = all(g["n_resolved"] >= 1 for g in groups) if groups else False
    all_negative_anchor = all(g.get("n_failed", 0) >= 1 for g in groups) if groups else False

    reward_vals = [float(r["scores"]["R"]) for r in rollouts]
    mask = dry_run_mask_check(cfg)

    # Edit-window coverage on active GRPO groups (head+tail; P6.5 surgical).
    max_windows = int(cfg.get("max_windows_per_row", 2))
    allow_mid = bool(cfg.get("allow_mid_edit_windows", False))
    do_mask_super = bool(cfg.get("mask_superseded_edits", True))
    edit_cov: dict[str, Any] = {
        "edit_total": 0,
        "edit_fully_in_window": 0,
        "edit_truncated_beyond": 0,
        "edit_in_window_pct": None,
        "final_winning_edits_in_window_pct": None,
        "superseded_failed_edits_supervised": 0,
        "n_rollouts_checked": 0,
        "n_groups": len(groups),
        "windowing": "header_tail" if not allow_mid else "header_tail_plus_mid_edit",
        "max_seq_length": int(cfg["max_seq_length"]),
        "max_windows_per_row": max_windows,
        "mask_superseded_edits": do_mask_super,
    }
    try:
        model_dir = resolve_model_path(cfg)
        backend, tok = load_tokenizer_backend(model_dir)
        max_seq = int(cfg["max_seq_length"])
        et = ei = etr = 0
        fw_tot = fw_in = 0
        super_sup = 0
        n_checked = 0
        n_windows = 0
        for g in groups:
            for meta_path in g.get("meta_paths") or []:
                run_dir = Path(meta_path).parent if meta_path else Path()
                if not run_dir.is_dir():
                    continue
                rendered = _render_run_any(run_dir, mask_tool_results=True)
                if not rendered or not rendered.get("messages"):
                    continue
                norm = normalize_messages_for_gemma(rendered["messages"])
                text = apply_chat_template(norm, model_dir)
                ids = encode_text(backend, tok, text)
                labels = build_labels_assistant_only(text, ids, backend, tok)
                from train_qlora import prepare_surgical_labels_and_windows

                labels, wins, detailed, _ms = prepare_surgical_labels_and_windows(
                    text,
                    ids,
                    labels,
                    backend,
                    tok,
                    max_seq,
                    mask_superseded=do_mask_super,
                    max_windows_per_row=max_windows,
                    allow_mid_edit_windows=allow_mid,
                )
                edits = find_edit_token_spans(text, backend, tok)
                cov = edit_coverage_for_windows(edits, wins)
                finals = [
                    {"name": e["name"], "start": e["tok_start"], "end": e["tok_end"]}
                    for e in detailed
                    if e.get("role") == "final_winning"
                ]
                fcov = edit_coverage_for_windows(finals, wins)
                supers = [e for e in detailed if e.get("superseded")]
                for e in supers:
                    a, b = int(e["tok_start"]), int(e["tok_end"])
                    for i in range(max(0, a), min(b, len(labels))):
                        if labels[i] != IGNORE_INDEX:
                            super_sup += 1
                et += cov["edit_total"]
                ei += cov["edit_fully_in_window"]
                etr += cov["edit_truncated_beyond"]
                fw_tot += len(finals)
                fw_in += int(fcov["edit_fully_in_window"])
                n_checked += 1
                n_windows += len(wins)
        edit_cov.update(
            {
                "edit_total": et,
                "edit_fully_in_window": ei,
                "edit_truncated_beyond": etr,
                "edit_in_window_pct": round(100.0 * ei / et, 2) if et else 100.0,
                "final_winning_edits_total": fw_tot,
                "final_winning_edits_in_window": fw_in,
                "final_winning_edits_in_window_pct": (
                    round(100.0 * fw_in / fw_tot, 2) if fw_tot else 100.0
                ),
                "superseded_failed_edits_supervised": super_sup,
                "n_rollouts_checked": n_checked,
                "n_windows": n_windows,
            }
        )
    except Exception as exc:  # noqa: BLE001
        edit_cov["error"] = str(exc)

    # Cross-directory pairing evidence: tasks with both bon + star metas
    by_task_roots: dict[str, set[str]] = defaultdict(set)
    for r in rollouts:
        by_task_roots[r["task_id"]].add(r.get("source_root") or "")
    cross_dir_tasks = sorted(
        tid
        for tid, rs in by_task_roots.items()
        if len(rs) > 1
        and any("bon" in x for x in rs)
        and any("star" in x for x in rs)
    )

    focus = {
        g["task_id"]: {
            "n_resolved": g["n_resolved"],
            "n_failed": g["n_failed"],
            "n_unique_trajectories": g.get("n_unique_trajectories"),
            "has_duplicate_trajectories": g.get("has_duplicate_trajectories"),
            "resolved_flags": g["resolved_flags"],
            "advantage": g["advantage"],
            "source_roots": g.get("source_roots"),
        }
        for g in groups
        if g["task_id"] in {"fastapi_15280", "rich_4077"}
    }

    report = {
        "dry_run": True,
        "base_model": cfg["base_model"],
        "peft_type": cfg["peft_type"],
        "rank": cfg["rank"],
        "group_size": cfg["group_size"],
        "beta": cfg["beta"],
        "max_seq_length": cfg["max_seq_length"],
        "traces_roots": [str(r) for r in roots],
        "n_rollouts": len(rollouts),
        "n_tasks": len({r["task_id"] for r in rollouts}),
        "n_active_groups": len(groups),
        "n_groups": len(groups),  # alias
        "n_groups_with_variance": len(groups),
        "filter": filt,
        "failed_rollouts_with_positive_adv": failed_pos,
        "zero_pass_groups_active": zero_pass_active,
        "n_mixed_source_groups": mixed_sources,
        "groups_with_duplicate_trajectories": dup_groups,
        "no_duplicate_trajectories": no_dups,
        "zero_adv_rollouts": zero_adv,
        "advantage_margin": margin,
        "max_loop_pairs_positive": max_loop,
        "n_spiral_resolved_excluded": int(filt.get("n_spiral_resolved_excluded") or 0),
        "positive_anchors_with_loop_gt_2": positive_anchors_loop_gt,
        "fastapi_14455_active_group": fastapi_14455_active,
        "min_resolved_adv": min_resolved_adv,
        "max_unresolved_adv": max_unresolved_adv,
        "n_mixed_polarity_groups": len(mixed_groups),
        "n_active_eq_n_mixed": active_eq_mixed,
        "all_pass_groups_active": all_pass_active,
        "polarity_margin_ok": polarity_ok,
        "cross_dir_bon_star_tasks": cross_dir_tasks,
        "focus_groups": focus,
        "reward_summary": {
            "min": min(reward_vals) if reward_vals else 0.0,
            "median": statistics.median(reward_vals) if reward_vals else 0.0,
            "max": max(reward_vals) if reward_vals else 0.0,
            "mean": statistics.mean(reward_vals) if reward_vals else 0.0,
            "n_resolved": sum(1 for r in rollouts if r["resolved"]),
            "n_resolved_spiral": sum(
                1
                for r in rollouts
                if r["resolved"] and int(r.get("loop_pairs") or 0) > max_loop
            ),
        },
        "advantage_check": {
            "all_active_have_positive_anchor": all_positive_anchor,
            "all_active_have_negative_anchor": all_negative_anchor,
            "n_active_eq_n_mixed": active_eq_mixed,
            "failed_rollouts_with_positive_adv": failed_pos,
            "orm_strict_dominance": failed_pos == 0,
            "no_duplicate_trajectories": no_dups,
            "zero_adv_rollouts": zero_adv,
            "positive_anchors_with_loop_gt_2": positive_anchors_loop_gt,
            "fastapi_14455_active_group": fastapi_14455_active,
            "min_resolved_adv": min_resolved_adv,
            "max_unresolved_adv": max_unresolved_adv,
            "polarity_margin_ok": polarity_ok,
            "sample_groups": groups[:5],
        },
        "mask": mask,
        "edit_coverage": edit_cov,
        "no_prm": True,
        # P6.5: head+tail windowing intentionally drops mid-trajectory thrash edits
        # from coverage; gate on surgical finals (100% in-window) + no superseded CE.
        "pass": bool(groups)
        and all_positive_anchor
        and all_negative_anchor
        and active_eq_mixed
        and failed_pos == 0
        and zero_pass_active == 0
        and all_pass_active == 0
        and no_dups
        and polarity_ok
        and positive_anchors_loop_gt == 0
        and (mask.get("pass", True) is True)
        and edit_cov.get("error") is None
        and float(edit_cov.get("final_winning_edits_in_window_pct") or 0) >= 100.0
        and int(edit_cov.get("superseded_failed_edits_supervised") or 0) == 0
        and int(edit_cov.get("n_rollouts_checked") or 0) >= 1,
    }
    return report


def _render_run_any(run_dir: Path, *, mask_tool_results: bool = True) -> dict[str, Any] | None:
    """Like render_trajectories.render_run but keeps unresolved (needed for GRPO negatives)."""
    from render_trajectories import _assistant_content, _tool_content  # type: ignore

    meta_p = run_dir / "meta.json"
    trace_p = _find_trace(run_dir)
    if trace_p is None:
        return None
    try:
        trace = json.loads(trace_p.read_text())
        meta = json.loads(meta_p.read_text()) if meta_p.is_file() else {}
    except json.JSONDecodeError:
        return None
    messages: list[dict[str, Any]] = []
    for step in trace.get("steps") or []:
        src = (step.get("source") or "").lower()
        if src in ("system", "user"):
            content = (step.get("message") or "").strip()
            if content:
                messages.append({"role": src, "content": content})
            continue
        if src != "agent":
            continue
        asst = _assistant_content(step)
        if asst:
            messages.append({"role": "assistant", "content": asst})
        if step.get("observation") is not None or (step.get("tool_calls") or []):
            messages.append(
                {
                    "role": "tool",
                    "content": _tool_content(step, mask=mask_tool_results),
                }
            )
    if len(messages) < 2:
        return None
    tid = str(meta.get("task_id") or trace.get("task_id") or run_dir.name)
    return {"task_id": tid, "messages": messages, "meta": meta}


def _grpo_trace_roots(cfg: dict[str, Any]) -> list[Path]:
    roots_cfg = cfg.get("traces_roots") or []
    roots: list[Path] = []
    env_root = os.environ.get("GRPO_TRACES")
    if env_root:
        for part in env_root.split(":"):
            part = part.strip()
            if part:
                roots.append(Path(part))
    for r in roots_cfg:
        p = Path(r)
        if not p.is_absolute():
            p = _repo_root() / p
        if p not in roots:
            roots.append(p)
    return roots


def train_gpu(cfg: dict[str, Any], out_dir: Path) -> None:
    """Offline GRPO: advantage-weighted CE on grouped rollouts (ORM+heuristics, G13).

    Requires prior DoRA SFT adapter via env SFT_ADAPTER or cfg['sft_adapter'].
    Uses existing BoN/STaR metas (no online sampling). KL via beta toward SFT init.
    """
    try:
        import torch
        from peft import PeftModel, prepare_model_for_kbit_training
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    except ImportError as exc:
        raise SystemExit(
            f"GPU GRPO imports missing ({exc}). Install transformers peft bitsandbytes."
        ) from exc

    if not torch.cuda.is_available():
        raise SystemExit("GPU GRPO requires CUDA")

    report = dry_run(cfg)
    if not report.get("pass"):
        raise SystemExit(f"GRPO dry-run gate failed before GPU train: pass={report.get('pass')}")

    roots = _grpo_trace_roots(cfg)
    rollouts = iter_rollout_metas(roots)
    for row in rollouts:
        row["scores"] = score_rollout(row, cfg["reward"])
    active, filt = group_advantages(
        rollouts,
        int(cfg.get("group_size", 4)),
        require_positive_in_group=bool(cfg.get("require_positive_in_group", True)),
        require_negative_in_group=bool(cfg.get("require_negative_in_group", True)),
        require_reward_variance=bool(cfg.get("require_reward_variance", True)),
        clamp_unresolved_advantage=bool(cfg.get("clamp_unresolved_advantage", True)),
        enforce_polarity_margin=bool(cfg.get("enforce_polarity_margin", True)),
        advantage_margin=float(cfg.get("advantage_margin", 0.05)),
        max_loop_pairs_positive=int(cfg.get("max_loop_pairs_positive", 2)),
        max_edits_positive=int(cfg.get("max_edits_positive", 8)),
        max_patch_chars_positive=int(cfg.get("max_patch_chars_positive", 4500)),
    )
    by_meta = {str(r.get("meta_path")): r for r in rollouts}
    train_items: list[dict[str, Any]] = []
    for g in active:
        for meta_path, adv in zip(g.get("meta_paths") or [], g.get("advantage") or []):
            row = by_meta.get(str(meta_path))
            if not row:
                continue
            adv_f = float(adv)
            if abs(adv_f) < 1e-8:
                continue
            run_dir = Path(row.get("run_dir") or "")
            if not run_dir.is_dir():
                continue
            rendered = _render_run_any(run_dir, mask_tool_results=True)
            if not rendered:
                continue
            train_items.append(
                {
                    "task_id": row["task_id"],
                    "messages": rendered["messages"],
                    "advantage": adv_f,
                    "resolved": bool(row.get("resolved")),
                    "run_dir": str(run_dir),
                }
            )
    if not train_items:
        raise SystemExit("GRPO GPU train: no advantage-weighted trajectories rendered")

    model_dir = resolve_model_path(cfg)
    sft_adapter = (cfg.get("sft_adapter") or os.environ.get("SFT_ADAPTER") or "").strip()
    if not sft_adapter or not Path(sft_adapter).exists():
        raise SystemExit("SFT_ADAPTER / cfg.sft_adapter required (path to P5.6 DoRA adapter)")

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    from transformers import AutoConfig

    hf_cfg = AutoConfig.from_pretrained(str(model_dir), trust_remote_code=True)
    qcfg = getattr(hf_cfg, "quantization_config", None)
    is_ct = False
    if qcfg is not None:
        qname = str(getattr(qcfg, "quant_method", qcfg) or "")
        is_ct = "compressed" in qname.lower() or "compressed-tensors" in str(type(qcfg)).lower()
        if isinstance(qcfg, dict):
            is_ct = is_ct or "compressed" in str(qcfg.get("quant_method", "")).lower()
    load_kwargs: dict[str, Any] = {
        "device_map": "auto",
        "trust_remote_code": True,
        "torch_dtype": torch.bfloat16,
        "attn_implementation": str(cfg.get("attn_implementation") or "sdpa"),
    }
    if not is_ct and bool(cfg.get("load_in_4bit", True)):
        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
        )
    from train_qlora import (
        install_chunked_causal_lm_loss,
        patch_ct_pack_quantized_forward,
        patch_peft_for_gemma4_clippable,
        replace_gemma4_clippable_with_nn_linear_proxies,
    )

    print(f"GRPO_LOAD_MODEL ct={is_ct} path={model_dir}", flush=True)
    base = AutoModelForCausalLM.from_pretrained(str(model_dir), **load_kwargs)

    # Drop vision before PEFT inject — SFT adapter targets q_proj/… leaf names that
    # also exist under Gemma4ClippableLinear in the vision tower.
    def _drop_vision(root: Any, label: str) -> int:
        dropped = 0
        for attr in (
            "vision_tower",
            "vision_model",
            "multi_modal_projector",
            "mm_projector",
            "vision_projector",
        ):
            if hasattr(root, attr) and getattr(root, attr) is not None:
                try:
                    setattr(root, attr, None)
                    dropped += 1
                    print(f"GRPO_DROP_VISION {label}.{attr}", flush=True)
                except Exception as exc:
                    print(f"GRPO_DROP_VISION_FAIL {label}.{attr} err={exc}", flush=True)
        return dropped

    n_drop = _drop_vision(base, "base")
    if hasattr(base, "model"):
        n_drop += _drop_vision(base.model, "base.model")
    print(f"GRPO_DROP_VISION_TOTAL n={n_drop}", flush=True)
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass

    if is_ct:
        patch_ct_pack_quantized_forward(base)
        install_chunked_causal_lm_loss(base, chunk_size=int(cfg.get("ce_loss_chunk_size", 256)))
    if is_ct or any(type(m).__name__ == "Gemma4ClippableLinear" for _, m in base.named_modules()):
        replace_gemma4_clippable_with_nn_linear_proxies(base)
        patch_peft_for_gemma4_clippable()
    if not is_ct:
        base = prepare_model_for_kbit_training(base)
    try:
        model = PeftModel.from_pretrained(base, sft_adapter, is_trainable=True)
    except TypeError:
        model = PeftModel.from_pretrained(base, sft_adapter)
        for n, p in model.named_parameters():
            if "lora_" in n:
                p.requires_grad = True
    if bool(cfg.get("gradient_checkpointing", True)):
        model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False}
        )
        if hasattr(model, "config"):
            model.config.use_cache = False

    # 48GB CT+LoRA: keep GRPO seq aligned with SFT fit unless overridden.
    max_seq = int(cfg.get("max_seq_length", 4096))
    lr = float(cfg.get("learning_rate", 1e-5))
    beta = float(cfg.get("beta", 0.04))
    epochs = float(cfg.get("num_train_epochs", 1))
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr)
    device = next(model.parameters()).device

    losses: list[float] = []
    model.train()
    steps = 0
    for _ep in range(max(1, int(epochs))):
        for item in train_items:
            norm = normalize_messages_for_gemma(item["messages"])
            text = apply_chat_template(norm, model_dir)
            full_ids = encode_text("transformers", tokenizer, text)
            labels_full = build_labels_assistant_only(
                text, full_ids, "transformers", tokenizer
            )
            from train_qlora import prepare_surgical_labels_and_windows

            labels_full, wins, _detailed, _ms = prepare_surgical_labels_and_windows(
                text,
                full_ids,
                labels_full,
                "transformers",
                tokenizer,
                max_seq,
                mask_superseded=bool(cfg.get("mask_superseded_edits", True)),
                max_windows_per_row=int(cfg.get("max_windows_per_row", 2)),
                allow_mid_edit_windows=bool(cfg.get("allow_mid_edit_windows", False)),
            )
            w_adv = float(item["advantage"])
            for win in wins:
                ids = win["input_ids"]
                labels = win["labels"]
                if sum(1 for x in labels if x != IGNORE_INDEX) < 1:
                    continue
                input_ids = torch.tensor([ids], device=device)
                lab = torch.tensor([labels], device=device)
                out = model(input_ids=input_ids, labels=lab)
                # Advantage-weighted CE: maximize likelihood of +adv, minimize -adv.
                loss = out.loss * (-w_adv) * (1.0 + beta)
                if not torch.isfinite(loss):
                    continue
                opt.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), float(cfg.get("max_grad_norm", 1.0))
                )
                opt.step()
                losses.append(float(loss.detach().cpu()))
                steps += 1
                if steps % 5 == 0 or steps == 1:
                    print(
                        f"GRPO_STEP {steps} loss={losses[-1]:.4f} adv={w_adv:.3f} "
                        f"task={item['task_id']} win={win.get('kind')} ntok={len(ids)}",
                        flush=True,
                    )

    out_dir.mkdir(parents=True, exist_ok=True)
    save_meta = save_peft_adapter_bf16(model, out_dir)
    tokenizer.save_pretrained(str(out_dir))
    peak_vram = None
    try:
        peak_vram = float(torch.cuda.max_memory_allocated() / (1024**3))
    except Exception:
        pass
    meta = {
        "base_model": cfg.get("base_model"),
        "sft_adapter": sft_adapter,
        "peft_type": cfg.get("peft_type"),
        "rank": cfg.get("rank"),
        "beta": beta,
        "n_train_items": len(train_items),
        "n_active_groups": len(active),
        "filter": {
            "zero_adv_rollouts": filt.get("zero_adv_rollouts"),
            "positive_anchors_with_loop_gt_2": filt.get("positive_anchors_with_loop_gt_2"),
        },
        "n_steps": steps,
        "max_seq_length": max_seq,
        "windowing": (
            "header_tail"
            if not bool(cfg.get("allow_mid_edit_windows", False))
            else "header_tail_plus_mid_edit"
        ),
        "mask_superseded_edits": bool(cfg.get("mask_superseded_edits", True)),
        "max_windows_per_row": int(cfg.get("max_windows_per_row", 2)),
        "adapter_save": save_meta,
        "train_loss_start": losses[0] if losses else None,
        "train_loss_end": losses[-1] if losses else None,
        "train_loss_mean": sum(losses) / len(losses) if losses else None,
        "peak_vram_gb": peak_vram,
        "no_prm": True,
        "method": "offline_advantage_weighted_ce",
    }
    (out_dir / "train_grpo_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"GRPO_ADAPTER_SAVED {out_dir} steps={steps}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="P5.10–13 GRPO (agent-dev-spec §5, G13)")
    ap.add_argument("--config", type=Path, required=True, help="configs/train_grpo.yaml")
    ap.add_argument("--out", type=Path, required=True, help="agent/adapters/<name>")
    ap.add_argument("--dry-run", action="store_true", help="CPU reward/advantage/mask check")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = validate_grpo_config(load_config(args.config))
    if args.dry_run:
        report = dry_run(cfg)
        # Trim huge sample_groups patch noise already absent
        print(json.dumps(report, indent=2))
        print(f"GRPO_DRY_RUN_PASS={report['pass']}", flush=True)
        return 0 if report["pass"] else 2
    out_dir = args.out if args.out.is_absolute() else Path.cwd() / args.out
    train_gpu(cfg, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
