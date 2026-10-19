#!/bin/bash
# P5.6→P5.7→P5.10→P5.19→GATE_TRAIN_SHIP on one 80GB GPU pod.
# Stages sync to s3://…/results/p56_train_pipeline/ after each milestone.
set -uo pipefail
exec >> /workspace/p56_train_bootstrap.log 2>&1
echo "=== P5.6 train pipeline bootstrap $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nvidia-smi | head -n 5 || true

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_IGNORE_REQUIRES_PYTHON=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export AWS_DEFAULT_REGION=us-east-1
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
if [ -f /workspace/p51_aws_creds.env ]; then
  # shellcheck disable=SC1091
  set -a; source /workspace/p51_aws_creds.env; set +a
fi
pip install -q awscli 2>/dev/null || true
hash -r || true

BUCKET=gemma4-dev-agent-data-650825122703
S3_PREFIX="s3://$BUCKET/results/p56_train_pipeline"
ROOT=/workspace/gemma4-p56
STATUS_FILE=/workspace/p56_train.status
MODEL_ROOT=/models
OUT="$ROOT/results/p56_train_pipeline"
mkdir -p "$ROOT" "$MODEL_ROOT" "$OUT" "$ROOT/data_train" "$ROOT/configs" "$ROOT/scripts" "$ROOT/results"

set_status() {
  printf '%s\n' "$1" > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  echo "STATUS=$1"
}

s3_sync_stage() {
  local label="$1"
  aws s3 sync "$OUT/" "$S3_PREFIX/" --only-show-errors || true
  aws s3 cp /workspace/p56_train_bootstrap.log "$S3_PREFIX/p56_train_bootstrap.log" --only-show-errors || true
  aws s3 cp "$STATUS_FILE" "$S3_PREFIX/p56_train.status" --only-show-errors || true
  echo "S3_SYNC $label $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

fail() {
  set_status "$1"
  s3_sync_stage "FAIL_$1"
  exit "${2:-13}"
}

rm -f "$STATUS_FILE"
set_status BOOTSTRAPPING

# Torch CUDA index from driver
python3 - <<'PY'
import re, subprocess
from pathlib import Path
out=subprocess.check_output(["nvidia-smi"], text=True)
m=re.search(r"CUDA Version:\s*([0-9.]+)", out)
ver=float(m.group(1)) if m else 0.0
idx = "cu130" if ver >= 13.0 else "cu128"
Path("/tmp/torch_cu_index.env").write_text(f"export TORCH_CU_INDEX={idx}\nexport CUDA_DRIVER_VER={ver}\n")
print(f"cuda_driver={ver} torch_index={idx}")
PY
# shellcheck disable=SC1091
source /tmp/torch_cu_index.env

# Install train stack (peft/transformers/bitsandbytes/datasets/accelerate)
python3 - <<PY
import subprocess, sys
pkgs = [
  "transformers>=4.45.0",
  "peft>=0.13.0",
  "bitsandbytes>=0.43.0",
  "datasets>=2.19.0",
  "accelerate>=0.33.0",
  "compressed-tensors==0.17.0",
  "safetensors",
  "sentencepiece",
  "protobuf",
  "pyyaml",
]
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *pkgs])
print("PIP_TRAIN_OK")
PY

# Stage uploaded bundle from /workspace
for f in train_qlora.py train_grpo.py merge_adapters.py serve_check_4bit.py gate_train_ship.py \
         patch_swegemma_subprocess.py render_trajectories.py; do
  if [ -f "/workspace/$f" ]; then
    cp "/workspace/$f" "$ROOT/scripts/$f"
  fi
done
for f in train_qlora.yaml train_grpo.yaml merge_ties.yaml serve_check_4bit.yaml gate_train_ship.yaml; do
  if [ -f "/workspace/$f" ]; then
    cp "/workspace/$f" "$ROOT/configs/$f"
  fi
done
if [ -f /workspace/sft.jsonl ]; then
  cp /workspace/sft.jsonl "$ROOT/data_train/sft.jsonl"
fi
# Traces tree (uploaded as tarball or rsync dir)
if [ -f /workspace/p56_traces.tgz ]; then
  # tarball paths are results/<exp>/traces/... relative to gemma4-agent root
  tar -xzf /workspace/p56_traces.tgz -C "$ROOT"
fi
if [ -d /workspace/p56_results ]; then
  mkdir -p "$ROOT/results"
  cp -a /workspace/p56_results/. "$ROOT/results/"
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

# ---- P5.6 DoRA SFT ----
set_status SFT_RUNNING
SFT_OUT="$OUT/sft_dora_adapter"
mkdir -p "$SFT_OUT"
python3 scripts/train_qlora.py --config configs/train_qlora.yaml \
  --data data_train/sft.jsonl --out "$SFT_OUT" \
  || fail SFT_FAIL 20
test -f "$SFT_OUT/adapter_config.json" || fail SFT_NO_ADAPTER 21
set_status SFT_DONE
s3_sync_stage SFT_DONE
echo "SFT_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- P5.10 GRPO ----
set_status GRPO_RUNNING
GRPO_OUT="$OUT/grpo_adapter"
export SFT_ADAPTER="$SFT_OUT"
mkdir -p "$GRPO_OUT"
python3 scripts/train_grpo.py --config configs/train_grpo.yaml --out "$GRPO_OUT" \
  || fail GRPO_FAIL 30
test -f "$GRPO_OUT/adapter_config.json" || fail GRPO_NO_ADAPTER 31
set_status GRPO_DONE
s3_sync_stage GRPO_DONE
echo "GRPO_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- P5.19 TIES merge ----
set_status MERGE_RUNNING
MASTER="$OUT/master_swe_adapter"
mkdir -p "$MASTER"
python3 scripts/merge_adapters.py --config configs/merge_ties.yaml \
  --adapters "$SFT_OUT" "$GRPO_OUT" --out "$MASTER" \
  || fail MERGE_FAIL 40
test -f "$MASTER/adapter_config.json" || fail MERGE_NO_ADAPTER 41
set_status MERGE_DONE
s3_sync_stage MERGE_DONE
echo "MERGE_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- P5.7 4-bit serve-check ----
set_status SERVE_CHECK_RUNNING
# Point gate config at master adapter
python3 - <<PY
import yaml
from pathlib import Path
p = Path("configs/gate_train_ship.yaml")
cfg = yaml.safe_load(p.read_text()) or {}
cfg["adapter"] = "$MASTER"
cfg["base_model"] = "gemma-4-31b-it-qat-w4a16-ct"
cfg["serve_check_report"] = "$OUT/p57_serve_check.json"
p.write_text(yaml.safe_dump(cfg, sort_keys=False))
print("GATE_CFG_OK", cfg["adapter"])
PY
python3 scripts/serve_check_4bit.py --config configs/serve_check_4bit.yaml \
  --adapter "$MASTER" --out "$OUT/p57_serve_check.json" \
  || fail SERVE_CHECK_FAIL 50
set_status SERVE_CHECK_DONE
s3_sync_stage SERVE_CHECK_DONE
echo "SERVE_CHECK_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- GATE_TRAIN_SHIP ----
set_status GATE_RUNNING
python3 scripts/gate_train_ship.py --config configs/gate_train_ship.yaml \
  --out "$OUT/gate_train_ship.json" \
  || fail GATE_FAIL 60
set_status GATE_DONE
s3_sync_stage GATE_DONE

# Final persist marker
set_status S3_PERSIST_DONE
s3_sync_stage S3_PERSIST_DONE
echo "PIPELINE_COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat "$OUT/gate_train_ship.json" || true
exit 0
