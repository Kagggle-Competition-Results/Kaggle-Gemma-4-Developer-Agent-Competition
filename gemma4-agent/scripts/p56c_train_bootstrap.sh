#!/bin/bash
# P5.6→P5.7→P5.10→P5.19→GATE_TRAIN_SHIP on one 80GB GPU pod.
# Stages sync to s3://…/results/p56c_full_edit_pipeline/ after each milestone.
set -uo pipefail
exec >> /workspace/p56c_train_bootstrap.log 2>&1
echo "=== P5.6c full-edit train+P6.1 smoke bootstrap $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nvidia-smi | head -n 5 || true

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_IGNORE_REQUIRES_PYTHON=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export AWS_DEFAULT_REGION=us-east-1
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
# Cut CUDA allocator fragmentation on 48GB CT+LoRA (P5.6c VRAM hot-fix).
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
# Prefer host driver libcuda over CUDA toolkit "compat" stubs (avoids
# torch.cuda "CUDA unknown error" / is_available()==False on some RunPod images).
export LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/usr/local/cuda/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export CUDA_DEVICE_ORDER=PCI_BUS_ID
if [ -f /workspace/p56c_cuda.env ]; then
  # shellcheck disable=SC1091
  set -a; source /workspace/p56c_cuda.env; set +a
fi
# Early CUDA sanity (non-fatal log)
python3 - <<'PY' || true
import torch
print(f"CUDA_SANITY torch={torch.__version__} cuda={torch.version.cuda} avail={torch.cuda.is_available()} n={torch.cuda.device_count()}")
PY
if [ -f /workspace/p51_aws_creds.env ]; then
  # shellcheck disable=SC1091
  set -a; source /workspace/p51_aws_creds.env; set +a
fi
pip install -q awscli 2>/dev/null || true
hash -r || true

BUCKET=gemma4-dev-agent-data-650825122703
S3_PREFIX="s3://$BUCKET/results/p56c_full_edit_pipeline"
ROOT=/workspace/gemma4-p56c
STATUS_FILE=/workspace/p56c_train.status
MODEL_ROOT=/models
OUT="$ROOT/results/p56c_full_edit_pipeline"
mkdir -p "$ROOT" "$MODEL_ROOT" "$OUT" "$ROOT/data_train" "$ROOT/configs" "$ROOT/scripts" "$ROOT/results"

set_status() {
  printf '%s\n' "$1" > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  echo "STATUS=$1"
}

