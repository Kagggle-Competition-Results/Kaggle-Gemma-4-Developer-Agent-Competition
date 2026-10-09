# RunPod BoN/STaR harvest ops (agents)

**Read before any P5 harvest pod.** Companion: [`harness_verify.md`](./harness_verify.md), [`p51_train_inventory.md`](./p51_train_inventory.md).

## Why prior BoNs scored yield=0

`sandbox=subprocess` → swegemma **no-ops** `install_test_dependencies` / `install_editable_package`.  
Verify uses host `system_site_packages` only. Kaggle notebooks pip-install the metric wheelhouse first; bare RunPod vLLM bootstrap does **not**. Correct patches looked like `resolved=False` with empty `error`.

## CUDA / torch

- Prefer hosts with `nvidia-smi` **CUDA Version ≥ 13** for vllm 0.31 / torch cu130.
- If driver is 12.8: install torch from `cu128` index and **re-pin after** `pip install vllm` (vllm may pull cu130).
- After vllm: **force-reinstall `torch torchvision torchaudio` together** (never torch `--no-deps` alone). r5b failed with `RuntimeError: operator torchvision::nms does not exist`.
- Preflight: `import vllm.entrypoints.openai.api_server` before BoN.
- Do **not** treat `S3_PERSIST_DONE` as success if `ServerStartupError` / `BON_FAIL`.
- Fail fast: `python -c 'import torch; torch.zeros(1,device="cuda")'` before BoN.

## Mandatory preflight (every pod)

1. Sync competition `data/` (incl. `wheels/`, `snapshots/`, `tasks.jsonl`).
2. `python scripts/ensure_subprocess_host_deps.py --data data`  
   - Installs pytest+safe subset from `data/wheels` only (never `*.whl` unconstrained — starlette 1.x breaks FastAPI).
   - Current gap: wheel path still lacks `inline_snapshot` and `dirty_equals`; harvest labels stay noisy until those are added.
3. Pin a FastAPI-compatible starlette, e.g. `starlette-0.46.2` from `data/wheels` (`--no-deps`).
4. **Oracle gate:** gold patch on `rich_3882` must `resolved=True` before BoN. Abort if not.
5. Env: `SWE_SANDBOX=subprocess` (or `docker` + `gemma4-sandbox:base` if Docker image ready).
6. Persist `traces/` to S3 **before** `podTerminate`.

## Harvest scripts

| Script | Notes |
| --- | --- |
| `rejection_sample.py` | `BON_TEMPERATURE` (default 0.2); persists `test_exit_code` + `test_output_tail`; fail-fast if no host pytest |
| `star_correct.py` | same sandbox/host rules; `--error-tail-lines 20` |
| `ensure_subprocess_host_deps.py` | host verify deps |

## Current strategy (2026-10-09, after `P5.1b-star-r1`)

| Priority | Action | Ablation (G3) |
| --- | --- | --- |
| **1 GPU** | Fix missing subprocess deps, then rerun broadened non-holdout harvest/STaR | Current STaR misses are dominated by env failures |
| 2 GPU | STaR only on near-misses after clean oracle+deps preflight | `P5.1b-star-r1` gave 1/9 recoveries, but labels are still noisy |
| 3 CPU | `render_trajectories.py` + marginal-value audit | Keep main SFT clean; hold out env-polluted rows |
| Parallel Kaggle | Single-variable zip only if daily slot (LB baseline **0.06**) | wall or decode only |

**Do not:** prompt A/Bs; SFT on &lt;tens of keepers; skip oracle gate; terminate before S3 sync.

## S3 layout

- Tools scripts: `s3://gemma4-dev-agent-data-650825122703/tools/scripts/`
- Results: `s3://…/results/p51a_r4/` (yield=0.75), `results/p51a_r5c/` (0.9375), `results/p51a_r6/` (~0.13), `results/p51b_star_r1/` (recoveries=1/9)
- Competition data: `s3://…/competition/full/`
