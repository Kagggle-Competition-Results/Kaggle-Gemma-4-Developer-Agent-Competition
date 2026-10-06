# Environment pins (from official `HARNESS_README.md` + local status)

## Host stack (official)

| Component | Role |
| --- | --- |
| `swegemma` | SWE harness, 9 tools, verify_task / pytest scoring |
| `adk-submission` | Declarative YAML compiler (`compile_submission`); no competitor Python |
| `adk-eval-core` | Sandbox backends, trajectory tracing, edit helpers |

Local CLI (after install from competition packages / wheel index when available):

**Harness wheelhouse (authoritative for local/Kaggle):** Kaggle dataset
`metric/gemma-4-developer-agent-wheelhouse` — includes `swegemma-0.2.7`, `adk_submission-0.2.12`,
`adk_eval_core-0.1.0`, `google_adk-…`. **Not on public PyPI.** Official starter notebook:
`ryanholbrook/getting-started-gemma-4-developer-agent` (`machine_shape: NvidiaL4`).

**P2.6 smoke kernel:** `moranzavdi/gemma4-p26-smoke-swegemma` (repo: `gemma4-agent/notebooks/p26_smoke/kernel/`).


```bash
swegemma eval \
  --tasks tasks.jsonl \
  --snapshots-dir snapshots \
  --submission-dir agent \
  --results-dir results/run_01 \
  --sandbox docker \
  --max-tool-calls 60 \
  --max-time-minutes 5
```

Soundness controls from harness docs:
- `--skip-agent-patch` → empty patch / baseline should fail Phase 2
- Oracle: apply gold `patch` from `tasks.jsonl` then verify (via harness / local verify)

## Model / serve

| Item | Value |
| --- | --- |
| Competition model | `gemma-4-31b-it-qat-w4a16-ct` |
| Context | `max_model_len=32768` |
| GPUs | 4×L4, TP=4, `gpu_memory_utilization=0.80` |
| LoRA | `max_loras=8`, `max_lora_rank=128` |
| Zip | unpacked &lt; 3 GiB |

**P0.6 4-bit serve parity:** **PASS** on RunPod RTX A6000 48GB (`results/p06_serve_check.json`): load 65.33s (&lt;90), gen 1000 tokens, no OOM. Model `google/gemma-4-31B-it-qat-w4a16-ct` via S3. Note: FlashInfer sampler disabled (`VLLM_USE_FLASHINFER_SAMPLER=0`) due to host nvcc.

## Submission schema (locked from HARNESS_README)

- Root: `agent.yaml` (+ optional `eval_config.yaml` under `evaluation:` key)
- Skills: directories with `SKILL.md` (not flat `.md` at skills root)
- `!include` for prompts / sampling YAML
- Adapters: `adapters/<name>/adapter_config.json` + `adapter_model.safetensors` only
- Budgets: `evaluation.max_time_minutes`, `max_tool_calls`, `max_turns`, `timeout_seconds`

## Data layout

```text
data/
  tasks.jsonl                 # 129 tasks (instance_id, base_commit, patch, test_patch, …)
  snapshots/<instance_id>.tgz
  graphs/<repo>_<commit40>.json
  embeddings/<repo>_<commit40>.npz
  wheels/  docker/  sandbox/  sample_submission/
```

Join: `instance_id` + `base_commit` → graph/embed paths. **All 129 tasks have graph+embed files** (some commits shared → 127 unique files).

### Soundness / sandbox verify (agents start here)

**Runbook (versioned):** [`docs/harness_verify.md`](harness_verify.md) — pin_deps, Starlette constraint rule, image extras, S3 resume, quarantine.

Quick probe after harness changes:

```bash
export SOUNDNESS_DOCKER_PLATFORM=linux/amd64
python scripts/soundness_check.py --data data/_full --rebuild-image \
  --task-id fastapi_11194 --out /tmp/probe_soundness.csv
# expect usable=True
```

### Local download status

| Asset | Status |
| --- | --- |
| `tasks.jsonl` + `HARNESS_README.md` | present (129 tasks parsed) |
| Full tree `data/_full/` | **ready** — 129 snaps / 127 graphs / 127 embeds; **0 zero-byte**; join 129/129 |
| Full zip `~20.42 GiB` | verified (`data/_full_dl/`); also syncing → S3 (see `docs/cloud-ops.md`) |
| Inventory artifacts | `data/_meta/full_inventory.json`, `full_join_inventory.json` |
| Rules accepted | yes (`userHasEntered=true`) |
| P0.6 4-bit serve | **PASS** (A6000 48GB; load 65s; 1000 tok) |

## Workbench

Any IDE/agent may edit the repo. Never put vendor IDE config under `agent/`. Cloud spend requires `CLOUD_APPROVAL` — see `docs/cloud-ops.md`.