s3_sync_stage() {
  local label="$1"
  aws s3 sync "$OUT/" "$S3_PREFIX/" --only-show-errors || true
  aws s3 cp /workspace/p56c_train_bootstrap.log "$S3_PREFIX/p56c_train_bootstrap.log" --only-show-errors || true
  aws s3 cp "$STATUS_FILE" "$S3_PREFIX/p56c_train.status" --only-show-errors || true
  echo "S3_SYNC $label $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

# KEEP_ALIVE_ON_FAIL=1 (default): on train-stage failure, HOLD the pod for a
# hot-fix retry instead of exiting (watchdog must also not terminate on FAIL).
# Touch /workspace/p56_retry to re-run the failed stage after scp'ing fixes.
# Touch /workspace/p56_abort to stop the pipeline.
KEEP_ALIVE_ON_FAIL="${KEEP_ALIVE_ON_FAIL:-1}"

refresh_uploaded_bundle() {
  for f in train_qlora.py train_grpo.py merge_adapters.py serve_check_4bit.py gate_train_ship.py \
           patch_swegemma_subprocess.py render_trajectories.py run_p56c_smoke.py \
           ensure_subprocess_host_deps.py; do
    if [ -f "/workspace/$f" ]; then
      cp "/workspace/$f" "$ROOT/scripts/$f"
    fi
  done
  for f in train_qlora.yaml train_grpo.yaml merge_ties.yaml serve_check_4bit.yaml gate_train_ship.yaml \
           distillation_policy.md splits.json; do
    if [ -f "/workspace/$f" ]; then
      cp "/workspace/$f" "$ROOT/configs/$f"
    fi
  done
  if [ -f /workspace/p56c_smoke_stage.sh ]; then
    chmod +x /workspace/p56c_smoke_stage.sh
  fi
}

hold_for_retry() {
  local stage="$1"
  set_status "HOLD_$stage"
  s3_sync_stage "HOLD_$stage"
  echo "HOLD_$stage — pod kept alive. Upload fixes to /workspace then: touch /workspace/p56_retry"
  echo "         or abort with: touch /workspace/p56_abort"
  rm -f /workspace/p56_retry /workspace/p56_abort
  while [ ! -f /workspace/p56_retry ] && [ ! -f /workspace/p56_abort ]; do
    sleep 30
  done
  if [ -f /workspace/p56_abort ]; then
    set_status "ABORTED_$stage"
    s3_sync_stage "ABORTED_$stage"
    exit 99
  fi
  rm -f /workspace/p56_retry
  refresh_uploaded_bundle
  echo "RETRY_$stage $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

fail() {
  # Bootstrap hard errors: with keep-alive, HOLD then re-exec bootstrap (model
  # sync is cheap if already cached). Without keep-alive, exit.
  set_status "$1"
  s3_sync_stage "FAIL_$1"
  if [ "$KEEP_ALIVE_ON_FAIL" = "1" ]; then
    hold_for_retry "$1"
    echo "REEXEC_BOOTSTRAP after $1 $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exec bash /workspace/p56c_train_bootstrap.sh
  fi
  exit "${2:-13}"
}

# Retrying stage runner for train steps (SFT/GRPO/merge/serve/gate).
run_retryable() {
  local run_status="$1"
  local fail_status="$2"
  local done_status="$3"
  shift 3
  while true; do
    set_status "$run_status"
    if "$@"; then
      set_status "$done_status"
      s3_sync_stage "$done_status"
      echo "$done_status $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      return 0
    fi
    set_status "$fail_status"
    s3_sync_stage "FAIL_$fail_status"
    echo "$fail_status $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ "$KEEP_ALIVE_ON_FAIL" != "1" ]; then
      exit 20
    fi
    hold_for_retry "$fail_status"
  done
}

rm -f "$STATUS_FILE"
set_status BOOTSTRAPPING

# Torch wheel index from driver ABI (cuDriverGetVersion), NOT nvidia-smi
# "CUDA Version" (that is max toolkit). cu130 fails on driver 12040 → use cu124.
python3 - <<'PY'
import ctypes
import re
import subprocess
from pathlib import Path

def driver_abi() -> float:
    try:
        lib = ctypes.CDLL("libcuda.so.1")
        ver = ctypes.c_int(0)
        lib.cuDriverGetVersion(ctypes.byref(ver))
        major, rem = divmod(int(ver.value), 1000)
        minor = rem // 10
        return float(f"{major}.{minor}")
    except Exception as exc:
        print(f"cuDriverGetVersion_fail={exc}")
    out = subprocess.check_output(["nvidia-smi"], text=True)
    m = re.search(r"CUDA Version:\s*([0-9.]+)", out)
    return float(m.group(1)) if m else 12.4

ver = driver_abi()
if ver >= 13.0:
    idx = "cu130"
elif ver >= 12.8:
    idx = "cu128"
elif ver >= 12.4:
    idx = "cu124"
elif ver >= 12.1:
    idx = "cu121"
else:
    idx = "cu118"
Path("/tmp/torch_cu_index.env").write_text(
    f"export TORCH_CU_INDEX={idx}\nexport CUDA_DRIVER_VER={ver}\n"
)
print(f"cuda_driver_abi={ver} torch_index={idx}")
PY
# shellcheck disable=SC1091
source /tmp/torch_cu_index.env

# Align torch wheel to driver ABI, then install train stack.
python3 - <<PY
import os, subprocess, sys
idx = os.environ.get("TORCH_CU_INDEX", "cu124")
print(f"PIP_TORCH_INDEX={idx}")
subprocess.check_call([
    sys.executable, "-m", "pip", "install", "-q",
    "torch",
    "--index-url", f"https://download.pytorch.org/whl/{idx}",
])
pkgs = [
  # Match harvest pins: Gemma4 tokenizer_config needs transformers 5.x.
  # TrainingArguments.warmup_ratio is filtered in train_qlora.py when absent.
  "transformers>=5.10.4,<5.18.0",
  "huggingface_hub>=1.31.0",
  "tokenizers>=0.21.1",
  "peft>=0.13.0",
  "bitsandbytes>=0.43.0",
  "datasets>=2.19.0",
  "accelerate>=0.33.0",
  "compressed-tensors==0.17.0",
  "safetensors>=0.6.2",
  "sentencepiece",
  "protobuf",
  "pyyaml",
]
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *pkgs])
import torch, transformers
print(
    f"PIP_TRAIN_OK transformers={transformers.__version__} "
    f"torch={torch.__version__} cuda={torch.version.cuda} "
    f"cuda_avail={torch.cuda.is_available()}"
)
if not torch.cuda.is_available():
    raise SystemExit("CUDA_UNAVAILABLE after torch install — abort stage")
