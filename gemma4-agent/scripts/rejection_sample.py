#!/usr/bin/env python3
"""P5.1a — BoN rejection sampling (agent-dev-spec §5).

Keeps only resolved=true trajectories under <out>/keep/.
Harvest sampling temperature via BON_TEMPERATURE (default 0.2); frozen agent
submit configs stay untouched (G3 freeze).
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
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
from swegemma.deduplication import resolve_task_snapshot_paths
from swegemma.evaluate import Evaluator
from swegemma.models import load_tasks
from swegemma.models.discovery import validate_single_declared_model

try:
    from host_drift import drift_immune_resolved, load_gold_baseline
except ImportError:  # pragma: no cover - allow running without helper on PATH
    _SCRIPT_DIR = Path(__file__).resolve().parent
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    from host_drift import drift_immune_resolved, load_gold_baseline

# Ensure src-layout packages are importable under subprocess verify/agent tools.
try:
    from patch_swegemma_subprocess import apply_patches as _apply_srcpath_patches

    _apply_srcpath_patches(python=None, dry_run=False)
except Exception as _srcpath_exc:  # noqa: BLE001
    print(f"WARN srcpath_patch_skip: {_srcpath_exc}", flush=True)

_PINNED_SNAPSHOT_KEYS: set[tuple[str, str, str]] = set()


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
            raise SystemExit(f"unsupported tasks json: {p}")
        return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return [t.strip() for t in arg.split(",") if t.strip()]


def _summarize_oracle_error(test_output: str, *, max_lines: int = 20) -> str:
    """G11: <=20 lines of failed nodes + short assertion crumbs for STaR."""
    text = test_output or ""
    if not text.strip():
        return ""
    lines = text.splitlines()
    summary_idx = next(
        (i for i, ln in enumerate(lines) if "short test summary info" in ln.lower()),
        None,
    )
    fail_idx = next(
        (
            i
            for i, ln in enumerate(lines)
            if "FAILURES" in ln and (ln.strip().startswith("=") or ln.strip().startswith("FAILURES"))
        ),
        None,
    )
    if summary_idx is not None:
        picked = lines[summary_idx : summary_idx + max_lines]
    elif fail_idx is not None:
        picked = lines[fail_idx : fail_idx + max_lines]
    else:
        picked = [ln for ln in lines if ln.strip()][-max_lines:]
    out = []
    for ln in picked:
        out.append(ln[:240] + ("…" if len(ln) > 240 else ""))
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def result_diag(result: object) -> dict:
    """Persist verify fields; backfill empty error from test_output (pre-V2 harness)."""
    err = getattr(result, "error", None) or getattr(result, "error_message", None) or ""
    out = str(getattr(result, "test_output", "") or "")
    err_s = str(err).strip()
    if not err_s and out.strip() and not bool(
        getattr(result, "resolved", False) or getattr(result, "success", False)
    ):
        err_s = _summarize_oracle_error(out, max_lines=20)
    return {
        "resolved": bool(
            getattr(result, "resolved", False) or getattr(result, "success", False)
        ),
        "patch": getattr(result, "agent_patch", "") or "",
        "error": err_s[:500],
        "test_exit_code": getattr(result, "test_exit_code", None),
        "test_output_tail": out[-1500:] if out else "",
        # Keep a longer window for host-drift compensation (Wave2).
        "test_output": out[-50000:] if out else "",
    }


def preflight_subprocess_host() -> None:
    """Check the host interpreter, not this venv, for subprocess verify deps."""
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
                "swegemma SubprocessManager skips install_test_dependencies/install_editable_package; "
                "verify uses system_site_packages only. On Kaggle the metric wheelhouse is pip-installed "
                "into the host first; RunPod bootstrap did not. Fix: install competition verify deps on "
                "host (see scripts/ensure_subprocess_host_deps.py) or set SWE_SANDBOX=docker with "
                "gemma4-sandbox:base."
            )


def _find_wheels_dir(data_dir: Path) -> Path | None:
    for cand in (
        data_dir / "wheels",
        data_dir.parent / "wheels",
        data_dir / "_full" / "wheels",
    ):
        if cand.is_dir() and any(cand.glob("*.whl")):
            return cand
    return None


def _extract_task_workspace(
    snapshots_dir: Path,
    task_id: str,
    repo: str,
    dest: Path,
) -> tuple[Path, Path, Path | None, Path | None]:
    snap_path, base_snapshot_path, patch_path = resolve_task_snapshot_paths(
        snapshots_dir, task_id, repo
    )
    archive = base_snapshot_path or snap_path
    if not archive.is_file():
        raise SystemExit(f"HARVEST_BLOCKER: missing snapshot archive for {task_id}: {archive}")
    with tarfile.open(archive, "r:*") as tf:
        for member in tf.getmembers():
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            tf.extract(member, dest, set_attrs=False)
    if patch_path and patch_path.is_file() and patch_path.stat().st_size > 0:
        patch_cmds = (
            ["git", "apply", "--unsafe-paths", "-p1", str(patch_path)],
            ["git", "apply", "--unsafe-paths", "-p0", str(patch_path)],
        )
        applied = False
        for cmd in patch_cmds:
            probe = subprocess.run(
                cmd,
                cwd=dest,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if probe.returncode == 0:
                applied = True
                break
        if not applied:
            raise SystemExit(f"HARVEST_BLOCKER: failed to apply snapshot patch for {task_id}")
    return dest, archive, base_snapshot_path, patch_path


def pin_subprocess_host_for_task(root: Path, data_dir: Path, task: object) -> None:
    wheels_dir = _find_wheels_dir(data_dir)
    if wheels_dir is None:
        raise SystemExit(f"HARVEST_BLOCKER: no wheels dir under {data_dir}")
    task_id = getattr(task, "instance_id")
    repo = getattr(task, "repo", "")
    snap_path, base_snapshot_path, patch_path = resolve_task_snapshot_paths(
        data_dir / "snapshots", task_id, repo
    )
    cache_key = (
        str((base_snapshot_path or snap_path).resolve()),
        str(patch_path.resolve()) if patch_path else "",
        repo,
    )
    if cache_key in _PINNED_SNAPSHOT_KEYS:
        return

    pin_root = root / "tmp"
    pin_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(
        tempfile.mkdtemp(prefix=f"p51_pin_{task_id}_", dir=str(pin_root))
    )
    try:
        _extract_task_workspace(data_dir / "snapshots", task_id, repo, work_dir)
        pin_script = root / "scripts" / "pin_subprocess_task_deps.py"
        pin = subprocess.run(
            [
                sys.executable,
                str(pin_script),
                "--workspace",
                str(work_dir),
                "--wheels",
                str(wheels_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        tail = "\n".join((pin.stdout + "\n" + pin.stderr).splitlines()[-20:])
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "repo": repo,
                    "pin_host_rc": pin.returncode,
                    "pin_host_tail": tail,
                }
            ),
            flush=True,
        )
        if pin.returncode != 0:
            raise SystemExit(
                f"HARVEST_BLOCKER: host task pin failed for {task_id} (rc={pin.returncode})"
            )
        _PINNED_SNAPSHOT_KEYS.add(cache_key)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _load_existing_meta(run_dir: Path) -> dict | None:
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text())
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _count_keepers(ledger: list[dict], tid: str) -> int:
    return sum(1 for row in ledger if row.get("task_id") == tid and row.get("resolved"))


def _run_one_sample(
    *,
    tid: str,
    k: int,
    task: object,
    eval_config: EvalConfig,
    bon_temp: float,
    sandbox: str,
    run_dir: Path,
    gold_baseline: dict,
    test_patch: str,
) -> dict:
    traces_dir = run_dir / "traces"
    traces_dir.mkdir(exist_ok=True)
    evaluator = Evaluator(eval_config)
    print(f"=== {tid} sample {k} ===", flush=True)
    ts = time.time()
    try:
        result = run_sync(evaluator.evaluate_task, task)
        diag = result_diag(result)
    except Exception as e:  # noqa: BLE001
        diag = {
            "resolved": False,
            "patch": "",
            "error": str(e)[:500],
            "test_exit_code": None,
            "test_output_tail": "",
            "test_output": "",
        }
    patch = diag["patch"]
    raw_resolved = bool(diag["resolved"])
    test_output = str(diag.get("test_output") or diag.get("test_output_tail") or "")
    resolved, drift_dbg = drift_immune_resolved(
        raw_resolved=raw_resolved,
        test_output=test_output,
        agent_patch=patch,
        test_patch=test_patch,
        task_id=tid,
        gold_baseline=gold_baseline,
    )
    if resolved and not raw_resolved:
        print(
            f"DRIFT_RESOLVED {tid} sample={k} reason={drift_dbg.get('reason')}",
            flush=True,
        )
    return {
        "task_id": tid,
        "sample": k,
        "resolved": resolved,
        "resolved_raw_harness": raw_resolved,
        "patch_chars": len(patch),
        "patch": patch,
        "seconds": round(time.time() - ts, 2),
        "error": diag["error"],
        "test_exit_code": diag["test_exit_code"],
        "test_output_tail": diag["test_output_tail"],
        "test_output": diag.get("test_output") or "",
        "drift": drift_dbg,
        "source": "self",
        "license": "self",
        "temperature": bon_temp,
        "sandbox": sandbox,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="P5.1a BoN rejection sampling")
    ap.add_argument("--tasks", required=True, help="comma list, or file of task ids")
    ap.add_argument("--n", type=int, default=8, help="samples per task")
    ap.add_argument("--agent-dir", type=Path, default=Path("agent"))
    ap.add_argument("--out", type=Path, default=Path("traces/bon"))
    ap.add_argument("--data", type=Path, default=None, help="data root (default: sibling of agent or $P31_ROOT/data)")
    ap.add_argument("--model", type=Path, default=None)
    ap.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("BON_CONCURRENCY", "1")),
        help="parallel rollouts against local vLLM (default 1; Wave2 screening uses 2)",
    )
    ap.add_argument(
        "--command-timeout",
        type=int,
        default=int(os.environ.get("BON_COMMAND_TIMEOUT", "75")),
        help="shell/pytest command timeout seconds (overrides eval_config timeout_seconds)",
    )
    ap.add_argument(
        "--gold-baseline",
        type=Path,
        default=None,
        help="a40_gold_baseline_failures.json for host-drift-immune resolved=True",
    )
    args = ap.parse_args()

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
    keep = out / "keep"
    raw_root = out / "raw"
    keep.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)

    litellm.drop_params = True
    # Harvest diversity only — does not rewrite frozen agent/configs/sampling.yaml
    bon_temp = float(os.environ.get("BON_TEMPERATURE", "0.2"))
    os.environ["ADK_SAMPLING_TEMPERATURE"] = str(bon_temp)
    sandbox = os.environ.get("SWE_SANDBOX", "subprocess").strip() or "subprocess"
    if sandbox == "subprocess":
        preflight_subprocess_host()

    target = "gemma-4-31b-it-qat-w4a16-ct"
    declared = validate_single_declared_model(agent_dir)
    adapters = discover_adapters(str(agent_dir), adapter_extensions=ALLOWED_ADAPTER_EXTENSIONS)
    gpu_count = torch.cuda.device_count() if torch.cuda.is_available() else 1
    tp = 4 if gpu_count >= 4 else (2 if gpu_count >= 2 else 1)
    print(
        "BON declared",
        declared,
        "tp",
        tp,
        "n",
        args.n,
        "temp",
        bon_temp,
        "sandbox",
        sandbox,
        "tasks",
        len(task_ids),
        flush=True,
    )

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
    ledger = []
    kept = 0
    t0 = time.time()
    try:
        t_load = time.time()
        server.start()
        load_s = round(time.time() - t_load, 2)
        print("vLLM", server.base_url, "load_s", load_s, flush=True)
        models = server.create_model_registry(
            aliases=[declared, target],
            model_prefix="openai/",
            api_key="EMPTY",
        )

        tasks_all = {t.instance_id: t for t in load_tasks(data_dir / "tasks.jsonl")}
        missing = [tid for tid in task_ids if tid not in tasks_all]
        if missing:
            raise SystemExit(f"unknown tasks: {missing}")

        raw = yaml.safe_load((agent_dir / "eval_config.yaml").read_text())
        ev = raw.get("evaluation", raw)
        # Harvest command timeout (shell/pytest); keep agent wall from eval_config.
        timeout_seconds = int(args.command_timeout)
        max_tool_calls = int(ev.get("max_tool_calls", 60))
        max_time_minutes = float(ev.get("max_time_minutes", 4))
        max_turns = int(ev.get("max_turns", 80)) if ev.get("max_turns") is not None else None
        concurrency = max(1, int(args.concurrency))
        _limits, _gen = build_submission_limits()
        baseline_path = args.gold_baseline
        if baseline_path is None:
            env_p = os.environ.get("GOLD_BASELINE_JSON", "").strip()
            if env_p:
                baseline_path = Path(env_p)
            else:
                for cand in (
                    root / "results" / "p51a_r8_p314a" / "a40_gold_baseline_failures.json",
                    Path("results/p51a_r8_p314a/a40_gold_baseline_failures.json"),
                    data_dir.parent / "results" / "p51a_r8_p314a" / "a40_gold_baseline_failures.json",
                ):
                    if cand.is_file():
                        baseline_path = cand
                        break
        gold_baseline = load_gold_baseline(baseline_path)
        # task_id -> test_patch text for FAIL_TO_PASS targeting
        test_patches: dict[str, str] = {}
        for ln in (data_dir / "tasks.jsonl").read_text().splitlines():
            if not ln.strip():
                continue
            row = json.loads(ln)
            test_patches[row["instance_id"]] = row.get("test_patch") or ""
        print(
            f"BON limits command_timeout={timeout_seconds}s wall_min={max_time_minutes} "
            f"concurrency={concurrency} early_stop_keepers=2 resume=1 "
            f"gold_baseline_tasks={len(gold_baseline)} path={baseline_path}",
            flush=True,
        )

        for tid in task_ids:
            task = tasks_all[tid]
            if sandbox == "subprocess":
                pin_subprocess_host_for_task(root, data_dir, task)

            # Idempotent resume: load existing metas into ledger and skip them.
            pending: list[int] = []
            for k in range(1, args.n + 1):
                run_dir = raw_root / tid / f"n{k:02d}"
                run_dir.mkdir(parents=True, exist_ok=True)
                existing = _load_existing_meta(run_dir)
                if existing is not None:
                    existing.setdefault("task_id", tid)
                    existing.setdefault("sample", k)
                    ledger.append(existing)
                    if existing.get("resolved"):
                        kept += 1
                        dest = keep / f"{tid}_n{k:02d}"
                        if not dest.exists():
                            shutil.copytree(run_dir, dest)
                    print(
                        f"=== {tid} sample {k}/{args.n} RESUME_SKIP resolved={bool(existing.get('resolved'))} ===",
                        flush=True,
                    )
                    continue
                pending.append(k)

            # Early-stop at 2 keepers (including resumed).
            if _count_keepers(ledger, tid) >= 2:
                print(f"=== {tid} EARLY_STOP keepers>=2 (skip {len(pending)} pending) ===", flush=True)
                pending = []

            while pending and _count_keepers(ledger, tid) < 2:
                batch = pending[:concurrency]
                pending = pending[concurrency:]
                jobs = []
                for k in batch:
                    run_dir = raw_root / tid / f"n{k:02d}"
                    eval_config = EvalConfig(
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
                    jobs.append((k, run_dir, eval_config))

                def _do(job: tuple[int, Path, EvalConfig]) -> dict:
                    k, run_dir, eval_config = job
                    return _run_one_sample(
                        tid=tid,
                        k=k,
                        task=task,
                        eval_config=eval_config,
                        bon_temp=bon_temp,
                        sandbox=sandbox,
                        run_dir=run_dir,
                        gold_baseline=gold_baseline,
                        test_patch=test_patches.get(tid, ""),
                    )

                if len(jobs) == 1:
                    rows_out = [_do(jobs[0])]
                else:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                        rows_out = list(pool.map(_do, jobs))

                for row in sorted(rows_out, key=lambda r: int(r.get("sample") or 0)):
                    k = int(row["sample"])
                    run_dir = raw_root / tid / f"n{k:02d}"
                    (run_dir / "meta.json").write_text(json.dumps(row, indent=2) + "\n")
                    print(row, flush=True)
                    ledger.append(row)
                    if row.get("resolved"):
                        dest = keep / f"{tid}_n{k:02d}"
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(run_dir, dest)
                        kept += 1

                if _count_keepers(ledger, tid) >= 2 and pending:
                    print(
                        f"=== {tid} EARLY_STOP keepers>=2 (skip {len(pending)} remaining) ===",
                        flush=True,
                    )
                    pending = []

        summary = {
            "ticket": "P5.1a",
            "n_tasks": len(task_ids),
            "n_per_task": args.n,
            "n_attempts": len(ledger),
            "n_kept": kept,
            "yield": round(kept / len(ledger), 4) if ledger else 0.0,
            "load_s": load_s,
            "wall_s": round(time.time() - t0, 2),
            "temperature": bon_temp,
            "concurrency": concurrency,
            "command_timeout_seconds": timeout_seconds,
            "task_ids": task_ids,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ledger": ledger,
        }
        (out / "bon_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps({k: summary[k] for k in summary if k != "ledger"}, indent=2), flush=True)
        print(f"kept={kept} under {keep}", flush=True)
        return 0
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
