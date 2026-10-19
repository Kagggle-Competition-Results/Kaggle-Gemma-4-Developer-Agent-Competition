#!/usr/bin/env python3
"""P3.1 — smoke eval with current agent/eval_config.yaml budgets (community_budget_fit)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
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


def main() -> int:
    root = Path(os.environ.get("P31_ROOT", "/workspace/gemma4-p31"))
    agent_dir = root / "agent"
    data_dir = root / "data"
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    model_path = Path(
        os.environ.get(
            "MODEL_PATH",
            "/workspace/models/gemma-4-31B-it-qat-w4a16-ct",
        )
    )
    splits = json.loads((root / "configs" / "splits.json").read_text())
    smoke_ids = set(splits["smoke"])

    litellm.drop_params = True
    declared = validate_single_declared_model(agent_dir)
    # Alias registry must match the submitted agent.yaml model (31b default or 12b A/B).
    target = os.environ.get("SMOKE_TARGET_MODEL", declared)
    adapters = discover_adapters(str(agent_dir), adapter_extensions=ALLOWED_ADAPTER_EXTENSIONS)
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
            row = {
                "task_id": task.instance_id,
                "resolved": resolved,
                "patch_chars": len(patch),
                "seconds": round(time.time() - ts, 2),
                "error": str(getattr(result, "error", "") or "")[:300],
            }
        except Exception as e:  # noqa: BLE001 — per-task isolation
            row = {
                "task_id": task.instance_id,
                "resolved": False,
                "patch_chars": 0,
                "seconds": round(time.time() - ts, 2),
                "error": str(e)[:300],
            }
        print(row, flush=True)
        rows.append(row)

    summary = pd.DataFrame(rows)
    summary.to_csv(results / "smoke_summary.csv", index=False)
    n = len(summary)
    r = int(summary.resolved.sum())
    wall = round(time.time() - t0, 2)
    mean_s = float(summary.seconds.mean()) if n else 0.0
    out = {
        "ticket": os.environ.get("SMOKE_TICKET", "P3.1"),
        "budget_id": os.environ.get("SMOKE_BUDGET_ID", "community_budget_fit"),
        "ablation": os.environ.get("SMOKE_ABLATION", ""),
        "split": "smoke",
        "n": n,
        "resolved": r,
        "pass_at_1": round(r / n, 4) if n else 0.0,
        "mean_seconds": round(mean_s, 2),
        "extrapolated_hours_120": round(mean_s * 120 / 3600, 3),
        "wall_s": wall,
        "load_s": load_s,
        "sku": os.environ.get("P31_SKU", "unknown"),
        "hard_crash_rate": 0.0,
        "task_ids": list(summary.task_id),
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