PY

# Stage uploaded bundle from /workspace
for f in train_qlora.py train_grpo.py merge_adapters.py serve_check_4bit.py gate_train_ship.py \
         patch_swegemma_subprocess.py render_trajectories.py run_p56c_smoke.py \
         ensure_subprocess_host_deps.py; do
  if [ -f "/workspace/$f" ]; then
    cp "/workspace/$f" "$ROOT/scripts/$f"
  fi
done
for f in train_qlora.yaml train_grpo.yaml merge_ties.yaml serve_check_4bit.yaml gate_train_ship.yaml \
         distillation_policy.md splits.json; do
  if [ -f "/workspace/$f" ]; then
    cp "/workspace/$f" "$ROOT/configs/$f"
  fi
done
if [ -f /workspace/sft.jsonl ]; then
  cp /workspace/sft.jsonl "$ROOT/data_train/sft.jsonl"
fi
# Traces tree (uploaded as tarball or rsync dir)
if [ -f /workspace/p56c_traces.tgz ]; then
  # tarball paths are results/<exp>/traces/... relative to gemma4-agent root
  tar -xzf /workspace/p56c_traces.tgz -C "$ROOT"
fi
if [ -d /workspace/p56c_results ]; then
  mkdir -p "$ROOT/results"
  cp -a /workspace/p56c_results/. "$ROOT/results/"
fi
# Smoke stage helper + harvest agent stay under /workspace (not frozen agent/)
if [ -f /workspace/p56c_smoke_stage.sh ]; then
  chmod +x /workspace/p56c_smoke_stage.sh
fi

# Sync model weights
set_status MODEL_SYNC
aws s3 sync "s3://$BUCKET/models/gemma-4-31B-it-qat-w4a16-ct/" \
  "$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct/" --only-show-errors \
  || fail MODEL_SYNC_FAIL 14
export MODEL_PATH="$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct"
test -f "$MODEL_PATH/config.json" || fail MODEL_MISSING 15
echo "MODEL_OK $MODEL_PATH"

cd "$ROOT"
export PYTHONPATH="$ROOT/scripts:${PYTHONPATH:-}"

# CPU preflight skipped when SKIP_PREFLIGHT=1 (local already validated 73/73 + GRPO 17/17).
if [ "${SKIP_PREFLIGHT:-1}" = "1" ]; then
  set_status PREFLIGHT_SKIP
  echo "PREFLIGHT_SKIP local_validated=73/73_grpo17"
  s3_sync_stage PREFLIGHT_SKIP
else
  set_status PREFLIGHT
  python3 scripts/train_qlora.py --config configs/train_qlora.yaml \
    --data data_train/sft.jsonl --out /tmp/unused_sft --dry-run \
    | tee "$OUT/preflight_qlora.jsonl" \
    || fail PREFLIGHT_QLORA_FAIL 16
  python3 scripts/train_grpo.py --config configs/train_grpo.yaml \
    --out /tmp/unused_grpo --dry-run \
    | tee "$OUT/preflight_grpo.json" \
    || fail PREFLIGHT_GRPO_FAIL 17
  echo "PREFLIGHT_SCRIPTS_OK"
  s3_sync_stage PREFLIGHT
fi

cuda_empty_cache() {
  python3 - <<'PY' || true
import torch
if torch.cuda.is_available():
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    print(f"CUDA_EMPTY_CACHE free_gb={torch.cuda.mem_get_info()[0]/1024**3:.2f}", flush=True)
PY
}

# ---- P5.6 DoRA SFT ----
SFT_OUT="$OUT/sft_dora_adapter"
mkdir -p "$SFT_OUT"
run_retryable SFT_RUNNING SFT_FAIL SFT_DONE \
  bash -c 'python3 scripts/train_qlora.py --config configs/train_qlora.yaml --data data_train/sft.jsonl --out "'"$SFT_OUT"'" && test -f "'"$SFT_OUT"'/adapter_config.json"'
cuda_empty_cache

# ---- P5.10 GRPO ----
GRPO_OUT="$OUT/grpo_adapter"
export SFT_ADAPTER="$SFT_OUT"
mkdir -p "$GRPO_OUT"
run_retryable GRPO_RUNNING GRPO_FAIL GRPO_DONE \
  bash -c 'python3 scripts/train_grpo.py --config configs/train_grpo.yaml --out "'"$GRPO_OUT"'" && test -f "'"$GRPO_OUT"'/adapter_config.json"'
