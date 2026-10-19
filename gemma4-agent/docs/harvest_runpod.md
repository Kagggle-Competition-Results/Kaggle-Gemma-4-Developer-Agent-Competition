# RunPod BoN/STaR harvest ops (agents)

**Read before any P5 harvest pod.** Companion: [`harness_verify.md`](./harness_verify.md), [`p51_train_inventory.md`](./p51_train_inventory.md).

## Why prior BoNs scored yield=0

`sandbox=subprocess` → swegemma **no-ops** `install_test_dependencies` / `install_editable_package`.  
Verify uses host `system_site_packages` only. Kaggle notebooks pip-install the metric wheelhouse first; bare RunPod vLLM bootstrap does **not**. Correct patches looked like `resolved=False` with empty `error`.

**Src-layout import trap (audited 2026-10-17 on A40):** `SubprocessManager.exec` sets `PYTHONPATH=<workspace>` only. For `requests` / `httpx` (`src/<pkg>`), pytest imports **host** `.venv_bootstrap/.../site-packages/<pkg>` — gold patches never take effect. Flat-layout repos (`rich`, `fastapi`) import workspace correctly. Fix: `PYTHONPATH=<workspace>/src:<workspace>` (or real editable install). Confirmed: gold+test_patch for `requests_7505`/`6644`/`7205`/`httpx_3672` go **0-fail** with `src` on `PYTHONPATH`. Do **not** treat FAIL_TO_PASS failures under the broken import path as “host drift.”

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
   - Installs pytest+safe subset from `data/wheels`, then sandbox extras from `sandbox/extras.txt` online when missing (`inline_snapshot`, `dirty_equals`, …).
   - `pin_subprocess_task_deps.py` also installs those extras; BoN/STaR preflight fail-fast if they are absent.
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

## Current strategy (2026-10-17)

| Priority | Action | Ablation (G3) |
| --- | --- | --- |
| **1 GPU** | **P5.1a-r8**: BoN on **exact P3.14a** agent (8m/28/Verra), `BON_TEMPERATURE=0.75`, N=16, wave1=12 train_pool tasks | Harvest sampling only; submit YAML untouched |
| 2 CPU | Gate1A autopsy done; drop `fastapi_14953` / all-no_edit tasks | Pool hygiene |
| 3 GPU | STaR then DoRA when keepers + unique train_pool tasks grow | Locked Plan A |

**Harvest agent path:** `results/harvest_agent_p314a/` (exact P3.14a). Do **not** use contaminated working-tree `agent/` or freeze-20-line override for r8+.

**Do not:** prompt A/Bs on harvest; SFT on &lt;tens of keepers; skip oracle gate; terminate before S3 sync; provision cloud without live `CLOUD_APPROVAL`.

## S3 layout

- Tools scripts: `s3://gemma4-dev-agent-data-650825122703/tools/scripts/`
- Results: `s3://…/results/p51a_r4/` (yield=0.75), `results/p51a_r5c/` (0.9375), `results/p51a_r6/` (~0.13), `results/p51b_star_r1/` (recoveries=1/9)
- Competition data: `s3://…/competition/full/`
