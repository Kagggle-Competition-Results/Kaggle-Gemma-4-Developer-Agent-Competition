#!/usr/bin/env python3
"""P5.1b — STaR recovery (agent-dev-spec §5).

One retry after truncating verifier/stderr to last --error-tail-lines (G11).
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import dataclasses
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import litellm
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


def parse_tasks(arg: str) -> list[str]:
    p = Path(arg)
    if p.is_file():
        text = p.read_text().strip()
        if p.suffix == ".json":
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x) for x in data]
            if isinstance(data, dict) and "tasks" in data:
                return [str(x) for x in data["tasks"]]
        return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return [t.strip() for t in arg.split(",") if t.strip()]


def tail_lines(text: str, n: int) -> str:
    lines = (text or "").splitlines()
    if len(lines) <= n:
        return text or ""
    return "\n".join(lines[-n:])


def result_diag(result: object) -> dict:
    err = getattr(result, "error", None) or getattr(result, "error_message", None) or ""
    out = str(getattr(result, "test_output", "") or "")
    return {
        "resolved": bool(
            getattr(result, "resolved", False) or getattr(result, "success", False)
        ),
        "patch": getattr(result, "agent_patch", "") or "",
        "error": str(err),
        "test_exit_code": getattr(result, "test_exit_code", None),
        "test_output_tail": out[-1500:] if out else "",
    }


def star_feedback_blob(diag: dict) -> str:
    """Prefer verifier output; timeout-only strings are weak STaR signal."""
    parts: list[str] = []
    err = str(diag.get("error") or "").strip()
    out = str(diag.get("test_output_tail") or "").strip()
    if err:
        parts.append(err)
    if out and out not in err:
        parts.append(out)
    return "\n".join(parts)


def task_with_error_tail(task: object, error_tail: str) -> object:
    """Inject G11 error tail into problem_statement (Task is a frozen dataclass)."""
    if not error_tail.strip():
        return task
    block = (
        "\n\n## Previous attempt feedback (truncated; fix these failures)\n"
        "```\n"
        f"{error_tail.rstrip()}\n"
        "```\n"
        "Re-diagnose and submit a corrected patch. Do not repeat the same failing edit.\n"
    )
    base = getattr(task, "problem_statement", "") or ""
    marker = "## Previous attempt feedback"
    if marker in base:
        base = base.split(marker, 1)[0].rstrip()
    return dataclasses.replace(task, problem_statement=base + block)


def preflight_subprocess_host() -> None:
    host_python = os.environ.get("HOST_PYTHON", "python3")
    for mod in ("pytest", "inline_snapshot", "dirty_equals"):
        probe = subprocess.run(
            [host_python, "-c", f"import {mod}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode != 0:
            raise SystemExit(
                f"HARVEST_BLOCKER: sandbox=subprocess but host missing '{mod}'. "
                "swegemma SubprocessManager skips install_test_dependencies; "
                "run scripts/ensure_subprocess_host_deps.py or set SWE_SANDBOX=docker."
            )


def make_eval_config(
    *,
    data_dir: Path,
    run_dir: Path,
    agent_dir: Path,
    models: object,
    sandbox: str,
    timeout_seconds: int,
    max_time_minutes: float,
    max_tool_calls: int,
    max_turns: int | None,
) -> EvalConfig:
    return EvalConfig(
        tasks_path=data_dir / "tasks.jsonl",
        snapshots_dir=data_dir / "snapshots",
        results_dir=run_dir,
        submission_dir=agent_dir,
        models=models,
        sandbox=sandbox,
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


def main() -> int:
    ap = argparse.ArgumentParser(description="P5.1b STaR recovery")
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--agent-dir", type=Path, default=Path("agent"))
    ap.add_argument("--max-retries", type=int, default=1)
    ap.add_argument("--error-tail-lines", type=int, default=20)
    ap.add_argument("--out", type=Path, default=Path("traces/star"))
    ap.add_argument("--data", type=Path, default=None)
    ap.add_argument("--model", type=Path, default=None)
    args = ap.parse_args()
    if args.error_tail_lines < 1:
        raise SystemExit("--error-tail-lines must be >=1 (spec default 20)")

    root = Path(os.environ.get("P31_ROOT", os.environ.get("P5_ROOT", "/workspace/gemma4-p312")))
    agent_dir = args.agent_dir if args.agent_dir.is_absolute() else (Path.cwd() / args.agent_dir)
    if not (agent_dir / "agent.yaml").is_file() and (root / "agent" / "agent.yaml").is_file():
        agent_dir = root / "agent"
    data_dir = args.data or (root / "data")
    model_path = Path(
        args.model
        or os.environ.get("MODEL_PATH", "/workspace/models/gemma-4-31B-it-qat-w4a16-ct")
    )
    task_ids = parse_tasks(args.tasks)
    holdout_path = root / "configs" / "splits.json"
    if holdout_path.is_file():
        holdout = set(json.loads(holdout_path.read_text()).get("holdout") or [])
        blocked = [t for t in task_ids if t in holdout]
        if blocked:
            raise SystemExit(f"G4: refuse holdout task ids: {blocked}")

    out = args.out if args.out.is_absolute() else Path.cwd() / args.out
    out.mkdir(parents=True, exist_ok=True)
    keep = out / "keep"
    keep.mkdir(parents=True, exist_ok=True)
    sandbox = os.environ.get("SWE_SANDBOX", "subprocess").strip() or "subprocess"
    if sandbox == "subprocess":
        preflight_subprocess_host()

    litellm.drop_params = True
    os.environ["STAR_ERROR_TAIL_LINES"] = str(args.error_tail_lines)
    target = "gemma-4-31b-it-qat-w4a16-ct"
    declared = validate_single_declared_model(agent_dir)
    adapters = discover_adapters(str(agent_dir), adapter_extensions=ALLOWED_ADAPTER_EXTENSIONS)
    gpu_count = torch.cuda.device_count() if torch.cuda.is_available() else 1
    tp = 4 if gpu_count >= 4 else (2 if gpu_count >= 2 else 1)

    vllm_cfg = VllmConfig(
        model=str(model_path),
        port=int(os.environ.get("VLLM_PORT", "8000")),
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
    models = server.create_model_registry(
        aliases=[declared, target], model_prefix="openai/", api_key="EMPTY"
    )
    tasks_all = {t.instance_id: t for t in load_tasks(data_dir / "tasks.jsonl")}
    missing = [tid for tid in task_ids if tid not in tasks_all]
    if missing:
        raise SystemExit(f"unknown tasks: {missing}")

    raw = yaml.safe_load((agent_dir / "eval_config.yaml").read_text())
    ev = raw.get("evaluation", raw)
    timeout_seconds = int(ev.get("timeout_seconds", 300))
    max_tool_calls = int(ev.get("max_tool_calls", 60))
    max_time_minutes = float(ev.get("max_time_minutes", 4))
    max_turns = int(ev.get("max_turns", 80)) if ev.get("max_turns") is not None else None
    _limits, _gen = build_submission_limits()

    recoveries = 0
    ledger = []
    t0 = time.time()
    try:
        for tid in task_ids:
            task = tasks_all[tid]
            run_dir = out / tid / "attempt0"
            run_dir.mkdir(parents=True, exist_ok=True)
            # Resume/seed: if attempt0/meta.json already present with non-empty
            # error feedback (e.g. staged BoN near-miss), skip re-running attempt0
            # and go straight to error-guided retry (no new CLI flags; G2).
            seed_meta_p = run_dir / "meta.json"
            reused_seed = False
            if seed_meta_p.is_file():
                try:
                    seed = json.loads(seed_meta_p.read_text())
                except Exception:  # noqa: BLE001
                    seed = {}
                seed_feedback = star_feedback_blob(
                    {
                        "error": seed.get("error") or seed.get("error_tail") or "",
                        "test_output_tail": seed.get("test_output_tail")
                        or seed.get("oracle_error_summary_g11")
                        or "",
                    }
                )
                if seed.get("resolved") is True:
                    print(f"=== STAR {tid} attempt0 REUSE resolved=True ===", flush=True)
                    ledger.append(seed)
                    dest = keep / f"{tid}_attempt0"
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(run_dir, dest)
                    continue
                if seed_feedback.strip() and seed.get("resolved") is not True:
                    print(
                        f"=== STAR {tid} attempt0 REUSE_SEED "
                        f"(skip eval; error_tail={args.error_tail_lines}) ===",
                        flush=True,
                    )
                    error_tail = tail_lines(seed_feedback, args.error_tail_lines)
                    row0 = {
                        "task_id": tid,
                        "attempt": 0,
                        "resolved": False,
                        "patch_chars": len(seed.get("patch") or "")
                        or int(seed.get("patch_chars") or 0),
                        "patch": seed.get("patch") or "",
                        "seconds": float(seed.get("seconds") or 0),
                        "error_tail": error_tail,
                        "test_exit_code": seed.get("test_exit_code"),
                        "test_output_tail": seed.get("test_output_tail") or "",
                        "sandbox": sandbox,
                        "source": seed.get("source") or "seeded_near_miss",
                        "license": "self",
                        "feedback_injected": False,
                        "seeded_from_existing_attempt0": True,
                    }
                    seed_meta_p.write_text(json.dumps(row0, indent=2) + "\n")
                    ledger.append(row0)
                    reused_seed = True
            if not reused_seed:
                eval_config = make_eval_config(
                    data_dir=data_dir,
                    run_dir=run_dir,
                    agent_dir=agent_dir,
                    models=models,
                    sandbox=sandbox,
                    timeout_seconds=timeout_seconds,
                    max_time_minutes=max_time_minutes,
                    max_tool_calls=max_tool_calls,
                    max_turns=max_turns,
                )
                evaluator = Evaluator(eval_config)
                print(f"=== STAR {tid} attempt0 ===", flush=True)
                ts = time.time()
                try:
                    result = run_sync(evaluator.evaluate_task, task)
                    diag = result_diag(result)
                except Exception as e:  # noqa: BLE001
                    diag = {
                        "resolved": False,
                        "patch": "",
                        "error": str(e),
                        "test_exit_code": None,
                        "test_output_tail": "",
                    }
                resolved = diag["resolved"]
                feedback = star_feedback_blob(diag)
                error_tail = tail_lines(feedback, args.error_tail_lines)
                row0 = {
                    "task_id": tid,
                    "attempt": 0,
                    "resolved": resolved,
                    "patch_chars": len(diag.get("patch") or ""),
                    "patch": diag.get("patch") or "",
                    "seconds": round(time.time() - ts, 2),
                    "error_tail": error_tail,
                    "test_exit_code": diag["test_exit_code"],
                    "test_output_tail": diag["test_output_tail"],
                    "sandbox": sandbox,
                    "source": "self",
                    "license": "self",
                    "feedback_injected": False,
                }
                (run_dir / "meta.json").write_text(json.dumps(row0, indent=2) + "\n")
                ledger.append(row0)
                if resolved:
                    dest = keep / f"{tid}_attempt0"
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(run_dir, dest)
                    continue
            # Retry: inject truncated verifier/error into problem_statement (G11).
            # Env STAR_PREV_ERROR_TAIL alone is NOT consumed by agent/ — must mutate task.
            for r in range(1, args.max_retries + 1):
                os.environ["STAR_PREV_ERROR_TAIL"] = error_tail
                injected = bool(error_tail.strip())
                retry_task = task_with_error_tail(task, error_tail)
                retry_dir = out / tid / f"attempt{r}"
                retry_dir.mkdir(parents=True, exist_ok=True)
                eval_config = make_eval_config(
                    data_dir=data_dir,
                    run_dir=retry_dir,
                    agent_dir=agent_dir,
                    models=models,
                    sandbox=sandbox,
                    timeout_seconds=timeout_seconds,
                    max_time_minutes=max_time_minutes,
                    max_tool_calls=max_tool_calls,
                    max_turns=max_turns,
                )
                evaluator = Evaluator(eval_config)
                print(
                    f"=== STAR {tid} attempt{r} (error_tail={args.error_tail_lines}, injected={injected}) ===",
                    flush=True,
                )
                ts = time.time()
                try:
                    result = run_sync(evaluator.evaluate_task, retry_task)
                    diag = result_diag(result)
                except Exception as e:  # noqa: BLE001
                    diag = {
                        "resolved": False,
                        "patch": "",
                        "error": str(e),
                        "test_exit_code": None,
                        "test_output_tail": "",
                    }
                resolved = diag["resolved"]
                feedback = star_feedback_blob(diag)
                error_tail = tail_lines(feedback, args.error_tail_lines)
                row = {
                    "task_id": tid,
                    "attempt": r,
                    "resolved": resolved,
                    "patch_chars": len(diag.get("patch") or ""),
                    "patch": diag.get("patch") or "",
                    "seconds": round(time.time() - ts, 2),
                    "error_tail": error_tail,
                    "test_exit_code": diag["test_exit_code"],
                    "test_output_tail": diag["test_output_tail"],
                    "sandbox": sandbox,
                    "source": "self",
                    "license": "self",
                    "feedback_injected": injected,
                }
                (retry_dir / "meta.json").write_text(json.dumps(row, indent=2) + "\n")
                ledger.append(row)
                if resolved:
                    recoveries += 1
                    dest = keep / f"{tid}_attempt{r}"
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(retry_dir, dest)
                    break
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass

    summary = {
        "ticket": "P5.1b",
        "n_tasks": len(task_ids),
        "max_retries": args.max_retries,
        "error_tail_lines": args.error_tail_lines,
        "recoveries": recoveries,
        "load_s": load_s,
        "wall_s": round(time.time() - t0, 2),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feedback_mode": "problem_statement_inject",
        "ledger": ledger,
    }
    (out / "star_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in summary if k != "ledger"}, indent=2), flush=True)
    print(f"recoveries={recoveries} keep={keep}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