cuda_empty_cache

# ---- P5.19 TIES merge ----
MASTER="$OUT/master_swe_adapter"
mkdir -p "$MASTER"
run_retryable MERGE_RUNNING MERGE_FAIL MERGE_DONE \
  bash -c 'python3 scripts/merge_adapters.py --config configs/merge_ties.yaml --adapters "'"$SFT_OUT"'" "'"$GRPO_OUT"'" --out "'"$MASTER"'" && test -f "'"$MASTER"'/adapter_config.json"'
cuda_empty_cache

# ---- P5.7 4-bit serve-check ----
run_retryable SERVE_CHECK_RUNNING SERVE_CHECK_FAIL SERVE_CHECK_DONE \
  python3 scripts/serve_check_4bit.py --config configs/serve_check_4bit.yaml \
    --adapter "$MASTER" --out "$OUT/p57_serve_check.json"
cuda_empty_cache

# ---- GATE_TRAIN_SHIP ----
# Write adapter + serve report into BOTH repo config and /workspace upload copy so
# refresh_uploaded_bundle on HOLD retry cannot wipe adapter="" again.
MASTER_FOR_GATE="$MASTER" OUT_FOR_GATE="$OUT" python3 - <<'PY'
import os
import yaml
from pathlib import Path
master = os.environ["MASTER_FOR_GATE"]
out = os.environ["OUT_FOR_GATE"]
paths = [
    Path("configs/gate_train_ship.yaml"),
    Path("/workspace/gate_train_ship.yaml"),
]
for p in paths:
    cfg = yaml.safe_load(p.read_text()) if p.is_file() else {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["adapter"] = master
    cfg["base_model"] = "gemma-4-31b-it-qat-w4a16-ct"
    cfg["serve_check_report"] = f"{out}/p57_serve_check.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    print("GATE_CFG_OK", p, cfg["adapter"])
PY
run_retryable GATE_RUNNING GATE_FAIL GATE_DONE \
  python3 scripts/gate_train_ship.py --config configs/gate_train_ship.yaml \
    --out "$OUT/gate_train_ship.json"
cuda_empty_cache

# Verify bf16 adapter sizes (~0.91 GiB each)
python3 - <<'PY'
import json, struct
from pathlib import Path
out = Path("/workspace/gemma4-p56c/results/p56c_full_edit_pipeline")
report = {}
for name in ("sft_dora_adapter", "grpo_adapter", "master_swe_adapter"):
    p = out / name / "adapter_model.safetensors"
    if not p.is_file():
        report[name] = {"exists": False}
        continue
    with p.open("rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        meta = json.loads(f.read(n))
    dtypes = {}
    for k, v in meta.items():
        if k == "__metadata__":
            continue
        dtypes[v.get("dtype")] = dtypes.get(v.get("dtype"), 0) + 1
    # Accept pure BF16/F16; reject any F32 payload (v1 was all F32 ~1.82GiB).
    bf16_ok = bool(dtypes) and ("F32" not in dtypes) and (
        "BF16" in dtypes or "F16" in dtypes
    )
    report[name] = {
        "exists": True,
        "bytes": p.stat().st_size,
        "gib": round(p.stat().st_size / 1024**3, 4),
        "dtypes": dtypes,
        "bf16_ok": bf16_ok,
    }
    print("ADAPTER_DTYPE", name, report[name])
(out / "adapter_dtype_audit.json").write_text(json.dumps(report, indent=2) + "\n")
if not all(v.get("bf16_ok") for v in report.values() if v.get("exists")):
    raise SystemExit("ADAPTER_NOT_BF16")
print("ADAPTER_BF16_OK")
PY
[ $? -eq 0 ] || fail ADAPTER_DTYPE_FAIL 21

# ---- P6.1 10-task smoke (Plan B: enable_lora + harvest_agent_p314a + master_swe_adapter) ----
# Do NOT touch frozen gemma4-agent/agent/; use working copy under /workspace.
run_retryable SMOKE_RUNNING SMOKE_FAIL SMOKE_DONE \
  bash /workspace/p56c_smoke_stage.sh

# Final persist marker
set_status S3_PERSIST_DONE
s3_sync_stage S3_PERSIST_DONE
echo "PIPELINE_COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat "$OUT/gate_train_ship.json" || true
cat "$OUT/p61_smoke.json" || true
exit 0
