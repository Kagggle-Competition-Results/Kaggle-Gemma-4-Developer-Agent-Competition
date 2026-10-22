#!/usr/bin/env python3
"""P5.6c / P6.1 — 10-task smoke with harvest_agent_p314a + LoRA master_swe_adapter (Plan B).

G4: evaluates ONLY configs/splits.json smoke IDs (never holdout).
G5: uses working agent copy under P31_ROOT/agent — does not edit frozen gemma4-agent/agent/.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import litellm
import pandas as pd
import torch
import yaml
from adk_submission import VllmConfig, VllmServer, discover_adapters
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps._configs import EventsCompactionConfig
from swegemma.config import ALLOWED_ADAPTER_EXTENSIONS, EvalConfig, build_submission_limits
from swegemma.evaluate import Evaluator
from swegemma.models import load_tasks
from swegemma.models.discovery import validate_single_declared_model


def run_sync(coro_or_fn, *args, **kwargs):
    fn = (lambda: coro_or_fn(*args, **kwargs)) if callable(coro_or_fn) else (lambda: coro_or_fn)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(fn())).result()
    return asyncio.run(fn())


def _extract_commands_from_trace_obj(obj: object) -> list[str]:
    """Pull full run_command strings from structured trace JSON (no quote truncation)."""
    cmds: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            name = node.get("function_name") or node.get("name") or ""
            args = node.get("arguments")
            if name in {"run_command", "bash", "shell", "execute"}:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"command": args}
                if isinstance(args, dict):
                    cmd = str(args.get("command") or args.get("cmd") or "").strip()
                    if len(cmd) >= 8:
                        cmds.append(cmd)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return cmds


def _extract_commands_from_json_text(blob: str) -> list[str]:
    """JSON-string aware fallback: decode escaped quotes inside \"command\" values."""
    cmds: list[str] = []
    for m in re.finditer(r'"command"\s*:\s*"((?:\\.|[^"\\])*)"', blob):
        raw = m.group(1)
        try:
            cmd = json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            cmd = (
                raw.replace(r"\"", '"')
                .replace(r"\\", "\\")
                .replace(r"\n", "\n")
                .replace(r"\t", "\t")
            )
        cmd = str(cmd).strip()
        if len(cmd) >= 8:
            cmds.append(cmd)
    return cmds


def _extract_structured_tool_names(obj: object) -> list[str]:
    """Collect tool/function names from structured ADK/SWE traces (not free-text)."""
    names: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            # Common shapes: {name}, {function_name}, {tool_name}, {function:{name}}, {tool_calls:[{name}]}
            for key in ("function_name", "tool_name"):
                v = node.get(key)
                if isinstance(v, str) and v.strip():
                    names.append(v.strip())
            n = node.get("name")
            if isinstance(n, str) and n.strip():
                # Only treat as a tool name when sibling args / tool_call context present,
                # or when name is a known host tool (avoids counting unrelated "name" fields).
                known = {
                    "edit_file",
                    "write_file",
                    "submit_patch",
                    "run_command",
                    "read_file",
                    "get_status",
                    "search_replace",
                    "list_dir",
                    "grep",
                    "bash",
                    "shell",
                    "execute",
                }
                if n in known or "arguments" in node or "args" in node or node.get("type") in {
                    "function_call",
                    "tool_call",
                    "functionCall",
                }:
                    names.append(n.strip())
            fn = node.get("function") or node.get("functionCall") or node.get("function_call")
            if isinstance(fn, dict):
                fn_name = fn.get("name")
                if isinstance(fn_name, str) and fn_name.strip():
                    names.append(fn_name.strip())
            tcs = node.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    walk(tc)
            for v in node.values():
                if v is tcs:
                    continue
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return names


