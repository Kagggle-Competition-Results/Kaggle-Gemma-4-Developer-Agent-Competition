#!/bin/bash
# P6.8 hot-fix: resume after GRPO_FAIL (missing traces extract). Skips SFT.
# Requires existing sft_dora_adapter on pod.
set -uo pipefail
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export COPYFILE_DISABLE=1
export AWS_DEFAULT_REGION=us-east-1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"

BUCKET=gemma4-dev-agent-data-650825122703
ROOT=/workspace/gemma4-p68
OUT="$ROOT/results/p68_v3_surgical"
S3_PREFIX="s3://$BUCKET/results/p68_v3_surgical"
STATUS_FILE=/workspace/p68_v3.status
MODEL_ROOT=/models
SFT_OUT="$OUT/sft_dora_adapter"
GRPO_OUT="$OUT/grpo_adapter"
MASTER="$OUT/master_swe_adapter"

if [ -f /workspace/p51_aws_creds.env ]; then
  # shellcheck disable=SC1091
  set -a; source /workspace/p51_aws_creds.env; set +a
fi

cd "$ROOT"
set_status() { echo "$1" | tee "$STATUS_FILE"; }
s3_sync_stage() {
  local label="$1"
  aws s3 sync "$OUT/" "$S3_PREFIX/" --only-show-errors || true
  aws s3 cp /workspace/p68_v3_bootstrap.log "$S3_PREFIX/p68_v3_bootstrap.log" --only-show-errors || true
  aws s3 cp "$STATUS_FILE" "$S3_PREFIX/p68_v3.status" --only-show-errors || true
  echo "S3_SYNC $label $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
cuda_empty_cache() {
  python3 - <<'PY'
import torch
if torch.cuda.is_available():
    torch.cuda.empty_cache()
    free, total = torch.cuda.mem_get_info()
    print(f"CUDA_EMPTY_CACHE free_gb={free/1024**3:.2f}")
PY
}
fail() {
  set_status "$1"
  s3_sync_stage "FAIL_$1"
  exit "${2:-20}"
}

# Prefer existing local SFT; else pull from S3 (exclude giant optimizer checkpoints).
if [ ! -f "$SFT_OUT/adapter_config.json" ] || [ ! -f "$SFT_OUT/adapter_model.safetensors" ]; then
  set_status SFT_SYNC
  mkdir -p "$SFT_OUT"
  aws s3 sync "$S3_PREFIX/sft_dora_adapter/" "$SFT_OUT/" \
    --exclude "checkpoints/*" --only-show-errors \
    || fail SFT_SYNC_FAIL 21
fi
test -f "$SFT_OUT/adapter_config.json" || fail SFT_ADAPTER_MISSING 21
test -f "$SFT_OUT/adapter_model.safetensors" || fail SFT_ADAPTER_MISSING 22
export MODEL_PATH="${MODEL_PATH:-$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct}"
# Model may still need sync on a fresh pod.
if [ ! -f "$MODEL_PATH/config.json" ]; then
  set_status MODEL_SYNC
  mkdir -p "$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct"
  aws s3 sync "s3://$BUCKET/models/gemma-4-31B-it-qat-w4a16-ct/" \
    "$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct/" --only-show-errors \
    || fail MODEL_SYNC_FAIL 14
fi
test -f "$MODEL_PATH/config.json" || fail MODEL_MISSING 15
export SFT_ADAPTER="$SFT_OUT"
echo "SFT_ADAPTER_OK $SFT_OUT"

# Refresh scripts/configs from /workspace uploads
for f in train_grpo.py merge_adapters.py serve_check_4bit.py gate_train_ship.py \
         patch_swegemma_subprocess.py render_trajectories.py run_p56c_smoke.py \
         ensure_subprocess_host_deps.py; do
  [ -f "/workspace/$f" ] && cp "/workspace/$f" "$ROOT/scripts/$f"
done
for f in train_grpo.yaml merge_ties.yaml serve_check_4bit.yaml gate_train_ship.yaml splits.json; do
  [ -f "/workspace/$f" ] && cp "/workspace/$f" "$ROOT/configs/$f"
done
[ -f /workspace/p68_smoke_stage.sh ] && chmod +x /workspace/p68_smoke_stage.sh

# Extract traces (p68 name + legacy p56c name)
set_status TRACES_EXTRACT
for _traces_tgz in /workspace/p68_traces.tgz /workspace/p56c_traces.tgz; do
  if [ -f "$_traces_tgz" ]; then
    tar -xzf "$_traces_tgz" -C "$ROOT"
    echo "TRACES_EXTRACTED $_traces_tgz"
    break
  fi
done
# sanity: at least one meta under configured roots
python3 - <<'PY'
from pathlib import Path
roots = [
    Path("results/p51a_r8_p314a/traces/bon"),
    Path("results/p51b_star_r1/traces/star"),
    Path("results/p51f_star/traces/star"),
    Path("results/p52_wave2_sieve/traces/star"),
    Path("results/p52_wave2_sieve/traces/bon"),
]
n = 0
for r in roots:
    if not r.is_dir():
        print("MISSING_ROOT", r)
        continue
    n += sum(1 for _ in r.rglob("*meta*.json"))
    n += sum(1 for p in r.rglob("*.json") if "meta" in p.name.lower() or p.parent.name in ("keep", "raw"))
print("TRACE_JSON_COUNT_APPROX", n)
if n < 10:
    raise SystemExit("traces still empty after extract")
PY

# Append resume marker into bootstrap log for watchdog continuity
exec >>/workspace/p68_v3_bootstrap.log 2>&1
echo "=== P6.8 RESUME_FROM_GRPO $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

cuda_empty_cache
set_status GRPO_RUNNING
mkdir -p "$GRPO_OUT"
python3 scripts/train_grpo.py --config configs/train_grpo.yaml --out "$GRPO_OUT" \
  && test -f "$GRPO_OUT/adapter_config.json" || fail GRPO_FAIL 20
set_status GRPO_DONE
s3_sync_stage GRPO_DONE
cuda_empty_cache

set_status MERGE_RUNNING
mkdir -p "$MASTER"
python3 scripts/merge_adapters.py --config configs/merge_ties.yaml \
  --adapters "$SFT_OUT" "$GRPO_OUT" --out "$MASTER" \
  && test -f "$MASTER/adapter_config.json" || fail MERGE_FAIL 20
set_status MERGE_DONE
s3_sync_stage MERGE_DONE
cuda_empty_cache

set_status SERVE_CHECK_RUNNING
python3 scripts/serve_check_4bit.py --config configs/serve_check_4bit.yaml \
  --adapter "$MASTER" --out "$OUT/p68_serve_check.json" || fail SERVE_CHECK_FAIL 20
set_status SERVE_CHECK_DONE
s3_sync_stage SERVE_CHECK_DONE
cuda_empty_cache

MASTER_FOR_GATE="$MASTER" OUT_FOR_GATE="$OUT" python3 - <<'PY'
import os, yaml
from pathlib import Path
master = os.environ["MASTER_FOR_GATE"]
out = os.environ["OUT_FOR_GATE"]
for p in [Path("configs/gate_train_ship.yaml"), Path("/workspace/gate_train_ship.yaml")]:
    cfg = yaml.safe_load(p.read_text()) if p.is_file() else {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["adapter"] = master
    cfg["base_model"] = "gemma-4-31b-it-qat-w4a16-ct"
    cfg["serve_check_report"] = f"{out}/p68_serve_check.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    print("GATE_CFG_OK", p, cfg["adapter"])
PY
set_status GATE_RUNNING
python3 scripts/gate_train_ship.py --config configs/gate_train_ship.yaml \
  --out "$OUT/gate_train_ship.json" || fail GATE_FAIL 20
set_status GATE_DONE
s3_sync_stage GATE_DONE
cuda_empty_cache

# bf16 audit
python3 - <<'PY'
import json, struct
from pathlib import Path
out = Path("/workspace/gemma4-p68/results/p68_v3_surgical")
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
    bf16_ok = bool(dtypes) and ("F32" not in dtypes) and ("BF16" in dtypes or "F16" in dtypes)
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

set_status SMOKE_RUNNING
bash /workspace/p68_smoke_stage.sh || fail SMOKE_FAIL 20
set_status SMOKE_DONE
s3_sync_stage SMOKE_DONE

set_status S3_PERSIST_DONE
s3_sync_stage S3_PERSIST_DONE
echo "PIPELINE_COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit 0