def _trace_kpis(results_dir: Path, task_id: str) -> dict:
    """Best-effort failed_edit / loop signals from per-task traces.

    loop_pairs = # of distinct full shell command strings that appear >=3 times.
    Prefer structured JSON traces so quoted grep patterns are not truncated at `"`.

    P6.10: n_edit_calls / n_submit / n_tool_calls count structured assistant tool
    invocations only (edit_file|write_file etc. in JSON tool_call objects) — NOT
    raw substring matches across logs (which inflated KPIs ~3× in P6.8).
    """
    from collections import Counter

    out = {
        "n_edit_calls": 0,
        "n_submit": 0,
        "n_tool_calls": 0,
        "loop_pairs": 0,
        "kpi_mode": "structured_tool_calls",
    }
    candidates = list(results_dir.rglob(f"*{task_id}*"))
    commands: list[str] = []
    tool_names: list[str] = []
    for p in candidates[:20]:
        if not p.is_file() or p.suffix not in {".json", ".jsonl", ".log", ".txt"}:
            continue
        try:
            text = p.read_text(errors="ignore")[:200000]
        except OSError:
            continue
        if p.suffix == ".json":
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                commands.extend(_extract_commands_from_json_text(text))
            else:
                commands.extend(_extract_commands_from_trace_obj(obj))
                tool_names.extend(_extract_structured_tool_names(obj))
        elif p.suffix == ".jsonl":
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    commands.extend(_extract_commands_from_json_text(line))
                else:
                    commands.extend(_extract_commands_from_trace_obj(obj))
                    tool_names.extend(_extract_structured_tool_names(obj))
        else:
            # logs: still extract shell commands for loop_pairs; do NOT regex-count edits
            commands.extend(_extract_commands_from_json_text(text))

    mutating = {"edit_file", "write_file"}
    out["n_edit_calls"] = sum(1 for n in tool_names if n in mutating)
    out["n_submit"] = sum(1 for n in tool_names if n == "submit_patch")
    out["n_tool_calls"] = len(tool_names)
    if commands:
        c = Counter(commands)
        out["loop_pairs"] = sum(1 for _, n in c.items() if n >= 3)
    return out


def main() -> int:
    root = Path(os.environ.get("P31_ROOT", "/workspace/p56c_smoke"))
    agent_dir = root / "agent"
    data_dir = root / "data"
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    model_path = Path(
        os.environ.get(
            "MODEL_PATH",
            "/models/gemma-4-31B-it-qat-w4a16-ct",
        )
    )
    splits = json.loads((root / "configs" / "splits.json").read_text())
    smoke_ids = set(splits["smoke"])
    holdout = set(splits.get("holdout") or [])
    if smoke_ids & holdout:
        raise SystemExit(f"G4 violation: smoke∩holdout={sorted(smoke_ids & holdout)}")

    litellm.drop_params = True
    declared = validate_single_declared_model(agent_dir)
    target = os.environ.get("SMOKE_TARGET_MODEL", declared)
    adapters = discover_adapters(str(agent_dir), adapter_extensions=ALLOWED_ADAPTER_EXTENSIONS)
    print("adapters_discovered", adapters, flush=True)
    gpu_count = torch.cuda.device_count() if torch.cuda.is_available() else 1
    tp = 4 if gpu_count >= 4 else (2 if gpu_count >= 2 else 1)
    print("declared", declared, "target", target, "tp", tp, "model", model_path, flush=True)

    vllm_cfg = VllmConfig(
        model=str(model_path),
        port=8000,
        host="127.0.0.1",
        tool_call_parser="gemma4",
        reasoning_parser="gemma4",
        max_model_len=32768,
        dtype="bfloat16"
        if (torch.cuda.is_available() and torch.cuda.is_bf16_supported())
        else "auto",
        gpu_memory_utilization=0.90,
        enable_auto_tool_choice=True,
        enable_lora=True,
        max_loras=8,
        max_lora_rank=128,
        tensor_parallel_size=tp,
        startup_timeout=60 * 20,
    )
    os.environ.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")
    server = VllmServer(vllm_cfg, adapter_manifest=adapters)
    t_load = time.time()
    server.start()
    load_s = round(time.time() - t_load, 2)
    print("vLLM", server.base_url, "load_s", load_s, flush=True)
    models = server.create_model_registry(
        aliases=[declared, target],
        model_prefix="openai/",
        api_key="EMPTY",
    )

    tasks_all = load_tasks(data_dir / "tasks.jsonl")
    smoke_tasks = [t for t in tasks_all if t.instance_id in smoke_ids]
    assert len(smoke_tasks) == 10, [t.instance_id for t in smoke_tasks]

    raw = yaml.safe_load((agent_dir / "eval_config.yaml").read_text())
    ev = raw.get("evaluation", raw)
    timeout_seconds = int(ev.get("timeout_seconds", 300))
    max_tool_calls = int(ev.get("max_tool_calls", 60))
    max_time_minutes = float(ev.get("max_time_minutes", 5))
    max_turns = int(ev.get("max_turns", 80)) if ev.get("max_turns") is not None else None
    print("budgets", timeout_seconds, max_tool_calls, max_time_minutes, max_turns, flush=True)
    _limits, _gen = build_submission_limits()

    eval_config = EvalConfig(
        tasks_path=data_dir / "tasks.jsonl",
        snapshots_dir=data_dir / "snapshots",
        results_dir=results,
        submission_dir=agent_dir,
        models=models,
        sandbox="subprocess",
        timeout_seconds=timeout_seconds,
        max_time_minutes=max_time_minutes,
        max_tool_calls=max_tool_calls,
        max_turns=max_turns,
        graph_dir=str(data_dir / "graphs"),
        embeddings_dir=str(data_dir / "embeddings"),
        context_cache_config=ContextCacheConfig(
            min_tokens=2048, ttl_seconds=1800, cache_intervals=10
        ),
        events_compaction_config=EventsCompactionConfig(
            compaction_interval=5,
            overlap_size=2,
            token_threshold=14336,
            event_retention_size=5,
        ),
    )
    evaluator = Evaluator(eval_config)
    rows = []
    t0 = time.time()
    for i, task in enumerate(smoke_tasks, 1):
        print(f"=== [{i}/{len(smoke_tasks)}] {task.instance_id} ===", flush=True)
        ts = time.time()
        try:
            result = run_sync(evaluator.evaluate_task, task)
            resolved = bool(
                getattr(result, "resolved", False) or getattr(result, "success", False)
            )
            patch = getattr(result, "agent_patch", "") or ""
            kpis = _trace_kpis(results, task.instance_id)
            failed_edit = (not resolved) and kpis["n_edit_calls"] == 0 and kpis["n_submit"] == 0
            row = {
                "task_id": task.instance_id,
                "resolved": resolved,
                "patch_chars": len(patch),
                "seconds": round(time.time() - ts, 2),
                "error": str(getattr(result, "error", "") or "")[:300],
                "failed_edit": failed_edit,
                "loop_pairs": kpis["loop_pairs"],
                "n_edit_calls": kpis["n_edit_calls"],
                "n_submit": kpis["n_submit"],
            }
        except Exception as e:  # noqa: BLE001 — per-task isolation
            row = {
                "task_id": task.instance_id,
                "resolved": False,
                "patch_chars": 0,
                "seconds": round(time.time() - ts, 2),
                "error": str(e)[:300],
                "failed_edit": True,
                "loop_pairs": 0,
                "n_edit_calls": 0,
                "n_submit": 0,
            }
        print(row, flush=True)
        rows.append(row)

    summary = pd.DataFrame(rows)
    summary.to_csv(results / "smoke_summary.csv", index=False)
    n = len(summary)
    r = int(summary.resolved.sum())
    wall = round(time.time() - t0, 2)
    mean_s = float(summary.seconds.mean()) if n else 0.0
    failed_edit_rate = float(summary.failed_edit.mean()) if n else 0.0
    loop_rate = float((summary.loop_pairs > 0).mean()) if n else 0.0
    out = {
        "ticket": os.environ.get("SMOKE_TICKET", "P6.1-p56c"),
        "budget_id": os.environ.get("SMOKE_BUDGET_ID", "community_budget_fit"),
        "ablation": "p56c_master_swe_adapter_v2_100pct_edit_window",
        "split": "smoke",
        "n": n,
        "resolved": r,
        "pass_at_1": round(r / n, 4) if n else 0.0,
        "failed_edit_rate": round(failed_edit_rate, 4),
        "loop_rate": round(loop_rate, 4),
        "mean_seconds": round(mean_s, 2),
        "extrapolated_hours_120": round(mean_s * 120 / 3600, 3),
        "wall_s": wall,
        "load_s": load_s,
        "sku": os.environ.get("P31_SKU", "unknown"),
        "adapter_plan": "B_enable_lora",
        "adapter_path": "agent/adapters/master_swe_adapter",
        "baseline_ref": "P3.14a_public_0.18_orig_smoke_0.20",
        "hard_crash_rate": 0.0,
        "task_ids": list(summary.task_id),
        "g4_holdout_untouched": True,
        "budgets": {
            "timeout_seconds": timeout_seconds,
            "max_tool_calls": max_tool_calls,
            "max_time_minutes": max_time_minutes,
            "max_turns": max_turns,
        },
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (results / "smoke.json").write_text(json.dumps(out, indent=2) + "\n")
    print(summary.to_string(index=False), flush=True)
    print(json.dumps(out, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
